# -*- coding: utf-8 -*-
"""engine 子进程管理、事件解析、登录会话状态机与采集串行编排。

约定见 ``app/CONTRACT.md`` §1（Engine 命令行 / ``##EVENT##`` 事件协议）与 §2（Sidecar）。

本模块只依赖标准库 + 同目录 ``state.py``，不 import FastAPI；由 ``main.py``
实例化 :class:`EngineService` 并通过 HTTP 暴露能力。

关键行为
--------
* 引擎进程：``python app/engine/entry.py --mode ... --platform ... --data-dir ...``，
  ``cwd`` 固定为 MediaCrawler 根目录，``PYTHONUNBUFFERED=1``，stdout/stderr 合并逐行读取。
* ``##EVENT##`` 行解析为事件；其余行落盘 ``<data-dir>/logs/<scope>.log`` 并在内存
  保留尾部 5000 行（供 ``GET /api/runs/{id}/logs`` 使用）。
* 并发（按平台解耦）：同一时间只允许一个 login（全局）与一个 run（全局保守）；
  login/test 仅同平台互斥、不同平台可并行（各自 browser_data profile 互不冲突）。
  冲突时抛 ``ServiceError("BUSY", ...)``，message 区分「该平台正在检测」/「该平台正在登录」/全局任务。
* 登录会话：``starting -> waiting_qr -> waiting_scan -> confirmed|expired|failed|cancelled``。
* 采集：按平台串行，单平台失败继续下一个；全部结束 run=done（有失败也 done），
  用户 stop 则 run=cancelled；进程内异常才置 run=failed。
* 取消/停止：杀引擎进程树（Windows ``taskkill /F /T``），登录会话取消后状态置 cancelled。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

# 允许 `python app/sidecar/main.py`、`python -m app.sidecar.engine_runner` 等不同启动方式
_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

from state import StateStore, now_ts  # noqa: E402  (本地模块，需先修 sys.path)
from events import bus  # noqa: E402  (进程内事件总线：run.* / account.* 通知事件)
from version import APP_VERSION as SIDECAR_VERSION  # noqa: E402  (版本号唯一来源)

log = logging.getLogger("sidecar.engine")

SIDECAR_DIR = Path(__file__).resolve().parent
APP_DIR = SIDECAR_DIR.parent
PROJECT_ROOT = APP_DIR.parent

# (platform, label) —— 顺序固定，UI 依赖；契约 §2.2 platform_labels
PLATFORMS: List[Tuple[str, str]] = [
    ("xhs", "小红书"),
    ("dy", "抖音"),
    ("ks", "快手"),
    ("bili", "B站"),
    ("wb", "微博"),
    ("tieba", "贴吧"),
    ("zhihu", "知乎"),
]
PLATFORM_LABELS: Dict[str, str] = dict(PLATFORMS)
PLATFORM_IDS: List[str] = [item[0] for item in PLATFORMS]

EVENT_PREFIX = "##EVENT##"
LOG_TAIL_MAX = 5000

# 登录会话终态（对应契约 §2.2 status 枚举）
LOGIN_TERMINAL_STATUSES = frozenset({"confirmed", "failed", "expired", "cancelled"})
# engine login.status -> API status。
# need_manual = 平台要求人工接管（滑块/拼图/短信验证码），不是失败，UI 需提示用户去浏览器窗口完成。
_ENGINE_LOGIN_STATUS_MAP = {
    "waiting_qr": "waiting_qr",
    "waiting_scan": "waiting_scan",
    "need_manual": "need_manual",
    "confirmed": "confirmed",
    "expired": "expired",
    "failed": "failed",
}

# 人工接管类型（与 engine 约定 kind 一致）
NEED_MANUAL_KINDS = ("sms", "slider", "captcha")

# 登录超时默认 420s（含用户在弹出浏览器窗口完成短信/滑块验证的时间）
DEFAULT_LOGIN_TIMEOUT = 420
# 有头登录时，给用户额外留出的看门狗宽限
LOGIN_WATCHDOG_GRACE = 120

RUN_STATUS_RUNNING = "running"
RUN_STATUS_DONE = "done"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_RE_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)

_ERROR_HINTS = ("traceback", "error", "exception", "fatal", "critical", "失败", "错误", "异常")
_WARN_HINTS = ("warn", "warning", "警告", "超时", "timeout")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return str(value).strip() if value is not None else default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def classify_log_level(text: str) -> str:
    """把引擎原始日志行粗分类为 info/warn/error（供 UI 上色）。"""
    low = (text or "").lower()
    if any(hint in low for hint in _ERROR_HINTS):
        return "error"
    if any(hint in low for hint in _WARN_HINTS):
        return "warn"
    return "info"


def _calc_progress(saved: int, expected: int, stage: str) -> int:
    """由事件里的 saved/stage 估算 0-100 进度（不追求精确，UI 只做展示）。"""
    if stage == "done":
        return 100
    if expected and expected > 0:
        return max(1, min(99, int(saved * 100 / expected)))
    stage_weights = {"boot": 5, "login": 10, "search": 20, "detail": 40, "comments": 60}
    return stage_weights.get(stage, 0)


def _kill_process_tree(proc: "subprocess.Popen[str]") -> None:
    """尽力杀掉引擎进程及其子进程（Playwright/Edge 子进程在树内）。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=15,
                creationflags=_RE_CREATE_NO_WINDOW,
            )
        except Exception:  # noqa: BLE001 - 杀进程失败时回退 terminate
            pass
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# need_manual（人工接管）工具
# ---------------------------------------------------------------------------
_SMS_HINTS = ("短信", "验证码", "手机号", "手机", "sms", "code")
_SLIDER_HINTS = ("滑块", "拼图", "拖动", "滑动", "slider", "captcha", "拼图验证")
_CAPTCHA_HINTS = ("验证", "captcha", "verify", "安全验证", "人机")


def infer_need_manual_kind(event: Dict[str, Any]) -> str:
    """从 engine 的 need_manual 事件推断 kind（sms/slider/captcha）。

    优先取事件里的 ``kind`` 字段（也兼容 ``need_manual.kind`` 嵌套），
    engine 老版本没带 kind 时按 message 关键词兜底推断。
    """
    candidates: List[Any] = [event.get("kind")]
    nested = event.get("need_manual")
    if isinstance(nested, dict):
        candidates.append(nested.get("kind"))
    for raw in candidates:
        kind = str(raw or "").strip().lower()
        if kind in NEED_MANUAL_KINDS:
            return kind
    message = str(event.get("message") or "").lower()
    for kind, hints in (
        ("sms", _SMS_HINTS),
        ("slider", _SLIDER_HINTS),
        ("captcha", _CAPTCHA_HINTS),
    ):
        if any(hint in message for hint in hints):
            return kind
    return ""


def default_need_manual_message(kind: str) -> str:
    """UI 展示用默认文案（engine 未给 message 时）。"""
    return {
        "sms": "平台需要短信验证：请在弹出的浏览器窗口输入手机收到的验证码，完成后会自动继续（请勿重复扫码）",
        "slider": "平台需要滑块/拼图验证：请在弹出的浏览器窗口完成验证，完成后会自动继续",
        "captcha": "平台需要安全验证：请在弹出的浏览器窗口中完成验证，完成后会自动继续",
    }.get(kind, "平台需要人工验证：请在弹出的浏览器窗口中完成滑块/拼图/短信验证，完成后会自动继续")


def _csv_process_row(line: str) -> Optional[Dict[str, Any]]:
    """解析 wmic ``/format:csv`` 一行：``Node,CommandLine,ProcessId``。"""
    line = line.strip()
    if not line or line.upper().startswith("NODE,"):
        return None
    head, sep, pid_text = line.rpartition(",")
    if not sep or not pid_text.strip().isdigit():
        return None
    cmdline = head.split(",", 1)[1] if "," in head else head
    return {"pid": int(pid_text.strip()), "commandline": cmdline}


def list_browser_processes() -> List[Dict[str, Any]]:
    """列出当前 msedge/chrome 进程（pid + 命令行）；仅 Windows 实现，其它平台返回空。"""
    if os.name != "nt":
        return []
    processes: List[Dict[str, Any]] = []
    # 1) wmic（快，Win10 可用）
    try:
        result = subprocess.run(
            [
                "wmic", "process", "where", "name='msedge.exe' or name='chrome.exe'",
                "get", "processid,commandline", "/format:csv",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=_RE_CREATE_NO_WINDOW,
        )
        for line in (result.stdout or "").splitlines():
            row = _csv_process_row(line)
            if row is not None:
                processes.append(row)
        if processes:
            return processes
    except Exception:  # noqa: BLE001 - wmic 不存在/被裁剪时走 PowerShell 回退
        pass
    # 2) PowerShell CIM 回退
    try:
        ps_script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -eq 'msedge.exe' -or $_.Name -eq 'chrome.exe' } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=_RE_CREATE_NO_WINDOW,
        )
        for line in (result.stdout or "").splitlines():
            if "\t" not in line:
                continue
            pid_text, cmdline = line.split("\t", 1)
            if pid_text.strip().isdigit():
                processes.append({"pid": int(pid_text.strip()), "commandline": cmdline})
    except Exception:  # noqa: BLE001
        pass
    return processes


def kill_browser_processes_by_marker(marker: str) -> List[int]:
    """按 profile 路径特征杀掉残留浏览器进程（防止取消登录后 Edge 继续驻留）。

    ``marker`` 一般是 ``<data-dir>/browser_data/<platform>_user_data_dir``；
    只有命令行里带该路径的 msedge/chrome 才会被杀，用户自己的浏览器不受影响。
    """
    marker_lower = str(marker or "").strip().lower()
    if not marker_lower or os.name != "nt":
        return []
    victims = [
        item["pid"]
        for item in list_browser_processes()
        if marker_lower in str(item.get("commandline") or "").lower()
    ]
    killed: List[int] = []
    for pid in victims:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=15,
                creationflags=_RE_CREATE_NO_WINDOW,
            )
            killed.append(pid)
        except Exception:  # noqa: BLE001
            pass
    return killed


# ---------------------------------------------------------------------------
# 错误 / 配置
# ---------------------------------------------------------------------------
class ServiceError(Exception):
    """可映射为 HTTP 错误体 ``{"error":{"code","message"}}`` 的业务异常。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class EngineStartError(Exception):
    """引擎进程无法启动（入口缺失、python 不存在等）。"""


@dataclass
class SidecarConfig:
    """sidecar 运行配置（所有路径均解析为绝对路径）。"""

    data_dir: Path
    mc_root: Path
    python: Path
    engine_entry: Path
    fake_engine: bool = False
    login_timeout: int = DEFAULT_LOGIN_TIMEOUT
    login_headless: bool = False
    test_timeout: float = 90.0
    crawl_timeout: float = 1800.0

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def qr_dir(self) -> Path:
        return self.data_dir / "qr"

    @property
    def crawler_dir(self) -> Path:
        return self.data_dir / "crawler"

    @property
    def browser_data_dir(self) -> Path:
        return self.data_dir / "browser_data"

    @property
    def app_db(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def media_db(self) -> Path:
        return self.crawler_dir / "media_crawler.db"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.logs_dir,
            self.exports_dir,
            self.qr_dir,
            self.crawler_dir,
            self.browser_data_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def platform_profile_template(self, platform: str, account_id: str = "") -> str:
        """账号 profile 目录的「模板」或旧单账号实际路径（多账号号池契约 §6.2）。

        - ``account_id`` 为空 → 旧路径 ``<data-dir>/browser_data/<platform>_user_data_dir``
          （已解析为具体平台，无 ``%s``，用于 kill_orphan/list/delete 等旧调用）；
        - 指定账号 → ``<data-dir>/browser_data/%s/<account_id>/user_data_dir``，
          ``%s`` 由 MediaCrawler 替换为平台名（7 平台 ``core.py`` 统一格式）。
        """
        account_id = str(account_id or "").strip()
        if account_id:
            return str(self.browser_data_dir / "%s" / account_id / "user_data_dir")
        return str(self.browser_data_dir / f"{platform}_user_data_dir")

    def account_profile_template(
        self, platform: str, account_id: str, profile_layout: str = "v2"
    ) -> str:
        """账号 profile 模板（含恰好一个 ``%s``）：legacy 账号沿用旧模板。"""
        if (profile_layout or "v2") == "legacy":
            return str(self.browser_data_dir / "%s_user_data_dir")
        return self.platform_profile_template(platform, account_id)

    def platform_profile_dir(self, platform: str, account_id: str = "") -> Path:
        """账号 profile 实际目录（兼容旧签名；v2 账号自动解析模板里的 ``%s``）。"""
        raw = self.platform_profile_template(platform, account_id)
        return Path(raw % platform) if "%s" in raw else Path(raw)

    @classmethod
    def from_env(
        cls,
        data_dir: str | Path,
        mc_root: str | Path | None = None,
        python: str | Path | None = None,
        engine_entry: str | Path | None = None,
    ) -> "SidecarConfig":
        data_path = Path(data_dir).expanduser().resolve()

        mc_env = _env_str("MC_ROOT")
        if mc_root is not None:
            mc_path = Path(mc_root).expanduser().resolve()
        elif mc_env:
            mc_path = Path(mc_env).expanduser().resolve()
        else:
            mc_path = (PROJECT_ROOT / "MediaCrawler").resolve()

        fake_engine = _truthy(os.environ.get("FAKE_ENGINE"), False)

        if python is not None:
            py_path = Path(python).expanduser().resolve()
        else:
            py_env = _env_str("SIDECAR_PYTHON") or _env_str("MC_PYTHON")
            candidates: List[Path] = []
            if py_env:
                candidates.append(Path(py_env).expanduser().resolve())
            candidates.append(mc_path / ".venv" / "Scripts" / "python.exe")
            candidates.append(mc_path / ".venv" / "bin" / "python")
            py_path = next((p for p in candidates if p.is_file()), Path(sys.executable))

        entry_env = _env_str("SIDECAR_ENGINE_ENTRY")
        if engine_entry is not None:
            entry_path = Path(engine_entry).expanduser().resolve()
        elif entry_env:
            entry_path = Path(entry_env).expanduser().resolve()
        elif fake_engine:
            entry_path = (SIDECAR_DIR / "_fake_engine.py").resolve()
        else:
            entry_path = (APP_DIR / "engine" / "entry.py").resolve()

        login_timeout = _to_int(_env_str("SIDECAR_LOGIN_TIMEOUT"), DEFAULT_LOGIN_TIMEOUT) or DEFAULT_LOGIN_TIMEOUT
        # 登录默认「有头」窗口：用户在可见 Edge 窗口完成扫码后的短信/滑块/拼图验证。
        # SIDECAR_LOGIN_HEADLESS=1/true 可强制回无头（自动化/无桌面环境）。
        login_headless_env = _env_str("SIDECAR_LOGIN_HEADLESS")
        login_headless = _truthy(login_headless_env, False) if login_headless_env else False
        test_timeout = float(_env_str("SIDECAR_TEST_TIMEOUT") or 90.0)
        crawl_timeout = float(_env_str("SIDECAR_CRAWL_TIMEOUT") or 1800.0)

        return cls(
            data_dir=data_path,
            mc_root=mc_path,
            python=py_path,
            engine_entry=entry_path,
            fake_engine=fake_engine,
            login_timeout=max(10, login_timeout),
            login_headless=login_headless,
            test_timeout=max(5.0, test_timeout),
            crawl_timeout=max(30.0, crawl_timeout),
        )

    def login_headless_arg(self) -> str:
        """传给 engine 的 ``--headless`` 参数（登录模式）。"""
        return "true" if self.login_headless else "false"


# ---------------------------------------------------------------------------
# 日志落盘 + 内存尾部
# ---------------------------------------------------------------------------
class LogSink:
    """一个 scope 的日志：写 ``<data-dir>/logs/<scope>.log`` 且内存保留尾部 5000 行。"""

    def __init__(self, path: Path, scope: str = "", maxlen: int = LOG_TAIL_MAX) -> None:
        self.path = Path(path)
        self.scope = scope or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._lines: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._closed = False
        try:
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
        except OSError:  # 落盘失败时仍保留内存日志
            self._fh = None

    def append(self, text: str) -> None:
        text = "" if text is None else str(text)
        entry = {"ts": now_ts(), "level": classify_log_level(text), "text": text}
        with self._lock:
            self._lines.append(entry)
            if self._fh is not None and not self._closed:
                try:
                    self._fh.write(text + "\n")
                except Exception:  # noqa: BLE001
                    self._fh = None

    def tail(self, count: int = 200) -> List[Dict[str, Any]]:
        count = max(1, min(int(count or 200), LOG_TAIL_MAX))
        with self._lock:
            lines = list(self._lines)
        return lines[-count:]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._fh is not None:
                try:
                    self._fh.flush()
                    self._fh.close()
                except Exception:  # noqa: BLE001
                    pass
                self._fh = None


def read_log_file_tail(path: Path, count: int) -> List[Dict[str, Any]]:
    """sidecar 重启后从日志文件读取尾部（ts 近似为文件 mtime）。"""
    count = max(1, min(int(count or 200), LOG_TAIL_MAX))
    if not path.is_file():
        return []
    lines: Deque[str] = deque(maxlen=count)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                lines.append(line.rstrip("\r\n"))
        mtime = int(path.stat().st_mtime)
    except OSError:
        return []
    return [
        {"ts": mtime, "level": classify_log_level(text), "text": text}
        for text in lines
        if text
    ]


# ---------------------------------------------------------------------------
# 引擎进程
# ---------------------------------------------------------------------------
class EngineProcess:
    """一个 engine 子进程：合并 stdout/stderr 逐行读取并分发事件。"""

    def __init__(
        self,
        cmd: List[str],
        cwd: Path,
        env: Dict[str, str],
        sink: LogSink,
        on_event: Callable[[Dict[str, Any]], None],
        label: str = "",
    ) -> None:
        self.cmd = list(cmd)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.sink = sink
        self.on_event = on_event
        self.label = label
        self.returncode: Optional[int] = None
        self._proc: Optional[subprocess.Popen[str]] = None
        self._thread: Optional[threading.Thread] = None
        self._finished = threading.Event()
        self._terminating = False

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "EngineProcess":
        if self._proc is not None:
            return self
        self._proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_RE_CREATE_NO_WINDOW,
        )
        self._thread = threading.Thread(
            target=self._read_loop, name=f"engine-{self.label or 'proc'}", daemon=True
        )
        self._thread.start()
        return self

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                raw = line.rstrip("\r\n")
                if not raw:
                    continue
                stripped = raw.lstrip()
                if stripped.startswith(EVENT_PREFIX):
                    payload = stripped[len(EVENT_PREFIX):].strip()
                    event: Any = None
                    try:
                        event = json.loads(payload)
                    except (TypeError, ValueError):
                        event = None
                    if isinstance(event, dict):
                        try:
                            self.on_event(event)
                        except Exception:  # noqa: BLE001 - 事件处理异常不拖垮读取线程
                            log.exception("处理 engine 事件失败：%s", payload[:300])
                    else:
                        self.sink.append(raw)
                else:
                    self.sink.append(raw)
        except Exception as exc:  # noqa: BLE001 - 读取端异常同样落日志
            self.sink.append(f"[sidecar] 读取引擎输出异常：{exc}")
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                pass
            try:
                self.returncode = proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self.returncode = proc.poll()
            self._finished.set()

    # -- 查询 / 终止 ------------------------------------------------------
    @property
    def pid(self) -> Optional[int]:
        return None if self._proc is None else self._proc.pid

    def wait(self, timeout: Optional[float] = None) -> bool:
        """等待进程退出（含输出读完）；返回 True=已退出。"""
        return self._finished.wait(timeout)

    def is_running(self) -> bool:
        return not self._finished.is_set()

    def terminate(self) -> None:
        proc = self._proc
        if proc is None or self._terminating:
            return
        self._terminating = True
        try:
            _kill_process_tree(proc)
        finally:
            self._finished.wait(5)

    def kill(self) -> None:
        self.terminate()


# ---------------------------------------------------------------------------
# 引擎命令构造
# ---------------------------------------------------------------------------
class EngineRunner:
    """按契约 §1.1 构造并启动 engine 子进程。"""

    def __init__(self, cfg: SidecarConfig) -> None:
        self.cfg = cfg

    def _base_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["MC_ROOT"] = str(self.cfg.mc_root)
        env.setdefault("BROWSER_CHANNEL", "msedge")
        # 不把 sidecar 自身的敏感环境变量透传给引擎
        for key in ("SIDECAR_TOKEN", "SIDECAR_EXPORTER_MODULE"):
            env.pop(key, None)
        return env

    def _entry_path(self) -> Path:
        return Path(self.cfg.engine_entry)

    def _spawn(
        self,
        args: List[str],
        sink: LogSink,
        on_event: Callable[[Dict[str, Any]], None],
        label: str,
    ) -> EngineProcess:
        entry = self._entry_path()
        if not entry.is_file():
            raise EngineStartError(f"engine 入口不存在：{entry}")
        if not self.cfg.mc_root.is_dir():
            raise EngineStartError(f"MediaCrawler 根目录不存在：{self.cfg.mc_root}")
        python = Path(self.cfg.python)
        if not python.is_file():
            raise EngineStartError(f"Python 解释器不存在：{python}")
        cmd = [str(python), str(entry)] + list(args)
        sink.append("[sidecar] 启动引擎：" + " ".join(cmd))
        proc = EngineProcess(
            cmd=cmd,
            cwd=self.cfg.mc_root,
            env=self._base_env(),
            sink=sink,
            on_event=on_event,
            label=label,
        )
        proc.start()
        sink.append(f"[sidecar] 引擎已启动 pid={proc.pid}")
        return proc

    # -- 三种模式 --------------------------------------------------------
    def _account_extra_args(self, account_id: str = "", profile_template: str = "") -> List[str]:
        """账号化附加参数（空账号不追加，保证旧 CLI 逐字节兼容）。"""
        extra: List[str] = []
        account_id = str(account_id or "").strip()
        profile_template = str(profile_template or "").strip()
        if account_id:
            extra.extend(["--account-id", account_id])
        if profile_template:
            extra.extend(["--profile-template", profile_template])
        return extra

    def login_args(
        self,
        platform: str,
        account_id: str = "",
        profile_template: str = "",
    ) -> List[str]:
        """登录模式命令行参数（有头窗口：用户在可见 Edge 里完成扫码后的二次验证）。"""
        return [
            "--mode", "login",
            "--platform", platform,
            "--data-dir", str(self.cfg.data_dir),
            "--headless", self.cfg.login_headless_arg(),
            "--timeout", str(self.cfg.login_timeout),
        ] + self._account_extra_args(account_id, profile_template)

    def test_args(
        self,
        platform: str,
        account_id: str = "",
        profile_template: str = "",
    ) -> List[str]:
        """test 模式命令行参数（始终保持无头，不弹窗口）。"""
        return [
            "--mode", "test",
            "--platform", platform,
            "--data-dir", str(self.cfg.data_dir),
            "--headless", "true",
        ] + self._account_extra_args(account_id, profile_template)

    @staticmethod
    def _scope_suffix(account_id: str) -> str:
        """日志 scope 的账号短号（仅 v2 账号有 engine_account_id）。"""
        account_id = str(account_id or "").strip()
        return f"-{account_id[-6:]}" if account_id else ""

    def start_login(
        self,
        platform: str,
        sink: LogSink,
        on_event: Callable[[Dict[str, Any]], None],
        account_id: str = "",
        profile_template: str = "",
    ) -> EngineProcess:
        label = f"login-{platform}{self._scope_suffix(account_id)}"
        return self._spawn(
            self.login_args(platform, account_id, profile_template), sink, on_event, label=label
        )

    def start_test(
        self,
        platform: str,
        sink: LogSink,
        on_event: Callable[[Dict[str, Any]], None],
        account_id: str = "",
        profile_template: str = "",
    ) -> EngineProcess:
        label = f"test-{platform}{self._scope_suffix(account_id)}"
        return self._spawn(
            self.test_args(platform, account_id, profile_template), sink, on_event, label=label
        )

    def kill_orphan_browsers(
        self,
        platform: str,
        account_id: str = "",
        profile_template: str = "",
    ) -> List[int]:
        """杀掉该账号 profile 下残留的浏览器进程（取消登录/引擎异常退出后兜底）。

        ``profile_template`` 含 ``%s`` 时先替换成平台名再作为命令行 marker；
        为空时按 ``platform_profile_dir(platform, account_id)`` 取实际目录。
        """
        template = str(profile_template or "").strip()
        if template and "%s" in template:
            marker = template % platform
        else:
            marker = str(self.cfg.platform_profile_dir(platform, account_id))
        return kill_browser_processes_by_marker(marker)

    def start_crawl(
        self,
        platform: str,
        keywords: List[str],
        max_notes: int,
        comments: bool,
        max_comments: int,
        sink: LogSink,
        on_event: Callable[[Dict[str, Any]], None],
        account_id: str = "",
        profile_template: str = "",
    ) -> EngineProcess:
        args = [
            "--mode", "crawl",
            "--platform", platform,
            "--keywords", ",".join(keywords),
            "--max-notes", str(int(max_notes)),
            "--comments", "true" if comments else "false",
            "--max-comments", str(int(max_comments)),
            "--data-dir", str(self.cfg.data_dir),
        ] + self._account_extra_args(account_id, profile_template)
        label = f"crawl-{platform}{self._scope_suffix(account_id)}"
        return self._spawn(args, sink, on_event, label=label)


# ---------------------------------------------------------------------------
# 并发控制（登录/检测按平台解耦）
#   - login / test 按平台互斥：同平台不可同时 login+test；不同平台可并行
#     （不同 browser_data profile，互不冲突）
#   - 同一时间只允许一个 login（全局，跨平台也串行）
#   - run 全局保守：run 进行中拒绝 login/test；有任意 test/login 时拒绝 run
#   - BUSY message 区分：同平台检测中 / 同平台登录中 / 全局
# ---------------------------------------------------------------------------
BUSY_GLOBAL_MESSAGE = "当前有登录/采集任务进行中，请等待结束或先取消"
BUSY_PLATFORM_TEST_MESSAGE = "该平台正在检测，请稍候"
BUSY_PLATFORM_LOGIN_MESSAGE = "该平台正在登录，请稍候"
BUSY_ACCOUNT_MESSAGE = "该账号正在被登录/检测/采集任务使用，请稍候"
# 兼容旧引用（登录/采集全局互斥的默认文案）
_BUSY_MESSAGE = BUSY_GLOBAL_MESSAGE


class ActivityGuard:
    """并发闸门：login 全局唯一；login/test 按平台/账号互斥；run 全局保守；账号级独占。

    账号化（契约 §6.2）：
    * ``acquire_account(account_id, ref)`` 保证同一账号 profile 同一时间只被一个引擎进程使用；
    * test 槽按 ``platform`` 保留旧语义（account_id 为空），指定账号时按 ``platform@account_id``；
    * login 仍全局唯一（一次只弹一个 Edge 登录窗）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 全局互斥槽（login / run 二选一）；login 时记录平台，用于按平台解耦
        self._exclusive_kind: Optional[str] = None
        self._exclusive_ref: Optional[str] = None
        self._exclusive_platform: Optional[str] = None
        # 按平台的 test 槽：旧语义 key=platform；账号级 key="platform@account_id"
        self._tests: Dict[str, str] = {}
        # 账号独占槽：account_id -> ref（一账号一引擎进程）
        self._accounts: Dict[str, str] = {}

    # -- login / run ----------------------------------------------------
    def acquire_exclusive(
        self,
        kind: str,
        ref: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[str]:
        """申请 login/run 全局槽；成功返回 None，失败返回 BUSY message。

        - 已有 login/run → BUSY_GLOBAL_MESSAGE（同一时间只允许一个 login/run）
        - kind=login 且该平台正在 test → BUSY_PLATFORM_TEST_MESSAGE
        - kind=login 且其它平台在 test → 允许（不同 browser_data profile 可并行）
        - kind=run → 保守：有任意 test 则 BUSY_GLOBAL_MESSAGE
        """
        with self._lock:
            if self._exclusive_kind is not None:
                return BUSY_GLOBAL_MESSAGE
            if kind == "login":
                if platform is not None and self._platform_has_test(platform):
                    return BUSY_PLATFORM_TEST_MESSAGE
                # 其它平台的 test 允许与登录并行
            else:  # run：全局保守，任何 test 都拒绝
                if self._tests:
                    return BUSY_GLOBAL_MESSAGE
            self._exclusive_kind = kind
            self._exclusive_ref = ref
            self._exclusive_platform = platform if kind == "login" else None
            return None

    def release_exclusive(self, ref: Optional[str] = None) -> None:
        with self._lock:
            if ref is not None and self._exclusive_ref is not None and ref != self._exclusive_ref:
                return
            self._exclusive_kind = None
            self._exclusive_ref = None
            self._exclusive_platform = None

    # -- test：按平台/账号并行 -------------------------------------------
    @staticmethod
    def _test_key(platform: str, account_id: str = "") -> str:
        account_id = str(account_id or "").strip()
        return f"{platform}@{account_id}" if account_id else str(platform)

    def _platform_has_test(self, platform: str) -> bool:
        """调用方需持锁：某平台是否有任意 test 槽（含账号级）。"""
        for key in self._tests:
            if key == platform or key.startswith(f"{platform}@"):
                return True
        return False

    def acquire_test(
        self,
        platform: str,
        ref: Optional[str] = None,
        account_id: str = "",
    ) -> Optional[str]:
        """申请 test 槽；成功返回 None。

        - 同平台同账号已有 test → BUSY_PLATFORM_TEST_MESSAGE（旧接口 account_id 空=平台级）
        - 有 run 进行中 → BUSY_GLOBAL_MESSAGE
        - 有 login：同平台 → BUSY_PLATFORM_LOGIN_MESSAGE；其它平台 → 允许
        - 账号已被占用（run/login 持有账号锁）→ BUSY_ACCOUNT_MESSAGE
        """
        key = self._test_key(platform, account_id)
        with self._lock:
            if self._exclusive_kind == "run":
                return BUSY_GLOBAL_MESSAGE
            if key in self._tests:
                return BUSY_PLATFORM_TEST_MESSAGE
            if self._exclusive_kind == "login" and self._exclusive_platform == platform:
                return BUSY_PLATFORM_LOGIN_MESSAGE
            account_id = str(account_id or "").strip()
            if account_id and account_id in self._accounts:
                return BUSY_ACCOUNT_MESSAGE
            self._tests[key] = ref or ""
            return None

    def release_test(
        self,
        platform: str,
        ref: Optional[str] = None,
        account_id: str = "",
    ) -> None:
        key = self._test_key(platform, account_id)
        with self._lock:
            current = self._tests.get(key)
            if current is None:
                return
            if ref is not None and current and current != ref:
                return
            self._tests.pop(key, None)

    # -- 账号独占（一账号一 profile 一进程） ------------------------------
    def acquire_account(self, account_id: str, ref: Optional[str] = None) -> Optional[str]:
        """申请账号独占锁；成功返回 None，被占用返回 BUSY_ACCOUNT_MESSAGE。"""
        account_id = str(account_id or "").strip()
        if not account_id:
            return None
        with self._lock:
            if account_id in self._accounts:
                return BUSY_ACCOUNT_MESSAGE
            self._accounts[account_id] = ref or ""
            return None

    def release_account(self, account_id: str, ref: Optional[str] = None) -> None:
        account_id = str(account_id or "").strip()
        if not account_id:
            return
        with self._lock:
            current = self._accounts.get(account_id)
            if current is None:
                return
            if ref is not None and current and current != ref:
                return
            self._accounts.pop(account_id, None)

    def is_account_busy(self, account_id: str) -> bool:
        account_id = str(account_id or "").strip()
        if not account_id:
            return False
        with self._lock:
            return account_id in self._accounts

    def account_ref(self, account_id: str) -> str:
        with self._lock:
            return self._accounts.get(str(account_id or "").strip(), "")

    # -- 查询 -------------------------------------------------------------
    def is_exclusive_busy(self) -> bool:
        with self._lock:
            return self._exclusive_kind is not None

    def is_test_busy(self, platform: Optional[str] = None) -> bool:
        with self._lock:
            if platform is None:
                return bool(self._tests)
            return self._platform_has_test(platform)

    def is_busy(self) -> bool:
        """是否有任意 login/run/test 在跑（delete_account 等场景用）。"""
        with self._lock:
            return self._exclusive_kind is not None or bool(self._tests)

    def describe(self) -> str:
        with self._lock:
            parts: List[str] = []
            if self._exclusive_kind is not None:
                label = self._exclusive_kind
                if self._exclusive_platform:
                    label += f"@{self._exclusive_platform}"
                parts.append(f"{label}:{self._exclusive_ref or ''}")
            if self._tests:
                parts.append("tests=" + ",".join(sorted(self._tests)))
            if self._accounts:
                parts.append("accounts=" + ",".join(sorted(self._accounts)))
            return ";".join(parts) if parts else "idle"



# ---------------------------------------------------------------------------
# 登录会话
# ---------------------------------------------------------------------------
@dataclass
class LoginSession:
    login_id: str
    platform: str
    # 账号化（契约 §2.3.1：key 仍是 login_id，只给 session 加 account 字段）
    account_id: str = ""
    account_alias: str = ""
    engine_account_id: str = ""
    profile_template: str = ""
    status: str = "starting"
    qr: Optional[Dict[str, Any]] = None
    message: str = ""
    started_at: int = field(default_factory=now_ts)
    finished_at: Optional[int] = None
    process: Optional[EngineProcess] = None
    sink: Optional[LogSink] = None
    stop_requested: bool = False
    released: bool = False
    # 人工接管（滑块/拼图/短信验证码）：最近一次 need_manual 信息，粘性保留到终态
    kind: str = ""
    need_manual: Optional[Dict[str, Any]] = None
    need_manual_count: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def is_terminal(self) -> bool:
        with self.lock:
            return self.status in LOGIN_TERMINAL_STATUSES

    def to_api(self) -> Dict[str, Any]:
        with self.lock:
            qr = None
            if self.qr:
                qr = {
                    "seq": self.qr.get("seq"),
                    "image": self.qr.get("image"),
                    "file": self.qr.get("file"),
                }
            need_manual = dict(self.need_manual) if self.need_manual else None
            return {
                "login_id": self.login_id,
                "platform": self.platform,
                "account_id": self.account_id,
                "account_alias": self.account_alias,
                "status": self.status,
                "qr": qr,
                "message": self.message,
                "started_at": self.started_at,
                # 契约外附加字段（任务要求）：人工接管信息供 UI 展示
                "kind": self.kind,
                "need_manual": need_manual,
                "manual_required": self.status == "need_manual",
            }


class LoginManager:
    """登录会话状态机：spawn login 模式 engine，事件驱动状态流转。"""

    def __init__(
        self,
        runner: EngineRunner,
        state: StateStore,
        guard: ActivityGuard,
        cfg: SidecarConfig,
        accounts: Optional[Any] = None,
    ) -> None:
        self.runner = runner
        self.state = state
        self.guard = guard
        self.cfg = cfg
        self.accounts = accounts
        self._lock = threading.RLock()
        self._sessions: Dict[str, LoginSession] = {}
        self._active_login_id: Optional[str] = None
        self._timers: Dict[str, threading.Timer] = {}
        self._release_timers: Dict[str, threading.Timer] = {}

    # -- 对外接口 ---------------------------------------------------------
    def _resolve_account(self, platform: str, account_id: str = "") -> Optional[Dict[str, Any]]:
        if self.accounts is None:
            return None
        try:
            return self.accounts.resolve(platform, account_id or "")
        except ServiceError:
            raise
        except Exception:  # noqa: BLE001 - 账号模块异常时退回旧单账号行为
            log.exception("解析账号失败（退回旧单账号行为）：platform=%s account_id=%s", platform, account_id)
            return None

    def start(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        login_id = "login_" + uuid.uuid4().hex[:12]
        account = self._resolve_account(platform, account_id)
        account_id_resolved = str(account["account_id"]) if account else ""
        engine_args = (
            self.accounts.engine_account_args(account)
            if (self.accounts is not None and account is not None)
            else {"account_id": "", "profile_template": ""}
        )
        session = LoginSession(
            login_id=login_id,
            platform=platform,
            account_id=account_id_resolved,
            account_alias=str(account.get("display_alias") or account.get("alias") or "") if account else "",
            engine_account_id=str(engine_args.get("account_id") or ""),
            profile_template=str(engine_args.get("profile_template") or ""),
        )
        busy = self.guard.acquire_exclusive("login", login_id, platform=platform)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        account_busy = (
            self.guard.acquire_account(account_id_resolved, login_id)
            if account_id_resolved
            else None
        )
        if account_busy:
            self.guard.release_exclusive(login_id)
            raise ServiceError("ACCOUNT_BUSY", account_busy, 409)
        with self._lock:
            self._prune()
            self._sessions[login_id] = session
            self._active_login_id = login_id
        try:
            suffix = self.runner._scope_suffix(session.engine_account_id)
            sink_name = (
                f"login-{platform}{suffix}-{login_id}.log"
                if session.engine_account_id
                else f"login-{platform}-{login_id}.log"
            )
            session.sink = LogSink(
                self.cfg.logs_dir / sink_name,
                scope=f"login-{platform}{suffix}",
            )
            self._start_timer(session)
            session.process = self.runner.start_login(
                platform,
                session.sink,
                lambda evt: self._handle_event(session, evt),
                account_id=session.engine_account_id,
                profile_template=session.profile_template,
            )
            # 监听进程退出：兜底「引擎崩溃/被杀但没发 engine.finished」的会话状态，
            # 并在进程真正退出后才释放互斥（避免 confirmed 后浏览器尚未关闭时被 run 抢用 profile）
            threading.Thread(
                target=self._watch_process,
                args=(session, session.process),
                name=f"login-watch-{login_id}",
                daemon=True,
            ).start()
        except EngineStartError as exc:
            self._finish(
                session,
                status="failed",
                message=f"引擎启动失败：{exc}",
                terminate_process=False,
            )
        except Exception as exc:  # noqa: BLE001 - 任何启动异常都要落到会话状态中
            log.exception("登录引擎启动异常")
            self._finish(
                session,
                status="failed",
                message=f"引擎启动异常：{exc}",
                terminate_process=False,
            )
        # 契约要求立即返回 {login_id, status:"starting"}
        return {"login_id": login_id, "status": "starting"}

    def get(self, platform: str, login_id: str) -> Dict[str, Any]:
        session = self._get_session(platform, login_id)
        return session.to_api()

    def cancel(self, platform: str, login_id: str) -> Dict[str, Any]:
        session = self._get_session(platform, login_id)
        with session.lock:
            already_terminal = session.status in LOGIN_TERMINAL_STATUSES
        if not already_terminal:
            self._finish(session, status="cancelled", message="已取消登录")
        proc = session.process
        if proc is not None and proc.is_running():
            proc.terminate()
        # 有头登录会拉起可见 Edge；引擎树被杀后仍可能残留浏览器进程，这里按 profile 兜底清理
        self._cleanup_browsers(session)
        self._write_session_log(session, "取消登录：已终止引擎进程树并清理该平台浏览器进程")
        return {"ok": True}

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.is_terminal():
                continue
            try:
                self._finish(session, status="cancelled", message="sidecar 关闭，登录已取消")
                if session.process is not None:
                    session.process.terminate()
                self._cleanup_browsers(session)
            except Exception:  # noqa: BLE001
                pass

    # -- 内部实现 ---------------------------------------------------------
    def _get_session(self, platform: str, login_id: str) -> LoginSession:
        with self._lock:
            session = self._sessions.get(login_id)
        if session is None or session.platform != platform:
            raise ServiceError("LOGIN_NOT_FOUND", f"登录会话不存在：{login_id}", 404)
        return session

    def _prune(self) -> None:
        """保留最近 50 个会话，避免内存无限增长（调用方已持锁）。"""
        if len(self._sessions) <= 50:
            return
        for login_id in list(self._sessions.keys()):
            session = self._sessions[login_id]
            if session.is_terminal() and login_id != self._active_login_id:
                del self._sessions[login_id]
                timer = self._timers.pop(login_id, None)
                if timer is not None:
                    timer.cancel()
                release_timer = self._release_timers.pop(login_id, None)
                if release_timer is not None:
                    release_timer.cancel()
                if session.sink is not None:
                    session.sink.close()
            if len(self._sessions) <= 50:
                break

    def _cleanup_browsers(self, session: LoginSession) -> List[int]:
        """杀掉该账号 profile 残留的 msedge/chrome（有头登录取消/崩溃后的兜底）。"""
        try:
            killed = self.runner.kill_orphan_browsers(
                session.platform,
                account_id=session.engine_account_id,
                profile_template=session.profile_template,
            )
        except Exception:  # noqa: BLE001
            log.exception("清理残留浏览器进程失败：%s", session.platform)
            return []
        if killed:
            self._write_session_log(
                session,
                f"已清理残留浏览器进程 {len(killed)} 个（platform={session.platform} "
                f"account={session.account_id or '-'}）：{killed[:12]}",
            )
        return killed

    def _start_timer(self, session: LoginSession) -> None:
        # 有头登录允许用户在浏览器里完成短信/滑块验证，engine 自身 --timeout 已放宽；
        # 这里再加宽限，避免 sidecar 比 engine 先放弃。
        delay = float(self.cfg.login_timeout) + float(LOGIN_WATCHDOG_GRACE)
        timer = threading.Timer(delay, self._on_watchdog, args=(session.login_id,))
        timer.daemon = True
        with self._lock:
            self._timers[session.login_id] = timer
        timer.start()

    def _on_watchdog(self, login_id: str) -> None:
        with self._lock:
            session = self._sessions.get(login_id)
        if session is None or session.is_terminal():
            return
        self._finish(session, status="failed", message="登录超时：引擎未返回结果")
        if session.process is not None:
            session.process.terminate()
        self._cleanup_browsers(session)

    def _watch_process(self, session: LoginSession, proc: EngineProcess) -> None:
        """登录引擎进程退出后的兜底：补终态 + 清理浏览器 + 释放互斥（幂等）。"""
        try:
            proc.wait()
        except Exception:  # noqa: BLE001
            pass
        try:
            # 引擎退出（正常/崩溃）后清理可能残留的可见浏览器窗口
            self._cleanup_browsers(session)
            with session.lock:
                terminal = session.status in LOGIN_TERMINAL_STATUSES
                current = session.status
            if not terminal:
                self._finish(
                    session,
                    status="failed",
                    message=f"登录引擎异常退出（退出码 {proc.returncode}），未返回结果",
                )
            elif current == "confirmed":
                self._write_session_log(session, "登录引擎已退出，浏览器/profile 已释放")
        finally:
            self._release(session)

    def _schedule_confirm_release(self, session: LoginSession, delay: float = 15.0) -> None:
        """confirmed 后若引擎迟迟不退出（浏览器收尾卡住），超时强制释放互斥并杀进程。"""
        timer = threading.Timer(delay, self._force_release_confirmed, args=(session.login_id,))
        timer.daemon = True
        with self._lock:
            old = self._release_timers.pop(session.login_id, None)
            if old is not None:
                old.cancel()
            self._release_timers[session.login_id] = timer
        timer.start()

    def _force_release_confirmed(self, login_id: str) -> None:
        with self._lock:
            session = self._sessions.get(login_id)
            self._release_timers.pop(login_id, None)
        if session is None:
            return
        self._write_session_log(session, "confirmed 后引擎退出超时，强制释放互斥并终止引擎进程")
        proc = session.process
        if proc is not None and proc.is_running():
            proc.terminate()
        self._cleanup_browsers(session)
        self._release(session)

    def _cancel_timer(self, session: LoginSession) -> None:
        with self._lock:
            timer = self._timers.pop(session.login_id, None)
        if timer is not None:
            timer.cancel()

    def _cancel_release_timer(self, session: LoginSession) -> None:
        with self._lock:
            timer = self._release_timers.pop(session.login_id, None)
        if timer is not None:
            timer.cancel()

    def _release(self, session: LoginSession) -> None:
        with session.lock:
            if session.released:
                return
            session.released = True
        self.guard.release_exclusive(session.login_id)
        if session.account_id:
            self.guard.release_account(session.account_id, session.login_id)
        self._cancel_timer(session)
        self._cancel_release_timer(session)

    def _finish(
        self,
        session: LoginSession,
        status: str,
        message: str,
        terminate_process: bool = False,
        account_invalid: bool = False,
    ) -> None:
        with session.lock:
            if session.status in LOGIN_TERMINAL_STATUSES:
                # 已终态：只补 message（例如 confirmed 后 engine.finished 带 info）
                if message and not session.message:
                    session.message = message
                return
            session.status = status
            session.message = message
            session.finished_at = now_ts()
            if session.need_manual:
                session.need_manual["active"] = False
        # 登录失败/过期：同步账号真源（need_login，避免下次被自动选中）
        if account_invalid and session.account_id and self.accounts is not None:
            try:
                self.accounts.on_login_failed(session.account_id, message or status)
            except Exception:  # noqa: BLE001
                log.exception("写入账号登录失败状态失败：%s", session.account_id)
        # confirmed 时浏览器/profile 可能还在收尾：互斥保留到引擎进程真正退出（_watch_process 释放）；
        # 另加 15s 安全兜底，防止引擎退出卡死导致后续 login/run 一直 BUSY。
        process_still_running = (
            status == "confirmed"
            and session.process is not None
            and session.process.is_running()
        )
        if process_still_running:
            self._cancel_timer(session)
            self._schedule_confirm_release(session)
        else:
            self._release(session)
        if terminate_process and session.process is not None:
            session.process.terminate()
        self._write_session_log(session, f"登录会话结束：{status} {message}")
        with self._lock:
            if self._active_login_id == session.login_id:
                self._active_login_id = None

    def _write_session_log(self, session: LoginSession, text: str) -> None:
        if session.sink is not None:
            session.sink.append(f"[sidecar] {text}")

    # -- 事件处理 ---------------------------------------------------------
    def _apply_need_manual(self, session: LoginSession, event: Dict[str, Any]) -> None:
        """处理 engine 的 need_manual（滑块/拼图/短信验证码）：状态置 need_manual 并保留信息。

        该状态不是失败：engine 会在用户于可见浏览器窗口完成验证后继续登录流程，
        期间可能周期发送 waiting_scan；本状态机保持 need_manual 粘性，直到终态。
        """
        kind = infer_need_manual_kind(event)
        message = str(event.get("message") or "").strip() or default_need_manual_message(kind)
        prev_kind = ""
        prev_status = ""
        with session.lock:
            if session.status in LOGIN_TERMINAL_STATUSES:
                return
            prev_kind = session.kind
            prev_status = session.status
            session.status = "need_manual"
            session.kind = kind
            session.message = message
            session.need_manual_count += 1
            session.need_manual = {
                "kind": kind,
                "message": message,
                "ts": now_ts(),
                "active": True,
                "count": session.need_manual_count,
            }
        if prev_status != "need_manual" or prev_kind != kind:
            self._write_session_log(
                session,
                f"平台需要人工接管（need_manual）：kind={kind or 'unknown'} message={message}",
            )
            # 通知事件（契约 §2.1）：首次进入 / 换验证类型时发布；同 kind 重复轮询不重复发
            if self.accounts is not None:
                try:
                    self.accounts.publish_login_need_manual(
                        session.login_id,
                        session.platform,
                        session.account_id,
                        kind=kind,
                        message=message,
                    )
                except Exception:  # noqa: BLE001 - 通知失败不影响登录状态机
                    log.exception("发布 login.need_manual 事件失败：%s", session.login_id)

    def _handle_event(self, session: LoginSession, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        with session.lock:
            terminal = session.status in LOGIN_TERMINAL_STATUSES
        if terminal and event_type != "engine.finished":
            return

        # 兼容两种写法：login.status{status:need_manual} 或 login.need_manual
        if event_type == "login.need_manual":
            self._apply_need_manual(session, event)
            return

        if event_type == "login.qrcode":
            qr = {
                "seq": event.get("seq"),
                "image": event.get("image"),
                "file": event.get("file"),
            }
            with session.lock:
                session.qr = qr
                # 二维码（重新）出现说明页面已回到扫码流程：允许从 need_manual 回到 waiting_qr，
                # 但保留 need_manual 历史信息（active=False）供 UI 排查。
                if session.status not in LOGIN_TERMINAL_STATUSES:
                    session.status = "waiting_qr"
                if session.need_manual:
                    session.need_manual["active"] = False
                if not session.message:
                    session.message = "请使用手机 App 扫码登录"
            self._write_session_log(
                session,
                f"收到二维码 seq={qr.get('seq')} bytes={event.get('bytes')} file={qr.get('file')}",
            )
            return

        if event_type == "login.status":
            engine_status = str(event.get("status") or "")
            message = str(event.get("message") or "")
            mapped = _ENGINE_LOGIN_STATUS_MAP.get(engine_status)
            if mapped is None:
                self._write_session_log(session, f"忽略未知 login.status：{engine_status}")
                return
            if mapped == "need_manual":
                self._apply_need_manual(session, event)
                return
            if mapped in LOGIN_TERMINAL_STATUSES:
                self._finish(
                    session,
                    status=mapped,
                    message=message or self._default_message(mapped),
                    account_invalid=mapped in ("failed", "expired"),
                )
                if mapped == "confirmed" and session.status == "confirmed":
                    self._mark_account_confirmed(session)
            else:
                with session.lock:
                    if session.status in LOGIN_TERMINAL_STATUSES:
                        pass
                    elif session.status == "need_manual":
                        # 人工接管期间 engine 可能周期发 waiting_scan 提示：保持 need_manual 粘性；
                        # 验证完成后 engine 会发「验证已完成，继续登录…」，此时恢复正常状态。
                        if message and ("验证已完成" in message or "继续登录" in message):
                            session.status = mapped
                            session.message = message
                            if session.need_manual:
                                session.need_manual["active"] = False
                            self._write_session_log(session, f"人工验证完成，状态恢复为 {mapped}：{message}")
                        else:
                            if session.need_manual:
                                session.need_manual["active"] = True
                                session.need_manual["last_poll_ts"] = now_ts()
                    else:
                        session.status = mapped
                        if message:
                            session.message = message
                        elif not session.message:
                            session.message = self._default_message(mapped)
            return

        if event_type == "engine.finished":
            ok = bool(event.get("ok"))
            message = str(event.get("message") or "")
            with session.lock:
                current = session.status
                stop_requested = session.stop_requested
            if current in LOGIN_TERMINAL_STATUSES:
                if current == "confirmed":
                    self._mark_account_confirmed(session)
                return
            if stop_requested:
                self._finish(session, status="cancelled", message=message or "已取消登录")
            elif ok:
                self._finish(session, status="confirmed", message=message or "登录成功")
                if session.status == "confirmed":
                    self._mark_account_confirmed(session)
            else:
                low = message.lower()
                if "cancel" in low or "取消" in message:
                    self._finish(session, status="cancelled", message=message)
                elif "expire" in low or "过期" in message or "超时" in message or "timeout" in low:
                    self._finish(
                        session,
                        status="expired",
                        message=message or "二维码已过期",
                        account_invalid=True,
                    )
                else:
                    self._finish(
                        session,
                        status="failed",
                        message=message or "登录失败",
                        account_invalid=True,
                    )
            return

        # engine.started 等无状态事件：记录到日志方便排查
        self._write_session_log(session, f"事件 {event_type}: {json.dumps(event, ensure_ascii=False)[:300]}")

    @staticmethod
    def _default_message(status: str) -> str:
        return {
            "starting": "正在启动登录引擎…",
            "waiting_qr": "请使用手机 App 扫码登录",
            "waiting_scan": "已扫码，请在手机端确认",
            "need_manual": default_need_manual_message(""),
            "confirmed": "登录成功",
            "expired": "二维码已过期，请重新发起登录",
            "failed": "登录失败",
            "cancelled": "已取消登录",
        }.get(status, "")

    def _mark_account_confirmed(self, session: LoginSession) -> None:
        try:
            self.state.set_account_state(
                session.platform,
                logged_in=True,
                last_checked_at=now_ts(),
                profile_exists=True,
                message="已登录",
            )
        except Exception:  # noqa: BLE001
            log.exception("写入平台登录态失败：%s", session.platform)
        # 账号真源（accounts）：登录成功 → login_state=ok / status=active / last_login_at
        if session.account_id and self.accounts is not None:
            try:
                self.accounts.on_login_success(session.account_id)
            except Exception:  # noqa: BLE001
                log.exception("写入账号登录成功状态失败：%s", session.account_id)


# ---------------------------------------------------------------------------
# 采集编排
# ---------------------------------------------------------------------------
@dataclass
class RunRuntime:
    run_id: str
    params: Dict[str, Any]
    keywords: List[str]
    platforms: List[str]
    started_ts: int
    expected_items: int
    sink: LogSink
    resume: bool = False
    account_policy: Dict[str, Any] = field(default_factory=dict)
    resume_accounts: Dict[str, str] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[EngineProcess] = None
    process_lock: threading.RLock = field(default_factory=threading.RLock)
    results_lock: threading.RLock = field(default_factory=threading.RLock)
    platform_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    saved_counts: Dict[str, int] = field(default_factory=dict)
    finished: bool = False
    thread: Optional[threading.Thread] = None


def _is_retryable_crawl_error(message: str) -> bool:
    """判断采集失败是否为可自动重试的瞬时网络/浏览器错误。

    不重试「未登录、登录失效、扫码/验证码」等需要人工操作的错误，避免反复
    拉起浏览器打扰用户。
    """
    text = str(message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    non_retryable = (
        "未登录",
        "登录失效",
        "登录失败",
        "需要登录",
        "扫码",
        "二维码",
        "验证码",
        "滑块",
        "拼图",
        "账号异常",
        "封禁",
        "not logged",
        "login required",
        "login failed",
    )
    if any(token in text or token in lowered for token in non_retryable):
        return False
    retryable = (
        "err_connection_closed",
        "err_connection_reset",
        "err_connection_refused",
        "err_connection_aborted",
        "err_timed_out",
        "err_name_not_resolved",
        "err_network_changed",
        "err_internet_disconnected",
        "net::err_",
        "page.goto",
        "retryerror",
        "datafetcherror",
        "timeouterror",
        "timeout",
        "timed out",
        "超时",
        "econnreset",
        "econnrefused",
        "connection closed",
        "connection reset",
        "target page, context or browser has been closed",
        "browser has been closed",
        "target closed",
        "引擎未返回 engine.finished",
        "引擎启动失败",
        "引擎启动异常",
    )
    return any(token in text or token in lowered for token in retryable)


class RunManager:
    """采集任务编排：按平台串行跑 engine crawl，单平台失败继续。"""

    def __init__(
        self,
        runner: EngineRunner,
        state: StateStore,
        guard: ActivityGuard,
        cfg: SidecarConfig,
        accounts: Optional[Any] = None,
    ) -> None:
        self.runner = runner
        self.state = state
        self.guard = guard
        self.cfg = cfg
        self.accounts = accounts
        self._lock = threading.RLock()
        self._runtimes: Dict[str, RunRuntime] = {}
        self._finished_order: List[str] = []

    # -- 账号策略 ---------------------------------------------------------
    def _normalize_account_policy(self, raw: Any) -> Dict[str, Any]:
        """解析任务账号策略：``{"mode":"auto"|"manual","strategy":"lru","account_ids":{platform:id}}``。"""
        policy = raw if isinstance(raw, dict) else {}
        mode = str(policy.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "manual"):
            raise ServiceError("INVALID_PARAMS", f"account_policy.mode 不合法：{mode}", 400)
        strategy = str(policy.get("strategy") or "").strip().lower()
        if not strategy:
            strategy = self.accounts.default_strategy() if self.accounts is not None else "lru"
        if strategy not in ("lru", "best_health", "round_robin", "manual"):
            raise ServiceError("INVALID_PARAMS", f"account_policy.strategy 不合法：{strategy}", 400)
        account_ids: Dict[str, str] = {}
        raw_ids = policy.get("account_ids") or {}
        if isinstance(raw_ids, dict):
            items = list(raw_ids.items())
        elif isinstance(raw_ids, list):
            items = [
                (item.get("platform"), item.get("account_id"))
                for item in raw_ids
                if isinstance(item, dict)
            ]
        else:
            items = []
        for platform, account_id in items:
            platform_text = str(platform or "").strip()
            account_id_text = str(account_id or "").strip()
            if not platform_text or not account_id_text:
                continue
            if platform_text not in PLATFORM_LABELS:
                raise ServiceError("INVALID_PLATFORM", f"account_policy 平台不支持：{platform_text}", 400)
            account_ids[platform_text] = account_id_text
        if mode == "manual" and not account_ids:
            raise ServiceError("INVALID_PARAMS", "account_policy.mode=manual 时必须指定 account_ids", 400)
        return {"mode": mode, "strategy": strategy, "account_ids": account_ids}

    def _aliases(self) -> Dict[str, str]:
        if self.accounts is None:
            return {}
        try:
            return self.accounts.aliases()
        except Exception:  # noqa: BLE001 - 别名仅展示用
            log.exception("读取账号别名失败")
            return {}

    # -- 对外接口 ---------------------------------------------------------
    def create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        keywords: List[str] = list(params["keywords"])
        platforms: List[str] = list(params["platforms"])
        account_policy = self._normalize_account_policy(params.get("account_policy"))
        run_id = "run_" + uuid.uuid4().hex[:12]
        busy = self.guard.acquire_exclusive("run", run_id)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        sink: Optional[LogSink] = None
        started_ts = now_ts()
        expected_items = max(
            1,
            _to_int(params.get("max_notes"), 0) * max(1, len(keywords)),
        )
        rt: Optional[RunRuntime] = None
        try:
            sink = LogSink(self.cfg.logs_dir / f"run-{run_id}.log", scope=f"run-{run_id}")
            rt = RunRuntime(
                run_id=run_id,
                params=dict(params),
                keywords=keywords,
                platforms=platforms,
                started_ts=started_ts,
                expected_items=expected_items,
                sink=sink,
                account_policy=account_policy,
            )
            self.state.create_run(
                run_id=run_id,
                params=dict(params),
                keywords=keywords,
                platforms=platforms,
                started_ts=started_ts,
                name=str(params.get("name") or ""),
                account_policy=account_policy,
            )
        except Exception:
            if sink is not None:
                sink.close()
            self.guard.release_exclusive(run_id)
            raise
        assert rt is not None and sink is not None
        with self._lock:
            self._prune_finished()
            self._runtimes[run_id] = rt
        sink.append(
            f"[sidecar] 创建采集任务 run_id={run_id} platforms={','.join(platforms)} "
            f"keywords={','.join(keywords)} max_notes={params.get('max_notes')} "
            f"account_policy={json.dumps(account_policy, ensure_ascii=False)}"
        )
        thread = threading.Thread(target=self._worker, args=(rt,), name=f"run-{run_id}", daemon=True)
        rt.thread = thread
        try:
            thread.start()
        except Exception as exc:  # noqa: BLE001 - 线程创建失败也要释放互斥并标记失败
            rt.finished = True
            try:
                self.state.update_run(
                    run_id, status=RUN_STATUS_FAILED, finished_at=now_ts(), error=str(exc)
                )
            except Exception:  # noqa: BLE001
                pass
            sink.close()
            self.guard.release_exclusive(run_id)
            raise ServiceError("RUN_START_FAILED", f"采集线程启动失败：{exc}", 500) from exc
        return {"run_id": run_id, "status": "running"}

    def resume(self, run_id: str, platforms: Optional[List[str]] = None) -> Dict[str, Any]:
        """断点续采：复用同一 run_id / started_ts，只重跑未完成（或显式指定）的平台。"""
        run = self.state.get_run(run_id)
        if run is None:
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        if run["status"] == RUN_STATUS_RUNNING:
            raise ServiceError("RUN_BUSY", "任务仍在运行，无法继续采集", 409)
        with self._lock:
            old_rt = self._runtimes.get(run_id)
        if old_rt is not None and not old_rt.finished:
            raise ServiceError("RUN_BUSY", "任务仍在运行，无法继续采集", 409)

        run_platforms = list(run["platforms"])
        by_platform = {item["platform"]: item for item in run_platforms}
        order = {item["platform"]: int(item.get("seq") or 0) for item in run_platforms}

        selected: List[str] = []
        if platforms:
            seen: set = set()
            for raw in platforms:
                platform = str(raw or "").strip()
                if not platform or platform in seen:
                    continue
                if platform not in by_platform:
                    raise ServiceError(
                        "INVALID_PLATFORM",
                        f"平台 {platform} 不在该任务中（可选：{','.join(by_platform)}）",
                        422,
                    )
                seen.add(platform)
                selected.append(platform)
        if not selected:
            # 默认：只续跑未完成平台（queued/failed/skipped）
            selected = [
                item["platform"]
                for item in run_platforms
                if item["status"] in ("queued", "failed", "skipped")
            ]
            if not selected:
                raise ServiceError(
                    "NO_PENDING_PLATFORMS",
                    "该任务所有平台均已完成，无需继续采集",
                    409,
                )
        selected.sort(key=lambda platform: order.get(platform, 0))

        busy = self.guard.acquire_exclusive("run", run_id)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        sink: Optional[LogSink] = None
        rt: Optional[RunRuntime] = None
        try:
            fresh = self.state.get_run(run_id)
            if fresh is None:
                raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
            if fresh["status"] == RUN_STATUS_RUNNING:
                raise ServiceError("RUN_BUSY", "任务仍在运行，无法继续采集", 409)

            baselines = {
                platform: _to_int(by_platform[platform].get("saved_items"), 0)
                for platform in selected
            }
            expected_items = max(
                1,
                _to_int(fresh["params"].get("max_notes"), 0) * max(1, len(fresh["keywords"])),
            )
            for platform in selected:
                baseline = baselines[platform]
                progress = (
                    max(1, min(99, int(baseline * 100 / expected_items))) if baseline else 0
                )
                self.state.update_platform(
                    run_id,
                    platform,
                    status="queued",
                    message="等待继续采集",
                    stage="",
                    progress=progress,
                    saved_items=baseline,
                    started_at=None,
                    finished_at=None,
                )
            self.state.reset_run_for_resume(run_id)

            sink = LogSink(self.cfg.logs_dir / f"run-{run_id}.log", scope=f"run-{run_id}")
            resume_accounts = (
                self.accounts.last_attempt_accounts(run_id) if self.accounts is not None else {}
            )
            resume_policy = fresh.get("account_policy") or self._normalize_account_policy(
                fresh["params"].get("account_policy")
            )
            rt = RunRuntime(
                run_id=run_id,
                params=dict(fresh["params"]),
                keywords=list(fresh["keywords"]),
                platforms=list(selected),
                started_ts=int(fresh["started_ts"]),
                expected_items=expected_items,
                sink=sink,
                resume=True,
                account_policy=dict(resume_policy or {}),
                resume_accounts={
                    platform: account_id
                    for platform, account_id in (resume_accounts or {}).items()
                    if platform in selected
                },
            )
            with rt.results_lock:
                rt.saved_counts = dict(baselines)
            with self._lock:
                old = self._runtimes.get(run_id)
                if old is not None and old.finished:
                    old.sink.close()
                if run_id in self._finished_order:
                    self._finished_order.remove(run_id)
                self._prune_finished()
                self._runtimes[run_id] = rt
            sink.append(
                f"[sidecar] 断点续采 run_id={run_id} platforms={','.join(selected)} "
                f"started_ts={fresh['started_ts']}（复用原值）account_policy={json.dumps(rt.account_policy, ensure_ascii=False)}"
            )
            for platform in selected:
                sink.append(
                    f"[sidecar] 续采平台 {platform}：saved_items 基线={baselines[platform]} "
                    f"优先沿用账号={rt.resume_accounts.get(platform) or '自动选号'}"
                )
            thread = threading.Thread(
                target=self._worker, args=(rt,), name=f"run-{run_id}-resume", daemon=True
            )
            rt.thread = thread
            try:
                thread.start()
            except Exception as exc:  # noqa: BLE001
                rt.finished = True
                self.guard.release_exclusive(run_id)
                sink.close()
                try:
                    self.state.update_run(
                        run_id, status=RUN_STATUS_FAILED, finished_at=now_ts(), error=str(exc)
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise ServiceError("RUN_START_FAILED", f"续采线程启动失败：{exc}", 500) from exc
        except BaseException:
            if sink is not None and (rt is None or rt.thread is None):
                sink.close()
            self.guard.release_exclusive(run_id)
            raise
        return {"run_id": run_id, "status": "running", "resumed_platforms": selected}

    def stop(self, run_id: str) -> Dict[str, Any]:
        if not self.state.run_exists(run_id):
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        with self._lock:
            rt = self._runtimes.get(run_id)
        if rt is None or rt.finished:
            # 已结束：幂等返回 ok（UI 可能停止按钮点晚）
            return {"ok": True}
        rt.stop_event.set()
        with rt.process_lock:
            proc = rt.process
        if proc is not None and proc.is_running():
            proc.terminate()
        rt.sink.append("[sidecar] 收到停止请求，正在终止引擎…")
        return {"ok": True}

    def get_run(self, run_id: str) -> Dict[str, Any]:
        run = self.state.get_run(run_id)
        if run is None:
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        return _run_to_api(run, self._aliases())

    def get_run_raw(self, run_id: str) -> Dict[str, Any]:
        run = self.state.get_run(run_id)
        if run is None:
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        return run

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        runs = self.state.list_runs(limit=limit)
        aliases = self._aliases()
        return [_run_to_summary(run, aliases) for run in runs]

    def logs(self, run_id: str, tail: int = 200) -> List[Dict[str, Any]]:
        if not self.state.run_exists(run_id):
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        with self._lock:
            rt = self._runtimes.get(run_id)
        if rt is not None:
            return rt.sink.tail(tail)
        return read_log_file_tail(self.cfg.logs_dir / f"run-{run_id}.log", tail)

    def is_running(self, run_id: str) -> bool:
        run = self.state.get_run(run_id)
        return bool(run and run["status"] == RUN_STATUS_RUNNING)

    def shutdown(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            if rt.finished:
                continue
            rt.stop_event.set()
            with rt.process_lock:
                proc = rt.process
            if proc is not None and proc.is_running():
                proc.terminate()
        deadline = time.monotonic() + 8.0
        for rt in runtimes:
            if rt.thread is not None:
                rt.thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for rt in runtimes:
            if not rt.finished:
                try:
                    self.state.update_run(
                        rt.run_id, status=RUN_STATUS_CANCELLED, finished_at=now_ts()
                    )
                except Exception:  # noqa: BLE001
                    pass
        for rt in runtimes:
            rt.sink.close()

    # -- 编排实现 ---------------------------------------------------------
    def _prune_finished(self) -> None:
        """内存中最多保留 20 个已结束 runtime（更多历史从日志文件读取）。"""
        while len(self._finished_order) > 20:
            run_id = self._finished_order.pop(0)
            rt = self._runtimes.pop(run_id, None)
            if rt is not None:
                rt.sink.close()

    def _worker(self, rt: RunRuntime) -> None:
        final_status = RUN_STATUS_DONE
        current_platform: Optional[str] = None
        try:
            for platform in rt.platforms:
                current_platform = platform
                if rt.stop_event.is_set():
                    self.state.update_platform(
                        rt.run_id,
                        platform,
                        status="skipped",
                        message="已取消，未执行",
                        finished_at=now_ts(),
                    )
                    continue
                self._run_one_platform(rt, platform)
                if rt.stop_event.is_set():
                    # 后续平台在下一轮循环开头被标记 skipped
                    continue
            run_error: Optional[str] = None
            if rt.stop_event.is_set():
                final_status = RUN_STATUS_CANCELLED
            elif rt.resume:
                # 续采结束按「全 run 所有平台是否都 done」判定：全 done 才 done，否则 failed 便于再次续采
                current = self.state.get_run(rt.run_id) or {"platforms": []}
                all_done = bool(current["platforms"]) and all(
                    item["status"] == "done" for item in current["platforms"]
                )
                final_status = RUN_STATUS_DONE if all_done else RUN_STATUS_FAILED
                run_error = "" if all_done else "部分平台失败"
                rt.sink.append(
                    f"[sidecar] 续采结束：全 run 平台 "
                    + ",".join(
                        f"{item['platform']}={item['status']}" for item in current["platforms"]
                    )
                    + f" → {final_status}"
                )
            else:
                final_status = RUN_STATUS_DONE
            self.state.update_run(
                rt.run_id, status=final_status, finished_at=now_ts(), error=run_error
            )
        except Exception as exc:  # noqa: BLE001 - 编排层异常不能拖垮 sidecar
            log.exception("采集任务 %s 编排异常", rt.run_id)
            final_status = RUN_STATUS_FAILED
            try:
                if current_platform is not None:
                    self.state.update_platform(
                        rt.run_id,
                        current_platform,
                        status="failed",
                        message=f"编排异常：{exc}",
                        finished_at=now_ts(),
                    )
                self.state.update_run(
                    rt.run_id, status=RUN_STATUS_FAILED, finished_at=now_ts(), error=str(exc)
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            rt.finished = True
            rt.sink.append(f"===== 采集任务 {rt.run_id} 结束：{final_status} =====")
            with rt.process_lock:
                rt.process = None
            self.guard.release_exclusive(rt.run_id)
            self._publish_run_event(rt, final_status)
            with self._lock:
                self._finished_order.append(rt.run_id)

    def _publish_run_event(self, rt: RunRuntime, final_status: str) -> None:
        """发布 run.finished / run.failed / run.partial 事件（契约 §2.1）。"""
        try:
            run = self.state.get_run(rt.run_id) or {}
            platforms = run.get("platforms") or []
            ok_count = sum(1 for item in platforms if item.get("status") == "done")
            failed_count = sum(1 for item in platforms if item.get("status") == "failed")
            skipped_count = sum(
                1 for item in platforms if item.get("status") in ("skipped", "queued")
            )
            saved_items = sum(_to_int(item.get("saved_items"), 0) for item in platforms)
            duration_s = max(0, now_ts() - int(rt.started_ts))
            platform = ""
            account_id = ""
            for item in reversed(platforms):
                if item.get("account_id"):
                    platform = str(item.get("platform") or "")
                    account_id = str(item.get("account_id") or "")
                    break
            account_alias = ""
            if account_id and self.accounts is not None:
                try:
                    account_alias = self.accounts.alias_of(account_id)
                except Exception:  # noqa: BLE001 - 别名仅展示用
                    account_alias = ""
            name = str(rt.params.get("name") or "")
            if final_status == RUN_STATUS_DONE:
                event_type, severity, dedupe_key = "run.finished", "info", f"run:{rt.run_id}:finished"
                title = f"采集完成：{name or rt.run_id}"
            elif final_status == RUN_STATUS_FAILED:
                event_type, severity, dedupe_key = "run.failed", "error", f"run:{rt.run_id}:failed"
                title = f"采集失败：{name or rt.run_id}"
            else:
                event_type, severity, dedupe_key = "run.partial", "warn", f"run:{rt.run_id}:partial"
                title = f"采集部分完成：{name or rt.run_id}"
            parts = [
                f"平台 {ok_count}/{len(platforms)} 成功",
                f"入库 {saved_items} 条",
                f"耗时 {duration_s} 秒",
            ]
            if failed_count:
                parts.append(f"{failed_count} 个平台失败")
            if skipped_count:
                parts.append(f"{skipped_count} 个平台未执行")
            message = "，".join(parts)
            error = str(run.get("error") or "")
            if error and final_status == RUN_STATUS_FAILED:
                message += f"（{error[:200]}）"
            event = {
                "type": event_type,
                "severity": severity,
                "dedupe_key": dedupe_key,
                "ts": now_ts(),
                "data": {
                    "run_id": rt.run_id,
                    "name": name,
                    "keywords_count": len(rt.keywords),
                    "platforms_total": len(platforms),
                    "platforms_ok": ok_count,
                    "platforms_failed": failed_count,
                    "platforms_skipped": skipped_count,
                    "saved_items": saved_items,
                    "duration_s": duration_s,
                    "error": error,
                    "platform": platform,
                    "account_id": account_id,
                    "account_alias": account_alias,
                    "report_id": "",
                    "version": 0,
                    "title": title,
                    "message": message,
                },
            }
            bus.publish(event)
            rt.sink.append(f"[sidecar] 已发布事件 {event_type}（dedupe_key={dedupe_key}）")
        except Exception:  # noqa: BLE001 - 通知事件失败不能影响采集任务收尾
            log.exception("发布 run 事件失败：%s", rt.run_id)

    def _run_platform_with_retries(
        self,
        rt: RunRuntime,
        platform: str,
        engine_account_id: str = "",
        profile_template: str = "",
        account_attempt: int = 1,
    ) -> None:
        """同一账号内执行一个平台；网络类瞬时失败自动重试（无需用户手动点）。

        自动重试只针对网络/浏览器连接类错误（ERR_CONNECTION_CLOSED、
        Page.goto、RetryError/DataFetchError、超时等），不重试未登录、
        扫码失败等需要人工处理的错误（账号类失败由 ``_run_one_platform`` 换号）。
        次数/间隔可用环境变量调整：
          SIDECAR_AUTO_RETRY_MAX   默认 2（首次失败后再试 2 次）
          SIDECAR_AUTO_RETRY_DELAY 默认 5 秒（之后 10s、20s...）
        """
        try:
            max_retries = int(os.environ.get("SIDECAR_AUTO_RETRY_MAX", "2"))
        except (TypeError, ValueError):
            max_retries = 2
        max_retries = max(0, min(max_retries, 5))
        try:
            base_delay = float(os.environ.get("SIDECAR_AUTO_RETRY_DELAY", "5"))
        except (TypeError, ValueError):
            base_delay = 5.0
        base_delay = max(1.0, min(base_delay, 30.0))

        attempt = 0
        while True:
            self._run_one_platform_attempt(
                rt, platform, engine_account_id, profile_template, account_attempt
            )
            if rt.stop_event.is_set():
                return
            row = self._platform_row(rt.run_id, platform)
            if row is None or row.get("status") != "failed":
                return
            message = str(row.get("message") or "")
            if attempt >= max_retries or not _is_retryable_crawl_error(message):
                return
            attempt += 1
            delay = base_delay * (2 ** (attempt - 1))
            label = PLATFORM_LABELS.get(platform, platform)
            retry_message = (
                f"网络波动，{int(delay)} 秒后自动重试（{attempt}/{max_retries}）：{message[:180]}"
            )
            self.state.update_platform(
                rt.run_id,
                platform,
                status="running",
                progress=_to_int(row.get("progress"), 0),
                stage="retry",
                saved_items=_to_int(row.get("saved_items"), 0),
                message=retry_message,
                finished_at=None,
            )
            rt.sink.append("")
            rt.sink.append(f"===== [{label}] 自动重试 {attempt}/{max_retries}，{int(delay)} 秒后重新采集 =====")
            rt.sink.append(f"      原因：{message[:500]}")
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if rt.stop_event.wait(0.5):
                    return

    # -- 选号 / 换号 ------------------------------------------------------
    def _select_account(
        self,
        rt: RunRuntime,
        platform: str,
        attempt: int,
        tried: List[str],
    ) -> Dict[str, Any]:
        """为一个平台选号：手动策略/续采优先 → 自动选号 → 旧单账号兼容兜底。"""
        assert self.accounts is not None
        policy = rt.account_policy or {}
        mode = str(policy.get("mode") or "auto")
        strategy = str(policy.get("strategy") or self.accounts.default_strategy())
        manual_ids = policy.get("account_ids") or {}
        if attempt == 1 and rt.resume_accounts.get(platform):
            resume_id = str(rt.resume_accounts.get(platform))
            try:
                selected = self.accounts.pick_manual(platform, resume_id)
                selected["strategy"] = "resume"
                selected["reasons"] = ["续采：沿用上次采集账号"] + list(selected.get("reasons") or [])
                rt.sink.append(f"[sidecar] 续采：优先沿用原账号 {resume_id}")
                return selected
            except ServiceError as exc:
                rt.sink.append(f"[sidecar] 续采原账号 {resume_id} 不可用（{exc.message}），重新选号")
        if mode == "manual":
            account_id = str(manual_ids.get(platform) or "")
            if not account_id:
                raise ServiceError(
                    "NO_ACCOUNT", f"平台 {PLATFORM_LABELS.get(platform, platform)} 手动模式未指定账号", 409
                )
            selected = self.accounts.pick_manual(platform, account_id)
            selected["strategy"] = "manual"
            return selected
        try:
            return self.accounts.pick(platform, strategy=strategy, exclude=tried)
        except ServiceError as exc:
            if exc.code != "NO_ACCOUNT" or tried:
                raise
            fallback = self.accounts.legacy_fallback(platform, exclude=tried)
            if fallback is None:
                raise
            rt.sink.append(
                "[sidecar] 旧单账号兼容：legacy 默认账号登录态未确认，沿用旧行为继续采集"
            )
            return fallback

    def _run_one_platform(self, rt: RunRuntime, platform: str) -> None:
        """执行一个平台：选号 → 账号独占锁 → 采集（网络类同号重试）→ 失败分类换号。"""
        if self.accounts is None:
            # 兼容/单元测试未注入账号模块：沿用旧单账号编排
            self._run_platform_with_retries(rt, platform, "", "", 1)
            return
        try:
            max_attempts = int(os.environ.get("SIDECAR_ACCOUNT_MAX_ATTEMPTS", "2"))
        except (TypeError, ValueError):
            max_attempts = 2
        max_attempts = max(1, min(max_attempts, 5))
        tried: List[str] = []
        failover_aliases: List[str] = []
        last_alias = ""
        label = PLATFORM_LABELS.get(platform, platform)
        # 续采/同 run 重跑时 attempt 序号从已有最大值继续（避免覆盖 run_accounts 历史）
        attempt_base = self.accounts.max_attempt(rt.run_id, platform)
        for attempt in range(1, max_attempts + 1):
            if rt.stop_event.is_set():
                return
            attempt_no = attempt_base + attempt
            try:
                selected = self._select_account(rt, platform, attempt, tried)
            except ServiceError as exc:
                if not tried:
                    message = (
                        f"无可用账号：{exc.message}"
                        if exc.code == "NO_ACCOUNT"
                        else f"选号失败：{exc.message}"
                    )
                    self.state.update_platform(
                        rt.run_id,
                        platform,
                        status="failed",
                        message=message[:1000],
                        finished_at=now_ts(),
                    )
                    rt.sink.append(f"===== [{label}] {message} =====")
                break
            account = selected.get("account") or {}
            account_id = str(selected.get("account_id") or account.get("account_id") or "")
            alias = str(
                selected.get("alias") or account.get("display_alias") or account.get("alias") or account_id
            )
            last_alias = alias
            pick_reason = str(selected.get("strategy") or "auto")
            engine_args = self.accounts.engine_account_args(account)
            account_busy = (
                self.guard.acquire_account(account_id, rt.run_id) if account_id else None
            )
            if account_busy:
                message = f"账号 {alias} 正在被占用：{account_busy}"
                rt.sink.append(f"===== [{label}] {message} =====")
                if not tried:
                    self.state.update_platform(
                        rt.run_id,
                        platform,
                        status="failed",
                        message=message,
                        finished_at=now_ts(),
                    )
                break
            try:
                self.accounts.mark_attempt(
                    rt.run_id, platform, attempt_no, account_id, reason=pick_reason
                )
                rt.sink.append(
                    f"===== [{label}] 第 {attempt_no} 次尝试：账号 {alias}"
                    f"（{account_id or '-'}，原因={pick_reason}）====="
                )
                self._run_platform_with_retries(
                    rt,
                    platform,
                    str(engine_args.get("account_id") or ""),
                    str(engine_args.get("profile_template") or ""),
                    attempt_no,
                )
                row = self._platform_row(rt.run_id, platform) or {}
                status = str(row.get("status") or "failed")
                message = str(row.get("message") or "")
                saved = _to_int(row.get("saved_items"), 0)
                result = self.accounts.on_crawl_result(
                    account_id,
                    ok=(status == "done"),
                    message=message,
                    saved_items=saved if status == "done" else 0,
                    platform=platform,
                    run_id=rt.run_id,
                )
                kind = str(result.get("kind") or "")
                finish_status = "done" if status == "done" else (
                    kind if kind in ("login_invalid", "risk_blocked") else "failed"
                )
                self.accounts.finish_attempt(
                    rt.run_id, platform, attempt_no, finish_status, message, account_id
                )
                if status == "done":
                    break
                if kind in ("login_invalid", "risk_blocked") and attempt < max_attempts:
                    tried.append(account_id)
                    failover_aliases.append(alias)
                    rt.sink.append(
                        f"[sidecar] 账号 {alias} 失败（{kind}: {message[:120]}）→ 自动切换下一个账号"
                    )
                    continue
                break
            finally:
                if account_id:
                    self.guard.release_account(account_id, rt.run_id)
        if failover_aliases and not rt.stop_event.is_set():
            row = self._platform_row(rt.run_id, platform) or {}
            note = "已自动切换账号：" + "、".join(failover_aliases) + f" → {last_alias}"
            if row.get("status") in ("failed", "done"):
                merged = (str(row.get("message") or "") + f"（{note}）")[:1000]
                self.state.update_platform(rt.run_id, platform, message=merged)
                rt.sink.append(f"===== [{label}] {note} =====")

    def _platform_row(self, run_id: str, platform: str) -> Optional[Dict[str, Any]]:
        run = self.state.get_run(run_id) or {}
        for item in run.get("platforms") or []:
            if item.get("platform") == platform:
                return item
        return None

    def _run_one_platform_attempt(
        self,
        rt: RunRuntime,
        platform: str,
        engine_account_id: str = "",
        profile_template: str = "",
        account_attempt: int = 1,
    ) -> None:
        label = PLATFORM_LABELS.get(platform, platform)
        with rt.results_lock:
            baseline = _to_int(rt.saved_counts.get(platform, 0), 0)
        start_progress = (
            _calc_progress(baseline, rt.expected_items, "boot") if baseline else 0
        )
        start_message = "正在启动采集引擎…"
        if rt.resume and baseline:
            start_message = f"正在启动采集引擎…（续采，已保留历史 {baseline} 条）"
        if engine_account_id:
            start_message += f"（账号 {engine_account_id}，第 {account_attempt} 次尝试）"
        self.state.update_platform(
            rt.run_id,
            platform,
            status="running",
            progress=start_progress,
            stage="boot",
            saved_items=baseline,
            message=start_message,
            started_at=now_ts(),
        )
        with rt.results_lock:
            rt.platform_results.pop(platform, None)
        rt.sink.append("")
        rt.sink.append(
            f"===== [{label}] 开始采集 platform={platform} "
            f"account={engine_account_id or 'legacy'} attempt={account_attempt} ====="
        )
        try:
            proc = self.runner.start_crawl(
                platform=platform,
                keywords=rt.keywords,
                max_notes=_to_int(rt.params.get("max_notes"), 15),
                comments=bool(rt.params.get("comments", True)),
                max_comments=_to_int(rt.params.get("max_comments"), 20),
                sink=rt.sink,
                on_event=lambda evt: self._on_crawl_event(rt, platform, evt),
                account_id=engine_account_id,
                profile_template=profile_template,
            )
        except EngineStartError as exc:
            self.state.update_platform(
                rt.run_id,
                platform,
                status="failed",
                message=f"引擎启动失败：{exc}",
                finished_at=now_ts(),
            )
            rt.sink.append(f"===== [{label}] 引擎启动失败：{exc} =====")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("启动采集引擎异常：%s", platform)
            self.state.update_platform(
                rt.run_id,
                platform,
                status="failed",
                message=f"引擎启动异常：{exc}",
                finished_at=now_ts(),
            )
            rt.sink.append(f"===== [{label}] 引擎启动异常：{exc} =====")
            return

        with rt.process_lock:
            rt.process = proc
        timed_out = False
        try:
            deadline = time.monotonic() + float(self.cfg.crawl_timeout)
            while True:
                if proc.wait(0.2):
                    break
                if rt.stop_event.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
            if proc.is_running():
                proc.terminate()
            proc.wait(10)
        finally:
            with rt.process_lock:
                rt.process = None

        if rt.stop_event.is_set():
            self.state.update_platform(
                rt.run_id,
                platform,
                status="failed",
                message="已取消",
                finished_at=now_ts(),
            )
            rt.sink.append(f"===== [{label}] 已取消 =====")
            return

        with rt.results_lock:
            finished_event = rt.platform_results.pop(platform, None)

        if timed_out:
            status = "failed"
            message = f"采集超时（>{int(self.cfg.crawl_timeout)} 秒），已终止"
        elif finished_event is None:
            status = "failed"
            message = f"引擎未返回 engine.finished 事件（进程退出码 {proc.returncode}）"
        elif bool(finished_event.get("ok")):
            status = "done"
            message = str(finished_event.get("message") or "采集完成")
        else:
            status = "failed"
            message = str(finished_event.get("message") or "采集失败")

        fields: Dict[str, Any] = {
            "status": status,
            "message": message,
            "finished_at": now_ts(),
        }
        if status == "done":
            fields["progress"] = 100
            fields["stage"] = "done"
        self.state.update_platform(rt.run_id, platform, **fields)
        rt.sink.append(f"===== [{label}] 结束：{status} {message} =====")

    def _on_crawl_event(self, rt: RunRuntime, platform: str, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "progress":
            stage = str(event.get("stage") or "")
            saved = _to_int(event.get("saved"), 0)
            with rt.results_lock:
                prev = rt.saved_counts.get(platform, 0)
                saved = max(prev, saved)
                rt.saved_counts[platform] = saved
            progress = _calc_progress(saved, rt.expected_items, stage)
            message = str(event.get("message") or "")
            fields: Dict[str, Any] = {
                "progress": progress,
                "stage": stage,
                "saved_items": saved,
            }
            if message:
                fields["message"] = message
            self.state.update_platform(rt.run_id, platform, **fields)
            return
        if event_type == "engine.finished":
            with rt.results_lock:
                rt.platform_results[platform] = dict(event)
            return
        if event_type == "engine.started":
            message = str(event.get("message") or "引擎已启动")
            self.state.update_platform(rt.run_id, platform, message=message)
            return
        # 其它事件忽略（可能包含 login.* 等，crawl 模式不应出现）


def _run_to_api(run: Dict[str, Any], aliases: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """契约 §2.2 ``GET /api/runs/{run_id}`` 响应（字段固定 + 账号化加法字段）。"""
    alias_map = aliases or {}
    platforms: List[Dict[str, Any]] = []
    for item in run["platforms"]:
        account_id = str(item.get("account_id") or "")
        platforms.append(
            {
                "platform": item["platform"],
                "label": PLATFORM_LABELS.get(item["platform"], item["platform"]),
                "status": item["status"],
                "progress": item["progress"],
                "stage": item["stage"],
                "saved_items": item["saved_items"],
                "message": item["message"],
                "started_at": item["started_at"],
                "finished_at": item["finished_at"],
                # 账号化加法字段（旧前端忽略即可）
                "account_id": account_id,
                "account_alias": alias_map.get(account_id, ""),
                "account_attempts": int(item.get("account_attempts") or 0),
            }
        )
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "created_at": run["created_at"],
        "finished_at": run["finished_at"],
        "params": run["params"],
        "account_policy": run.get("account_policy") or {},
        "platforms": platforms,
        "excel_path": run["excel_path"],
    }


def _run_to_summary(
    run: Dict[str, Any], aliases: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """``GET /api/runs`` 的 run 摘要。

    契约未固定摘要字段；为了让 desktop renderer 能直接复用任务详情渲染逻辑，
    这里返回与 ``GET /api/runs/{run_id}`` 同构的字段（platforms 为对象数组），
    并额外补 name/keywords/total_saved 便于列表展示。
    """
    summary = _run_to_api(run, aliases)
    summary["name"] = run.get("name") or ""
    summary["keywords"] = list(run["keywords"])
    summary["total_saved"] = sum(int(item.get("saved_items") or 0) for item in run["platforms"])
    return summary


# ---------------------------------------------------------------------------
# 统一门面：main.py 只依赖 EngineService
# ---------------------------------------------------------------------------
def load_exporter() -> Tuple[Optional[Callable[..., Any]], Optional[Callable[..., Any]], str]:
    """尝试加载同目录 exporter。

    契约 §3 要求 ``from exporter import export_run_excel/preview_run``；默认模块名
    就是 ``exporter``。selftest 可用 ``SIDECAR_EXPORTER_MODULE=_fake_exporter``
    （仅测试）注入替身，生产环境不受影响。

    返回 ``(export_fn, preview_fn, error)``；未就绪时两个函数为 None 并给出原因。
    """
    import importlib

    module_name = (_env_str("SIDECAR_EXPORTER_MODULE") or "exporter").strip()
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 缺 exporter 时 sidecar 仍要能启动
        return None, None, f"无法 import {module_name}：{exc}"
    export_fn = getattr(module, "export_run_excel", None)
    preview_fn = getattr(module, "preview_run", None)
    if not callable(export_fn):
        return None, preview_fn if callable(preview_fn) else None, (
            f"{module_name}.export_run_excel 不存在或不可调用"
        )
    return export_fn, preview_fn if callable(preview_fn) else None, ""


def load_run_feed() -> Tuple[Optional[Callable[..., Any]], str]:
    """尝试加载同目录 run_feed（「采集动态」内容流数据层）。

    默认模块名 ``run_feed``；selftest/联调可用 ``SIDECAR_RUN_FEED_MODULE`` 注入替身。
    返回 ``(build_run_feed_fn, error)``；未就绪时函数为 None 并给出原因（接口降级 503）。
    """
    import importlib

    module_name = (_env_str("SIDECAR_RUN_FEED_MODULE") or "run_feed").strip()
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 内容流未就绪时 sidecar 仍要能启动
        return None, f"无法 import {module_name}：{exc}"
    build_fn = getattr(module, "build_run_feed", None)
    if not callable(build_fn):
        return None, f"{module_name}.build_run_feed 不存在或不可调用"
    return build_fn, ""


class EngineService:
    """sidecar 全部业务能力的门面（main.py 的 endpoint 只调这里）。"""

    def __init__(self, cfg: SidecarConfig) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
        self.state = StateStore(cfg.app_db)
        recovered = self.state.recover_interrupted_runs()
        if recovered:
            log.warning("已恢复 %d 个上次中断的采集任务（running -> failed）", recovered)
        self.guard = ActivityGuard()
        self.runner = EngineRunner(cfg)
        # 多账号号池（account_manager 延迟导入，避免 engine_runner ↔ account_manager 循环导入）
        from account_manager import AccountManager

        self.accounts = AccountManager(cfg, self.state, self.guard)
        self.logins = LoginManager(self.runner, self.state, self.guard, cfg, accounts=self.accounts)
        self.runs = RunManager(self.runner, self.state, self.guard, cfg, accounts=self.accounts)
        self._export_fn, self._preview_fn, self._exporter_error = load_exporter()
        self._feed_fn, self._feed_error = load_run_feed()
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    # -- config / info ----------------------------------------------------
    def config_info(self) -> Dict[str, Any]:
        return {
            "data_dir": str(self.cfg.data_dir),
            "mc_root": str(self.cfg.mc_root),
            "python": str(self.cfg.python),
            "platform_labels": dict(PLATFORM_LABELS),
            # 契约外附加字段：便于 UI/验收脚本确认登录模式与超时策略
            "version": SIDECAR_VERSION,
            "login_headless": bool(self.cfg.login_headless),
            "login_timeout": int(self.cfg.login_timeout),
        }

    def system_info(self) -> Dict[str, Any]:
        return {
            "version": SIDECAR_VERSION,
            "data_dir": str(self.cfg.data_dir),
            "logs_dir": str(self.cfg.logs_dir),
        }

    # -- accounts ---------------------------------------------------------
    def list_accounts(self) -> List[Dict[str, Any]]:
        """``GET /api/accounts``：旧字段保持不变，加法追加账号池字段（契约 §6.2）。"""
        states = self.state.get_account_states()
        accounts: List[Dict[str, Any]] = []
        for platform, label in PLATFORMS:
            row = states.get(platform) or {}
            try:
                default = self.accounts.ensure_legacy_default(platform)
            except Exception:  # noqa: BLE001 - 账号模块异常不能拖垮旧接口
                log.exception("账号池懒适配失败：%s", platform)
                default = None
            platform_accounts = self.accounts.list(platform) if default is not None else []
            if default is not None:
                profile_exists = bool(default.get("profile_exists"))
            else:
                profile_dir = self.cfg.platform_profile_dir(platform)
                try:
                    profile_exists = profile_dir.is_dir() and any(profile_dir.iterdir())
                except OSError:
                    profile_exists = profile_dir.is_dir()
            logged_in = row.get("logged_in")
            if logged_in is None and not profile_exists:
                logged_in = None
            health_values = [int(item.get("health_score") or 0) for item in platform_accounts]
            accounts.append(
                {
                    # ---- 旧字段（一个都不能少，selftest/旧 renderer 依赖）----
                    "platform": platform,
                    "label": label,
                    "logged_in": logged_in,
                    "last_checked_at": row.get("last_checked_at"),
                    "profile_exists": bool(profile_exists),
                    "message": row.get("message") or "",
                    # ---- 账号池加法字段 ----
                    "account_count": len(platform_accounts),
                    "default_account_id": (default or {}).get("account_id", ""),
                    "default_account_alias": (default or {}).get("display_alias", ""),
                    "health": (
                        int(sum(health_values) / len(health_values)) if health_values else None
                    ),
                    "accounts": platform_accounts,
                }
            )
        return accounts

    def start_login(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        return self.logins.start(platform, account_id)

    def get_login(self, platform: str, login_id: str) -> Dict[str, Any]:
        return self.logins.get(platform, login_id)

    def cancel_login(self, platform: str, login_id: str) -> Dict[str, Any]:
        return self.logins.cancel(platform, login_id)

    def _is_platform_default(self, platform: str, account_id: str) -> bool:
        try:
            default = self.accounts.ensure_legacy_default(platform)
        except Exception:  # noqa: BLE001
            return False
        return str(default.get("account_id") or "") == str(account_id or "")

    def test_connection(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        """同步测试连接（HTTP 路由用同步 def → FastAPI 线程池，可多平台/多账号并行）。

        ``account_id`` 为空 → 平台默认账号（旧路由行为完全不变）。
        """
        account = self.accounts.resolve(platform, account_id or "")
        account_id_resolved = str(account.get("account_id") or "")
        engine_args = self.accounts.engine_account_args(account)
        engine_account_id = str(engine_args.get("account_id") or "")
        profile_template = str(engine_args.get("profile_template") or "")
        ref = f"test-{platform}-{uuid.uuid4().hex[:8]}"
        # 先占 test 槽（旧语义：platform 级；v2 账号：platform@account_id），再占账号独占锁
        busy = self.guard.acquire_test(platform, ref, account_id=engine_account_id)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        account_busy = (
            self.guard.acquire_account(account_id_resolved, ref) if account_id_resolved else None
        )
        if account_busy:
            self.guard.release_test(platform, ref, account_id=engine_account_id)
            raise ServiceError("ACCOUNT_BUSY", account_busy, 409)
        started_mono = time.monotonic()
        sink: Optional[LogSink] = None
        collected: Dict[str, Optional[Dict[str, Any]]] = {"test": None, "finished": None}
        collected_lock = threading.Lock()

        def on_event(event: Dict[str, Any]) -> None:
            event_type = str(event.get("type") or "")
            if event_type == "test.result":
                with collected_lock:
                    collected["test"] = dict(event)
            elif event_type == "engine.finished":
                with collected_lock:
                    collected["finished"] = dict(event)

        proc: Optional[EngineProcess] = None
        timed_out = False
        try:
            sink_suffix = EngineRunner._scope_suffix(engine_account_id)
            sink = LogSink(
                self.cfg.logs_dir / f"test-{platform}{sink_suffix}-{now_ts()}-{ref[-8:]}.log",
                scope=f"test-{platform}{sink_suffix}",
            )
            try:
                sink.append(
                    f"[sidecar] 测试连接 platform={platform} account={account_id_resolved or '-'} ref={ref}"
                )
                proc = self.runner.start_test(
                    platform,
                    sink,
                    on_event,
                    account_id=engine_account_id,
                    profile_template=profile_template,
                )
                deadline = time.monotonic() + float(self.cfg.test_timeout)
                while time.monotonic() < deadline:
                    if proc.wait(0.1):
                        break
                if proc.is_running():
                    timed_out = True
                    proc.terminate()
                proc.wait(10)
            except EngineStartError as exc:
                sink.append(f"[sidecar] 引擎启动失败：{exc}")
                duration_ms = int((time.monotonic() - started_mono) * 1000)
                self._update_account_after_test(
                    platform, account_id_resolved, False, f"引擎启动失败：{exc}"
                )
                return {
                    "ok": False,
                    "message": f"引擎启动失败：{exc}",
                    "duration_ms": duration_ms,
                    "account_id": account_id_resolved,
                    "account_alias": account.get("display_alias") or "",
                }
            except Exception as exc:  # noqa: BLE001
                log.exception("测试连接异常")
                duration_ms = int((time.monotonic() - started_mono) * 1000)
                self._update_account_after_test(
                    platform, account_id_resolved, False, f"测试异常：{exc}"
                )
                return {
                    "ok": False,
                    "message": f"测试异常：{exc}",
                    "duration_ms": duration_ms,
                    "account_id": account_id_resolved,
                    "account_alias": account.get("display_alias") or "",
                }

            duration_ms = int((time.monotonic() - started_mono) * 1000)
            network_error = False
            with collected_lock:
                test_event = collected["test"]
                finished_event = collected["finished"]
            if test_event is not None:
                ok = bool(test_event.get("ok"))
                network_error = bool(test_event.get("network_error"))
                message = str(test_event.get("message") or ("已登录" if ok else ("网络异常，登录态未确认" if network_error else "未登录")))
                engine_duration = _to_int(test_event.get("duration_ms"), 0)
                if engine_duration > 0:
                    duration_ms = engine_duration
            elif finished_event is not None:
                ok = bool(finished_event.get("ok"))
                message = str(finished_event.get("message") or ("检测完成" if ok else "检测失败"))
            else:
                ok = False
                message = "检测超时（90 秒）" if timed_out else (
                    f"引擎异常退出且未返回 test.result（退出码 {proc.returncode if proc else '无'}）"
                )
            sink.append(f"[sidecar] 测试连接结束：ok={ok} network_error={network_error} message={message}")
            self._update_account_after_test(
                platform,
                account_id_resolved,
                ok,
                message,
                network_error=network_error,
            )
            return {
                "ok": ok,
                "message": message,
                "duration_ms": duration_ms,
                "network_error": network_error,
                "account_id": account_id_resolved,
                "account_alias": account.get("display_alias") or "",
                "platform": platform,
            }
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception:  # noqa: BLE001
                    pass
            self.guard.release_test(platform, ref, account_id=engine_account_id)
            if account_id_resolved:
                self.guard.release_account(account_id_resolved, ref)

    def _update_account_after_test(
        self,
        platform: str,
        account_id: str,
        ok: bool,
        message: str,
        network_error: bool = False,
    ) -> None:
        """测试结果双写：accounts（真源）+ account_state（旧字段，仅平台默认账号）。"""
        try:
            if self.accounts is not None and account_id:
                self.accounts.on_test_result(
                    account_id,
                    ok,
                    message,
                    network_error=network_error,
                )
        except Exception:  # noqa: BLE001
            log.exception("写入账号测试结果失败：%s", account_id)
        if not account_id or not self._is_platform_default(platform, account_id):
            return
        try:
            if network_error:
                # 网络异常不能把账号标记成“未登录”，否则会误导用户重新扫码
                self.state.set_account_state(
                    platform,
                    logged_in=None,
                    last_checked_at=now_ts(),
                    message=message or "网络异常，登录态未确认",
                )
                return
            self.state.set_account_state(
                platform,
                logged_in=bool(ok),
                last_checked_at=now_ts(),
                message=("已登录" if ok else (message or "未登录")),
            )
        except Exception:  # noqa: BLE001
            log.exception("写入测试连接结果失败：%s", platform)

    def delete_account(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        """清除登录态/删除 profile（旧路由 = 平台默认账号；账号池 DELETE 走 archive）。"""
        account = self.accounts.resolve(platform, account_id or "")
        account_id_resolved = str(account.get("account_id") or "")
        # 保持全局 busy 拒绝：任意 login/run/test 进行中都不删除登录态；
        # 若恰是该平台在检测，用更明确的平台文案。
        if self.guard.is_exclusive_busy() or self.guard.is_test_busy():
            message = (
                BUSY_PLATFORM_TEST_MESSAGE
                if self.guard.is_test_busy(platform)
                else BUSY_GLOBAL_MESSAGE
            )
            raise ServiceError("BUSY", message, 409)
        # 占用平台槽位 + 账号独占锁，防「检查后、删除前」并发插入 test/run
        ref = f"delete-{platform}-{uuid.uuid4().hex[:6]}"
        busy = self.guard.acquire_test(platform, ref)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        account_busy = (
            self.guard.acquire_account(account_id_resolved, ref) if account_id_resolved else None
        )
        if account_busy:
            self.guard.release_test(platform, ref)
            raise ServiceError("ACCOUNT_BUSY", account_busy, 409)
        try:
            profile_dir = self.accounts.profile_path(account)
            # 账号化：删目录前先按 profile marker 杀掉残留 Edge/Chrome（避免 LOCK 占用导致 DELETE_FAILED）
            engine_args = self.accounts.engine_account_args(account)
            try:
                killed = self.runner.kill_orphan_browsers(
                    platform,
                    account_id=str(engine_args.get("account_id") or ""),
                    profile_template=str(engine_args.get("profile_template") or ""),
                )
                if killed:
                    log.info("清除登录态前已清理残留浏览器 %s 个（platform=%s）", len(killed), platform)
                    time.sleep(0.3)
            except Exception:  # noqa: BLE001 - 查杀失败仍继续尝试删除
                log.exception("清理残留浏览器失败（继续删除）：%s", platform)
            # Windows 下 Edge 退出后 profile 里的 LOCK 文件可能短暂占用，重试删除
            last_error: Optional[Exception] = None
            for attempt in range(3):
                try:
                    if profile_dir.exists():
                        shutil.rmtree(profile_dir, ignore_errors=False)
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.3 * (attempt + 1))
            if last_error is not None or (profile_dir.exists() and any(profile_dir.iterdir())):
                detail = str(last_error) if last_error is not None else "目录仍存在"
                raise ServiceError("DELETE_FAILED", f"删除登录态失败：{detail}", 500)
            self.state.set_account_state(
                platform,
                logged_in=False,
                last_checked_at=now_ts(),
                profile_exists=False,
                message="已清除登录态",
            )
            try:
                self.accounts.update(
                    account_id_resolved,
                    login_state="unknown",
                    status="need_login",
                    risk_note="已清除登录态",
                )
            except Exception:  # noqa: BLE001
                log.exception("写入账号清除登录态失败：%s", account_id_resolved)
            return {"ok": True, "account_id": account_id_resolved}
        finally:
            self.guard.release_test(platform, ref)
            if account_id_resolved:
                self.guard.release_account(account_id_resolved, ref)

    # -- runs -------------------------------------------------------------
    def create_run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.runs.create(params)

    def resume_run(self, run_id: str, platforms: Optional[List[str]] = None) -> Dict[str, Any]:
        return self.runs.resume(run_id, platforms)

    def list_runs(self) -> List[Dict[str, Any]]:
        return self.runs.list_runs()

    def get_run(self, run_id: str) -> Dict[str, Any]:
        return self.runs.get_run(run_id)

    def get_run_logs(self, run_id: str, tail: int) -> List[Dict[str, Any]]:
        return self.runs.logs(run_id, tail)

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        return self.runs.stop(run_id)

    def export_run(self, run_id: str) -> Dict[str, Any]:
        run_raw = self.runs.get_run_raw(run_id)
        if run_raw["status"] == RUN_STATUS_RUNNING:
            raise ServiceError("RUN_RUNNING", "任务仍在运行，结束后才能导出", 409)
        if self._export_fn is None:
            raise ServiceError(
                "EXPORTER_UNAVAILABLE",
                f"导出组件未就绪：{self._exporter_error or 'exporter.py 尚未提供'}",
                503,
            )
        try:
            result = self._export_fn(
                str(self.cfg.media_db),
                str(self.cfg.data_dir),
                run_id,
                list(run_raw["keywords"]),
                int(run_raw["started_ts"]),
            )
        except Exception as exc:  # noqa: BLE001 - exporter 内部错误统一转 500
            log.exception("导出失败：%s", run_id)
            raise ServiceError("EXPORT_FAILED", f"导出失败：{exc}", 500) from exc
        if not isinstance(result, dict):
            raise ServiceError("EXPORT_FAILED", "导出组件返回格式非法", 500)
        file_path = result.get("file_path")
        if not file_path:
            raise ServiceError("EXPORT_FAILED", "导出组件未返回 file_path", 500)
        path = Path(str(file_path))
        if not path.is_absolute():
            path = (self.cfg.data_dir / path).resolve()
        if not path.is_file():
            raise ServiceError("EXPORT_FAILED", f"导出文件未生成：{path}", 500)
        sheets = result.get("sheets") or []
        total_rows = _to_int(result.get("total_rows"), 0)
        if total_rows <= 0 and isinstance(sheets, list):
            total_rows = sum(_to_int(item.get("rows"), 0) for item in sheets if isinstance(item, dict))
        self.state.set_excel_path(run_id, str(path))
        return {
            "ok": True,
            "file_path": str(path),
            "sheets": sheets,
            "total_rows": total_rows,
        }

    def preview_run(self, run_id: str, limit: int = 20) -> Dict[str, Any]:
        run_raw = self.runs.get_run_raw(run_id)
        if self._preview_fn is None:
            raise ServiceError(
                "EXPORTER_UNAVAILABLE",
                f"导出组件未就绪：{self._exporter_error or 'exporter.py 尚未提供'}",
                503,
            )
        try:
            result = self._preview_fn(
                str(self.cfg.media_db),
                list(run_raw["keywords"]),
                int(run_raw["started_ts"]),
                max(1, min(int(limit or 20), 200)),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("数据预览失败：%s", run_id)
            raise ServiceError("PREVIEW_FAILED", f"数据预览失败：{exc}", 500) from exc
        if not isinstance(result, dict) or "sheets" not in result:
            return {"sheets": []}
        return {"sheets": result.get("sheets") or []}

    def get_run_feed(self, run_id: str, limit: int = 30, comments: int = 3) -> Dict[str, Any]:
        """「采集动态」内容流（契约 §2.2 ``GET /api/runs/{run_id}/feed``）。

        - run 不存在 → 404 RUN_NOT_FOUND（沿用现有错误体）；
        - DB/表/列缺失、脏数据由 run_feed.build_run_feed 降级为空内容流，不报 500；
        - limit clamp 1–100（默认 30），comments clamp 0–10（默认 3）。
        """
        run_raw = self.runs.get_run_raw(run_id)
        if self._feed_fn is None:
            raise ServiceError(
                "FEED_UNAVAILABLE",
                f"采集动态组件未就绪：{self._feed_error or 'run_feed.py 尚未提供'}",
                503,
            )
        limit_int = max(1, min(_to_int(limit, 30), 100))
        comment_limit = max(0, min(_to_int(comments, 3), 10))
        try:
            result = self._feed_fn(
                str(self.cfg.media_db),
                run_id=str(run_id),
                status=str(run_raw["status"]),
                keywords=list(run_raw["keywords"]),
                started_ts=run_raw["started_ts"],
                limit=limit_int,
                comments=comment_limit,
            )
        except Exception as exc:  # noqa: BLE001 - 内容流内部错误统一转 500（DB 缺失不在此列）
            log.exception("采集动态读取失败：%s", run_id)
            raise ServiceError("FEED_FAILED", f"采集动态读取失败：{exc}", 500) from exc
        if not isinstance(result, dict):
            raise ServiceError("FEED_FAILED", "采集动态组件返回格式非法", 500)
        return result

    # -- shutdown ---------------------------------------------------------
    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        log.info("sidecar 正在关闭：停止登录会话与采集任务…")
        try:
            self.logins.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("关闭登录会话失败")
        try:
            self.runs.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("关闭采集任务失败")
        try:
            self.state.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭 app.db 失败")






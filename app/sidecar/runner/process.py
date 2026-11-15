# -*- coding: utf-8 -*-
"""进程与日志适配器：日志 tail、引擎子进程、进程树/浏览器进程回收、EngineRunner 启动参数。

M2 从 engine_runner.py 纯搬迁（函数体未改）。依赖方向 process -> config。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import itertools
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, TextIO, Tuple

from state import now_ts

from .config import EngineStartError, SidecarConfig

log = logging.getLogger("sidecar.engine")  # 沿用旧 logger 名：日志输出与拆分前逐字节一致


EVENT_PREFIX = "##EVENT##"
LOG_TAIL_MAX = 5000

# 人工接管类型（与 engine 约定 kind 一致）
NEED_MANUAL_KINDS = ("sms", "slider", "captcha")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_RE_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)

_ERROR_HINTS = ("traceback", "error", "exception", "fatal", "critical", "失败", "错误", "异常")
_WARN_HINTS = ("warn", "warning", "警告", "超时", "timeout")

def classify_log_level(text: str) -> str:
    """把引擎原始日志行粗分类为 info/warn/error（供 UI 上色）。"""
    low = (text or "").lower()
    if any(hint in low for hint in _ERROR_HINTS):
        return "error"
    if any(hint in low for hint in _WARN_HINTS):
        return "warn"
    return "info"

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

class LogSink:
    """一个 scope 的日志：写 ``<data-dir>/logs/<scope>.log`` 且内存保留尾部 5000 行。

    M5a 起内存部分是**环形缓冲 + 绝对行号**（``deque(maxlen=LOG_TAIL_MAX)``）：

    * ``_total`` 是已写入行数（= 下一行行号，0 基），只增不减；
    * ``page()`` 按绝对行号切片，只拷贝请求区间（不再每次把 5000 行全量 list）；
    * 写入路径不变（先入环再落盘），不会因为上限而丢行——丢的只是最早的历史。
    """

    def __init__(self, path: Path, scope: str = "", maxlen: int = LOG_TAIL_MAX) -> None:
        self.path = Path(path)
        self.scope = scope or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._maxlen = max(1, int(maxlen))
        except (TypeError, ValueError):
            self._maxlen = LOG_TAIL_MAX
        self._lines: Deque[Dict[str, Any]] = deque(maxlen=self._maxlen)
        self._total = 0
        self._closed = False
        # 落盘失败（_fh=None）时只保留内存日志，所以类型上必须显式允许 None
        self._fh: Optional[TextIO] = None
        try:
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace", buffering=1)
        except OSError:  # 落盘失败时仍保留内存日志
            self._fh = None

    def append(self, text: str) -> None:
        text = "" if text is None else str(text)
        entry = {"ts": now_ts(), "level": classify_log_level(text), "text": text}
        with self._lock:
            self._lines.append(entry)
            self._total += 1
            if self._fh is not None and not self._closed:
                try:
                    self._fh.write(text + "\n")
                except Exception:  # noqa: BLE001
                    self._fh = None

    @property
    def next_line(self) -> int:
        """下一行行号（0 基）= 已写入总行数。"""
        with self._lock:
            return self._total

    def _slice_locked(self, start: int, limit: Optional[int]) -> List[Dict[str, Any]]:
        """按**绝对行号**拷贝环内切片（只拷贝需要的一段，避免每次全量 list）。"""
        size = len(self._lines)
        first_abs = self._total - size
        offset = max(0, int(start) - first_abs)
        end = size if limit is None else min(size, offset + max(0, int(limit)))
        if offset >= end:
            return []
        return list(itertools.islice(self._lines, offset, end))

    def tail(self, count: int = 200) -> List[Dict[str, Any]]:
        """尾部 ``count`` 行（现状语义；count clamp 1–LOG_TAIL_MAX）。"""
        count = max(1, min(int(count or 200), self._maxlen))
        with self._lock:
            return self._slice_locked(max(0, self._total - count), count)

    def page(
        self, count: int = 200, after_line: Optional[int] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """返回 ``(行列表, next_line)``。

        * ``after_line=None`` → 尾部 ``count`` 行（与 ``tail`` 一致）；
        * ``after_line=N`` → 只返回行号 **> N** 的新行（0 基，单次最多 ``LOG_TAIL_MAX`` 行）。

        ``next_line`` = 下一行行号（总行数），且**不回退**（客户端游标超前后保持
        ``max(总行数, after_line + 1)``，避免重复拉取历史）。
        """
        count_int = max(1, min(int(count or 200), self._maxlen))
        with self._lock:
            if after_line is None:
                return self._slice_locked(max(0, self._total - count_int), count_int), self._total
            start = max(0, int(after_line) + 1)
            return self._slice_locked(start, self._maxlen), max(self._total, start)

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


def read_log_file_page(
    path: Path, count: int = 200, after_line: Optional[int] = None
) -> Tuple[List[Dict[str, Any]], int]:
    """sidecar 重启后从日志文件读取（行号 = 文件内 0 基行号，只增不减）。

    * ``after_line=None`` → 尾部 ``count`` 行（``read_log_file_tail`` 的语义）；
    * ``after_line=N`` → 只返回文件行号 > N 的行（单次最多 ``LOG_TAIL_MAX`` 行）。

    返回 ``(行列表, next_line)``；``ts`` 仍近似为文件 mtime（与改造前一致）。
    """
    count_int = max(1, min(int(count or 200), LOG_TAIL_MAX))
    is_tail = after_line is None
    lower = 0 if after_line is None else max(0, int(after_line) + 1)
    keep: Deque[str] = deque(maxlen=count_int)
    fresh: List[str] = []
    total = 0
    mtime = 0
    if not path.is_file():
        return [], (0 if is_tail else max(0, lower))
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for index, line in enumerate(fh):
                total = index + 1
                text = line.rstrip("\r\n")
                if not text:
                    continue
                if is_tail:
                    keep.append(text)
                    continue
                if index < lower or len(fresh) >= LOG_TAIL_MAX:
                    continue
                fresh.append(text)
        mtime = int(path.stat().st_mtime)
    except OSError:
        return [], (0 if is_tail else max(0, lower))
    texts = list(keep) if is_tail else fresh
    lines = [{"ts": mtime, "level": classify_log_level(text), "text": text} for text in texts]
    return lines, (total if is_tail else max(total, lower))


def read_log_file_tail(path: Path, count: int) -> List[Dict[str, Any]]:
    """sidecar 重启后从日志文件读取尾部（ts 近似为文件 mtime）。"""
    return read_log_file_page(path, count)[0]

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
        line_prefix: str = "",
    ) -> None:
        self.cmd = list(cmd)
        # 平台并行时多个引擎写同一个 run 日志：每行加 ``[platform] `` 前缀，前端按前缀归属平台
        self.line_prefix = line_prefix
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
                        self.sink.append(self.line_prefix + raw)
                else:
                    self.sink.append(self.line_prefix + raw)
        except Exception as exc:  # noqa: BLE001 - 读取端异常同样落日志
            self.sink.append(f"[sidecar] 读取引擎输出异常：{exc}")
        finally:
            try:
                proc.stdout.close()
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
        line_prefix: str = "",
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
        sink.append(line_prefix + "[sidecar] 启动引擎：" + " ".join(cmd))
        proc = EngineProcess(
            cmd=cmd,
            cwd=self.cfg.mc_root,
            env=self._base_env(),
            sink=sink,
            on_event=on_event,
            label=label,
            line_prefix=line_prefix,
        )
        proc.start()
        sink.append(f"{line_prefix}[sidecar] 引擎已启动 pid={proc.pid}")
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
        options: Optional[Dict[str, Any]] = None,
        line_prefix: bool = False,
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
        if options:
            # 空 options 不追加参数：旧 CLI 逐字节兼容
            args.extend(["--options-json", json.dumps(options, ensure_ascii=False, sort_keys=True)])
        label = f"crawl-{platform}{self._scope_suffix(account_id)}"
        prefix = f"[{platform}] " if line_prefix else ""
        return self._spawn(args, sink, on_event, label=label, line_prefix=prefix)

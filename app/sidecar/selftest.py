# -*- coding: utf-8 -*-
"""sidecar 离线自测（不联网、不启动浏览器、不需要真实账号）。

运行方式（契约 §5.1）::

    # 用 MediaCrawler venv 的 python 执行
    MediaCrawler\\.venv\\Scripts\\python.exe app/sidecar/selftest.py

覆盖链路：

0. 阶段 0 单元校验：登录默认有头（`--headless false`）、420s 超时、test 保持无头、
   `SIDECAR_LOGIN_HEADLESS`/`SIDECAR_LOGIN_TIMEOUT` 覆盖、need_manual kind 推断/默认文案；
1. 启动 sidecar（``FAKE_ENGINE=1`` 注入假 engine，假 engine 遵守契约 §1 事件协议）
   → 校验 ``##SIDECAR_READY##{"port": N}`` 格式与 /health；
2. 鉴权：/api/* 无 token/错 token → 401 ``{error:{code:"UNAUTHORIZED"}}``；
3. /api/config、/api/system/info、CORS；
4. /api/accounts：初始 7 平台；扫码登录 confirmed → logged_in=true；
   登录过期 expired、慢速登录取消 cancelled；
   **need_manual 人工接管（kind=sms）=status 透传/不作为失败/二维码保留/waiting_scan 恢复/confirmed**；
5. /api/accounts/{p}/test 同步检测（并断言 test 子进程 --headless true）；
   **测试连接并发语义**：不同平台可并行（wall < 串行和 85%）、同平台第二个 409「该平台」、
   test 期间 login/run 409（全局文案）、login/run 期间 test 409；
6. /api/runs：创建（立即返回 running）→ 平台串行（xhs 成功 / dy 失败继续）
   → run=done（部分失败）→ 详情/日志/列表；stop 取消 → run=cancelled；
   互斥 BUSY；参数校验；
7. 导出：真实 exporter 就绪时做集成校验（失败仅 WARN，不阻塞 sidecar 自测）；
   未就绪时断言 503 EXPORTER_UNAVAILABLE；随后用 ``_fake_exporter`` 注入，
   重启 sidecar 后严格校验导出链路（文件存在 / sheet / excel_path 回写 / 预览）；
8. 重启恢复：手工插入一条 running 记录 → 重启后 running 置 failed/interrupted；
   历史 run 仍可列出；accounts 登录态持久化；
9. /api/system/open-file 只允许 ``<data-dir>/exports`` 下文件（403/404 分支）；
10. 未知平台/任务/登录会话的 404 与错误体结构。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SIDECAR_DIR = Path(__file__).resolve().parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))

PROJECT_ROOT = SIDECAR_DIR.parent.parent
MAIN_PY = SIDECAR_DIR / "main.py"


def offline_mc_root() -> Path:
    """离线测试用 MediaCrawler 根目录。

    已安装真实采集引擎时用真实目录；干净 clone（未跑 setup_mediacrawler.ps1）时在临时目录
    造一个占位根（engine 子进程启动前置校验只检查目录/入口存在，fake engine 不读取内容），
    保证离线自测不依赖第三方采集引擎是否已安装。
    """
    real = PROJECT_ROOT / "MediaCrawler"
    if (real / "main.py").is_file():
        return real
    stub = Path(tempfile.gettempdir()) / "opinionlens_selftest_media_crawler"
    (stub / "media_platform").mkdir(parents=True, exist_ok=True)
    (stub / "main.py").write_text("# offline selftest stub (fake engine only)\n", encoding="utf-8")
    return stub


READY_PREFIX = "##SIDECAR_READY##"
READY_RE = re.compile(r"^##SIDECAR_READY##(\{.*\})$")

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_RE_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)

try:
    import httpx
except Exception as exc:  # pragma: no cover - venv 已装 httpx
    print(f"[selftest] 缺少 httpx：{exc}（请用 MediaCrawler/.venv 的 python 运行）")
    sys.exit(2)

import engine_runner as er  # noqa: E402  （阶段 0 用它做登录模式/超时/命令行参数单元校验）
from _fake_llm_server import FakeLLMServer  # noqa: E402  （selftest 假模型 API：OpenAI + Anthropic）


# ---------------------------------------------------------------------------
# 结果统计
# ---------------------------------------------------------------------------
class SelfTestFailure(AssertionError):
    pass


class Report:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []
        self.warned: List[str] = []
        self.skipped: List[str] = []

    def check(self, name: str, condition: Any, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  [PASS] {name}")
        else:
            self.failed.append(f"{name} {detail}".strip())
            raise SelfTestFailure(f"{name} {detail}".strip())

    def warn(self, name: str) -> None:
        self.warned.append(name)
        print(f"  [WARN] {name}")

    def skip(self, name: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}")


# ---------------------------------------------------------------------------
# sidecar 进程管理
# ---------------------------------------------------------------------------
class SidecarProcess:
    def __init__(
        self,
        python: str,
        token: str,
        data_dir: Path,
        env_extra: Dict[str, str],
        label: str = "sidecar",
    ) -> None:
        self.python = python
        self.token = token
        self.data_dir = data_dir
        self.env_extra = dict(env_extra)
        self.label = label
        self.proc: Optional[subprocess.Popen[str]] = None
        self.port = 0
        self._stdout: List[str] = []
        self._stderr: List[str] = []
        self._lock = threading.Lock()

    # -- 启停 -------------------------------------------------------------
    def start(self) -> None:
        env = os.environ.copy()
        env.pop("SIDECAR_EXPORTER_MODULE", None)
        env.pop("FAKE_ENGINE", None)
        # AI 报告 / 报告中心配置只由本测试显式注入，避免宿主环境变量影响用例
        for key in (
            "FS_APP_VERSION",
            "SIDECAR_REPORT_DATA_MODULE",
            "SIDECAR_REPORT_TIMEOUT",
            "SIDECAR_REPORT_POLL_INTERVAL",
        ):
            env.pop(key, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        env["SIDECAR_DISABLE_OPEN"] = "1"
        env.update(self.env_extra)
        cmd = [
            self.python,
            str(MAIN_PY),
            "--port",
            "0",
            "--token",
            self.token,
            "--data-dir",
            str(self.data_dir),
            "--mc-root",
            str(offline_mc_root()),
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_RE_CREATE_NO_WINDOW,
        )
        threading.Thread(
            target=self._read_stream, args=(self.proc.stdout, self._stdout), daemon=True
        ).start()
        threading.Thread(
            target=self._read_stream, args=(self.proc.stderr, self._stderr), daemon=True
        ).start()

    def _read_stream(self, stream, sink: List[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                with self._lock:
                    sink.append(line.rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=15,
                        creationflags=_RE_CREATE_NO_WINDOW,
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.proc = None

    # -- 就绪 -------------------------------------------------------------
    def wait_ready(self, timeout: float = 45.0) -> Tuple[int, str]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                lines = list(self._stdout)
            for line in lines:
                match = READY_RE.match(line.strip())
                if not match:
                    continue
                payload = json.loads(match.group(1))
                self.port = int(payload["port"])
                return self.port, line.strip()
            proc = self.proc
            if proc is not None and proc.poll() is not None:
                raise SelfTestFailure(
                    f"sidecar 启动失败（退出码 {proc.returncode}）：\n{self.stderr_text()[-4000:]}"
                )
            time.sleep(0.05)
        raise SelfTestFailure(f"等待 SIDECAR_READY 超时：\n{self.stderr_text()[-4000:]}")

    def stderr_text(self) -> str:
        with self._lock:
            return "\n".join(self._stderr)

    def stdout_text(self) -> str:
        with self._lock:
            return "\n".join(self._stdout)


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------
class Ctx:
    def __init__(
        self,
        base_url: str,
        token: str,
        data_dir: Path,
        report: Report,
        real_exporter: bool,
        llm_server: Optional["FakeLLMServer"] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.data_dir = data_dir
        self.report = report
        self.real_exporter = real_exporter
        self.llm_server = llm_server
        self.client = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass

    # -- 基础请求 ---------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[int, Any, str]:
        request_headers: Dict[str, str] = {}
        if headers:
            request_headers.update(headers)
        if auth and "Authorization" not in request_headers:
            request_headers["Authorization"] = f"Bearer {self.token}"
        resp = self.client.request(method, self.base_url + path, headers=request_headers, json=body)
        text = resp.text
        try:
            data: Any = resp.json()
        except ValueError:
            data = None
        return resp.status_code, data, text

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        expect: int = 200,
        auth: bool = True,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        status, data, text = self.request(
            method, path, body=body, auth=auth, headers=headers
        )
        if status != expect:
            raise SelfTestFailure(
                f"{method} {path} 期望 HTTP {expect}，实际 {status}：{text[:1000]}"
            )
        return data

    def call_error(
        self,
        method: str,
        path: str,
        *,
        expect_status: int,
        expect_code: str,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        status, data, text = self.request(method, path, body=body, auth=auth)
        if status != expect_status:
            raise SelfTestFailure(
                f"{method} {path} 期望 HTTP {expect_status}，实际 {status}：{text[:1000]}"
            )
        if not isinstance(data, dict) or not isinstance(data.get("error"), dict):
            raise SelfTestFailure(f"{method} {path} 错误体不是 {{error:{{code,message}}}}：{text[:500]}")
        code = str(data["error"].get("code") or "")
        message = str(data["error"].get("message") or "")
        if code != expect_code:
            raise SelfTestFailure(
                f"{method} {path} 期望错误码 {expect_code}，实际 {code}（{message}）"
            )
        if not message:
            raise SelfTestFailure(f"{method} {path} 错误体缺少 message")
        return data["error"]

    def call_error_any(
        self,
        method: str,
        path: str,
        *,
        expect_status: int,
        expect_codes: Tuple[str, ...],
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        """断言错误状态与错误码集合（兼容账号池/互斥语义演进，如 BUSY vs ACCOUNT_BUSY）。"""
        status, data, text = self.request(method, path, body=body, auth=auth)
        if status != expect_status:
            raise SelfTestFailure(
                f"{method} {path} 期望 HTTP {expect_status}，实际 {status}：{text[:1000]}"
            )
        if not isinstance(data, dict) or not isinstance(data.get("error"), dict):
            raise SelfTestFailure(f"{method} {path} 错误体不是 {{error:{{code,message}}}}：{text[:500]}")
        code = str(data["error"].get("code") or "")
        if code not in expect_codes:
            raise SelfTestFailure(
                f"{method} {path} 期望错误码 ∈ {expect_codes}，实际 {code}"
                f"（{data['error'].get('message')}）"
            )
        if not str(data["error"].get("message") or ""):
            raise SelfTestFailure(f"{method} {path} 错误体缺少 message")
        return data["error"]

    # -- 轮询 -------------------------------------------------------------
    def poll(self, func, description: str, timeout: float, interval: float = 0.08) -> Any:
        deadline = time.monotonic() + timeout
        last_error: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                value = func()
            except SelfTestFailure:
                raise
            except Exception as exc:  # noqa: BLE001 - 轮询期间网络/解析异常视为未就绪
                last_error = exc
                value = None
            if value:
                return value
            time.sleep(interval)
        detail = f"（最后异常：{last_error}）" if last_error else ""
        raise SelfTestFailure(f"轮询超时：{description}{detail}")

    def call_no_busy(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        expect: int = 200,
        timeout: float = 15.0,
    ) -> Any:
        """调用一个「空闲才允许」的接口：短暂 409 BUSY 时重试（收尾阶段互斥释放有毫秒级延迟）。"""
        deadline = time.monotonic() + timeout
        while True:
            status, data, text = self.request(method, path, body=body)
            if status == expect:
                return data
            if (
                status == 409
                and isinstance(data, dict)
                and (data.get("error") or {}).get("code") in ("BUSY", "ACCOUNT_BUSY")
                and time.monotonic() < deadline
            ):
                time.sleep(0.1)
                continue
            raise SelfTestFailure(
                f"{method} {path} 期望 HTTP {expect}，实际 {status}：{text[:800]}"
            )


def _is_login_terminal(status: str) -> bool:
    return status in ("confirmed", "failed", "expired", "cancelled")


# ---------------------------------------------------------------------------
# 环境探测
# ---------------------------------------------------------------------------
def detect_real_exporter() -> Tuple[bool, str]:
    try:
        import exporter as real_exporter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"exporter.py 尚未就绪（{type(exc).__name__}: {exc}）"
    if not callable(getattr(real_exporter, "export_run_excel", None)):
        return False, "exporter.py 缺少 export_run_excel"
    if not callable(getattr(real_exporter, "preview_run", None)):
        return False, "exporter.py 缺少 preview_run"
    return True, ""


def _arg_value(args: List[str], name: str) -> Optional[str]:
    """取命令行参数 ``--name value`` / ``--name=value`` 的 value。"""
    for idx, item in enumerate(args):
        if item == name and idx + 1 < len(args):
            return args[idx + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def read_fake_engine_args(data_dir: Path, mode: str, platform: str) -> Optional[Dict[str, Any]]:
    """读取 fake engine 落盘的本次命令行参数（验证 sidecar→engine 传参）。

    账号级调用（v2 账号）落盘名带 ``_<account_id>`` 后缀；旧单账号（legacy）不带后缀。
    这里先找无后缀文件，再退回「最新一个带后缀的文件」，两种布局都能读到。
    """
    logs = data_dir / "logs"
    path = logs / f"fake_engine_args_{mode}_{platform}.json"
    if not path.is_file():
        candidates = sorted(
            logs.glob(f"fake_engine_args_{mode}_{platform}_*.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        path = candidates[0]
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Phase 0：登录模式 / 超时 / 命令行参数（纯单元校验，不起服务）
# ---------------------------------------------------------------------------
def phase0_config_unit(report: Report, data_dir: Path) -> None:
    print("\n[阶段 0] 登录有头模式 / 420s 超时 / need_manual 相关单元校验")
    unit_dir = data_dir / "unit"
    unit_dir.mkdir(parents=True, exist_ok=True)
    cfg = er.SidecarConfig(
        data_dir=unit_dir,
        mc_root=offline_mc_root(),
        python=Path(sys.executable),
        engine_entry=SIDECAR_DIR / "_fake_engine.py",
    )
    report.check(
        "SidecarConfig 默认登录有头（login_headless=False）",
        cfg.login_headless is False,
        repr(cfg.login_headless),
    )
    report.check(
        "SidecarConfig 默认登录超时 420s",
        cfg.login_timeout == 420,
        repr(cfg.login_timeout),
    )
    runner = er.EngineRunner(cfg)
    login_args = runner.login_args("xhs")
    report.check(
        "登录子进程参数 --headless false（有头窗口）",
        _arg_value(login_args, "--headless") == "false",
        " ".join(login_args),
    )
    report.check(
        "登录子进程参数 --timeout 420",
        _arg_value(login_args, "--timeout") == "420",
        " ".join(login_args),
    )
    test_args = runner.test_args("xhs")
    report.check(
        "test 子进程参数 --headless true（保持无头）",
        _arg_value(test_args, "--headless") == "true",
        " ".join(test_args),
    )

    old_env = {
        "SIDECAR_LOGIN_HEADLESS": os.environ.get("SIDECAR_LOGIN_HEADLESS"),
        "SIDECAR_LOGIN_TIMEOUT": os.environ.get("SIDECAR_LOGIN_TIMEOUT"),
    }
    os.environ["SIDECAR_LOGIN_HEADLESS"] = "1"
    os.environ["SIDECAR_LOGIN_TIMEOUT"] = "600"
    try:
        cfg_env = er.SidecarConfig.from_env(unit_dir / "env")
        report.check(
            "SIDECAR_LOGIN_HEADLESS=1 覆盖为无头",
            cfg_env.login_headless is True,
            repr(cfg_env.login_headless),
        )
        report.check(
            "SIDECAR_LOGIN_TIMEOUT=600 覆盖默认超时",
            cfg_env.login_timeout == 600,
            repr(cfg_env.login_timeout),
        )
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # need_manual kind 推断（engine 新版本带 kind / 老版本只有 message）
    report.check(
        "need_manual 事件带 kind 时原样保留",
        er.infer_need_manual_kind({"kind": "slider", "message": "请拖动滑块"}) == "slider",
    )
    report.check(
        "need_manual 事件无 kind 时从 message 推断 sms",
        er.infer_need_manual_kind({"message": "平台需要短信验证：请输入验证码"}) == "sms",
    )
    report.check(
        "need_manual 嵌套 need_manual.kind 也支持",
        er.infer_need_manual_kind({"need_manual": {"kind": "captcha"}, "message": "安全验证"}) == "captcha",
    )
    report.check(
        "need_manual 默认文案含短信验证提示",
        "短信验证" in er.default_need_manual_message("sms")
        and "浏览器窗口" in er.default_need_manual_message("sms"),
        er.default_need_manual_message("sms"),
    )


# ---------------------------------------------------------------------------
# Phase 0.2：模型 API + 报告中心单元校验（fake LLM + fake report_data）
# ---------------------------------------------------------------------------
def phase0_report_unit(
    report: Report, data_dir: Path, fake_llm: "FakeLLMServer"
) -> None:
    print("\n[阶段 0.2] 模型 API / 报告中心单元校验（fake LLM，OpenAI + Anthropic）")
    import state as state_mod
    from engine_runner import ServiceError, SidecarConfig
    from llm_client import LLMError, LLMService, LocalLLMClient
    from report_manager import ReportManager

    fake_llm.reset()
    unit_dir = data_dir / "report_unit"
    unit_dir.mkdir(parents=True, exist_ok=True)
    store = state_mod.StateStore(unit_dir / "app.db")
    llm = LLMService(None, store)
    client = LocalLLMClient(None, llm=llm)
    manager = None
    try:
        provider = llm.create_provider(
            {
                "name": "selftest-fake-provider",
                "kind": "openai_compatible",
                "base_url": fake_llm.base_url,
                "model": "fake-model",
                "api_key": "selftest-fake-key-1234",
            }
        )
        report.check(
            "LLMService.create_provider 密钥脱敏（****1234，忽略明文）",
            provider.get("api_key_masked") == "****1234"
            and provider.get("secret_configured") is True
            and "selftest-fake-key-1234" not in json.dumps(provider, ensure_ascii=False),
            json.dumps(provider, ensure_ascii=False)[:400],
        )
        connection = client.test_connection()
        report.check(
            "LocalLLMClient.test_connection ping 默认 provider（ok/model/local-llm）",
            connection.get("ok") is True
            and connection.get("model") == "fake-model"
            and ((connection.get("capabilities") or {}).get("narrative") or {}).get("endpoint")
            == "local-llm",
            json.dumps(connection, ensure_ascii=False)[:500],
        )
        items = [
            {
                "id": f"item_{index}",
                "text": "示例科技好评积极" if index % 2 == 0 else "项目投诉交付慢，负面",
            }
            for index in range(120)
        ]
        rows = client.analyze_sentiment(items)
        sizes = fake_llm.sentiment_batch_sizes()
        report.check(
            "LocalLLMClient.analyze_sentiment ≤50 条/批（120→50/50/20）且 id 对齐",
            len(rows) == 120
            and sizes == [50, 50, 20]
            and {str(row.get("id")) for row in rows}
            == {f"item_{index}" for index in range(120)},
            f"rows={len(rows)} sizes={sizes}",
        )
        package = {
            "schema_version": "public-sentiment-data@v1",
            "report": {
                "title": "示例科技舆情监测周报",
                "window": {"start": "2026-11-01", "end": "2026-11-09"},
                "keywords": ["示例科技"],
                "platforms": ["xhs"],
            },
            "counts": {"total": 4},
            "stats": {"risk": {"level": "低"}},
            "samples": {},
        }
        response = client.generate_narrative(package)
        narrative = response.get("narrative") if isinstance(response, dict) else {}
        report.check(
            "LocalLLMClient.generate_narrative 返回合法 narrative（meta.generated_by=local-llm）",
            response.get("ok") is True
            and (response.get("meta") or {}).get("generated_by") == "local-llm"
            and isinstance(narrative, dict)
            and bool((narrative.get("overview") or {}).get("one_line_summary"))
            and len(narrative.get("suggestions") or []) >= 1,
            json.dumps(response, ensure_ascii=False)[:500],
        )
        fake_llm.mode = "fail"
        error_code = ""
        try:
            llm.chat(llm.get_default() or {}, [{"role": "user", "content": "ping"}], timeout=5)
        except LLMError as exc:
            error_code = exc.code
        fake_llm.mode = "ok"
        report.check(
            "模型 HTTP 500 → LLMError(LLM_HTTP_5XX)（报告侧可识别并降级）",
            error_code == "LLM_HTTP_5XX",
            error_code,
        )
        # 本地编排错误分支（不依赖 sidecar 进程）
        unit_data_dir = unit_dir / "manager"
        unit_data_dir.mkdir(parents=True, exist_ok=True)
        sidecar_cfg = SidecarConfig(
            data_dir=unit_data_dir,
            mc_root=offline_mc_root(),
            python=Path(sys.executable),
            engine_entry=PROJECT_ROOT / "app" / "engine" / "entry.py",
        )
        empty_store = state_mod.StateStore(unit_dir / "empty" / "app.db")
        empty_llm = LLMService(None, empty_store)
        try:
            manager = ReportManager(
                sidecar_cfg, llm=empty_llm, report_data_loader=lambda: (None, "selftest: 未就绪")
            )
            code = ""
            status_code = 0
            try:
                manager.create_report({"keywords": ["示例科技"]})
            except ServiceError as exc:
                code, status_code = exc.code, exc.status_code
            report.check(
                "ReportManager report_data 未就绪 → 503 REPORT_DATA_UNAVAILABLE",
                code == "REPORT_DATA_UNAVAILABLE" and status_code == 503,
                f"{code}/{status_code}",
            )
        finally:
            if manager is not None:
                manager.shutdown()
                manager = None
            empty_store.close()
        manager = ReportManager(
            sidecar_cfg,
            llm=llm,
            report_data_loader=lambda: (__import__("_fake_report_data").build_report_package, ""),
        )
        try:
            created = manager.create_report(
                {
                    "window_start": "2026-11-01",
                    "window_end": "2026-11-09",
                    "keywords": ["示例科技"],
                    "platforms": ["xhs", "dy"],
                }
            )
            report.check(
                "ReportManager 未配置模型 API 也不拒绝（返回 queued，worker 走本地模板）",
                created.get("status") == "queued"
                and str(created.get("report_id") or "").startswith("rp_"),
                repr(created),
            )
            report_id = str(created.get("report_id") or "")
            final: Dict[str, Any] = {}
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                final = manager.get_report(report_id)
                if final.get("status") in ("completed", "degraded", "failed"):
                    break
                time.sleep(0.05)
            report.check(
                "fake report_data 报告生成完成（degraded 本地模板，AI 叙述已尝试）",
                final.get("status") == "degraded"
                and (final.get("meta") or {}).get("source") == "local-ai"
                and final.get("narrative_generated_by") == "local-llm"
                and bool(fake_llm.narrative_requests),
                json.dumps(final, ensure_ascii=False)[:700],
            )
        finally:
            manager.shutdown()
            manager = None
    finally:
        if manager is not None:
            try:
                manager.shutdown()
            except Exception:  # noqa: BLE001
                pass
        client.close()
        store.close()
        fake_llm.mode = "ok"
        fake_llm.reset()


def phase0_feature_units(report: Report) -> None:
    """新功能离线单测：多账号号池 / 通知推送 / 模型 API。

    三个模块各自提供 ``run(report=None) -> (passed, failed)``，本函数只做编排与汇总；
    模块内部完全离线（本地 fake HTTP / fake SMTP / 临时目录），不联网、不拉真实浏览器。
    """
    print("\n[阶段 0.3] 新功能离线单测：账号池 / 通知推送 / 模型 API")

    def _run(name: str, module_name: str) -> None:
        try:
            module = __import__(module_name)
        except Exception as exc:  # noqa: BLE001 - 导入失败要算失败项
            report.check(f"{name}：导入 {module_name}", False, repr(exc))
            return
        run = getattr(module, "run", None)
        if not callable(run):
            report.check(f"{name}：{module_name}.run 可调用", False)
            return
        try:
            result = run()
        except Exception as exc:  # noqa: BLE001
            report.check(f"{name}：离线自测执行", False, repr(exc))
            return
        if isinstance(result, tuple) and len(result) == 2:
            passed, failed = int(result[0]), int(result[1])
        else:  # 兼容只返回失败数的实现
            passed, failed = 0, int(result or 0)
        report.check(
            f"{name}：{passed} 项通过 / {failed} 项失败",
            failed == 0 and passed > 0,
            f"passed={passed} failed={failed}",
        )

    _run("多账号号池", "test_account_pool")
    _run("通知推送", "test_notify")
    _run("模型 API", "test_llm")


# ---------------------------------------------------------------------------
# Phase 1：新数据目录，跑完整 API 链路
# ---------------------------------------------------------------------------
def phase1(ctx: Ctx) -> Dict[str, Any]:
    report = ctx.report
    result: Dict[str, Any] = {}

    print("\n[阶段 1] 服务基础 / 鉴权 / 登录 / 测试连接 / 采集 / 停止 / 错误体")

    # 1. /health 免鉴权
    health = ctx.call("GET", "/health", auth=False)
    report.check("GET /health 免鉴权返回 {ok:true}", health == {"ok": True}, repr(health))

    # 2. 鉴权
    ctx.call_error("GET", "/api/config", expect_status=401, expect_code="UNAUTHORIZED", auth=False)
    status, data, _ = ctx.request(
        "GET", "/api/config", headers={"Authorization": "Bearer wrong-token"}
    )
    report.check(
        "错误 token 返回 401 + UNAUTHORIZED",
        status == 401 and isinstance(data, dict) and (data.get("error") or {}).get("code") == "UNAUTHORIZED",
    )

    # 3. /api/config
    config = ctx.call("GET", "/api/config")
    expected_labels = {
        "xhs": "小红书",
        "dy": "抖音",
        "ks": "快手",
        "bili": "B站",
        "wb": "微博",
        "tieba": "贴吧",
        "zhihu": "知乎",
    }
    report.check(
        "GET /api/config 字段齐全且 platform_labels 正确",
        isinstance(config.get("data_dir"), str)
        and Path(config["data_dir"]).is_absolute()
        and isinstance(config.get("mc_root"), str)
        and isinstance(config.get("python"), str)
        and config.get("platform_labels") == expected_labels,
        json.dumps(config, ensure_ascii=False)[:600],
    )
    report.check("GET /api/config data_dir 指向本次临时目录", Path(config["data_dir"]) == ctx.data_dir.resolve())
    report.check(
        "GET /api/config 暴露登录有头模式与 420s 超时（契约外附加字段）",
        config.get("login_headless") is False and int(config.get("login_timeout") or 0) == 420,
        json.dumps(config, ensure_ascii=False)[:600],
    )

    # 4. /api/system/info
    info = ctx.call("GET", "/api/system/info")
    report.check(
        "GET /api/system/info 字段齐全",
        isinstance(info.get("version"), str)
        and isinstance(info.get("data_dir"), str)
        and isinstance(info.get("logs_dir"), str),
        repr(info),
    )

    # 5. /api/accounts 初始态
    accounts = ctx.call("GET", "/api/accounts")["accounts"]
    report.check(
        "GET /api/accounts 返回 7 个平台（顺序固定）",
        [item["platform"] for item in accounts]
        == ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"],
        repr([item["platform"] for item in accounts]),
    )
    first = accounts[0]
    report.check(
        "GET /api/accounts 单条字段符合契约",
        {"platform", "label", "logged_in", "last_checked_at", "profile_exists", "message"}
        .issubset(set(first.keys()))
        and first["label"] == "小红书"
        and isinstance(first["profile_exists"], bool)
        and first["logged_in"] in (None, False, True),
        repr(first),
    )
    report.check(
        "初始未登录时 xhs logged_in 不是 true",
        first["logged_in"] is not True,
        repr(first),
    )

    # 6. 扫码登录（fake engine 默认 confirmed）
    login = ctx.call_no_busy("POST", "/api/accounts/xhs/login")
    report.check(
        "POST /api/accounts/xhs/login 立刻返回 {login_id,status:starting}",
        login.get("status") == "starting" and isinstance(login.get("login_id"), str) and login["login_id"],
        repr(login),
    )
    login_id = login["login_id"]
    seen_statuses: List[str] = []

    def _login_status() -> Optional[Dict[str, Any]]:
        data = ctx.call("GET", f"/api/accounts/xhs/login/{login_id}")
        seen_statuses.append(str(data.get("status")))
        return data if _is_login_terminal(str(data.get("status"))) else None

    final_login = ctx.poll(_login_status, "轮询 xhs 登录到 confirmed", timeout=25.0)
    report.check("扫码登录最终状态 confirmed", final_login["status"] == "confirmed", repr(final_login))
    qr = final_login.get("qr")
    report.check(
        "登录响应携带 qr{seq,image,file} 且 image 为 PNG dataURL",
        isinstance(qr, dict)
        and set(qr.keys()) == {"seq", "image", "file"}
        and str(qr.get("image") or "").startswith("data:image/png;base64,")
        and Path(str(qr.get("file"))).is_file(),
        repr(qr)[:500],
    )
    report.check(
        "登录响应包含契约字段（并附加 need_manual 信息字段）",
        {"login_id", "platform", "status", "qr", "message", "started_at"}.issubset(final_login.keys())
        and "kind" in final_login
        and "need_manual" in final_login
        and "manual_required" in final_login,
        repr(final_login.keys()),
    )
    report.check("登录过程中至少出现过 waiting_* 状态", any(s.startswith("waiting") for s in seen_statuses), str(seen_statuses))
    accounts = ctx.call("GET", "/api/accounts")["accounts"]
    xhs_account = accounts[0]
    report.check(
        "confirmed 后 /api/accounts 显示 logged_in=true 且记录 last_checked_at",
        xhs_account["logged_in"] is True and isinstance(xhs_account["last_checked_at"], int),
        repr(xhs_account),
    )

    # 6.1 登录子进程实际传参：有头窗口 + 420s 超时（fake engine 会落盘 argv）
    fake_login_args = read_fake_engine_args(ctx.data_dir, "login", "xhs")
    report.check(
        "登录子进程实际收到 --headless false（有头 Edge 窗口）",
        isinstance(fake_login_args, dict) and str(fake_login_args.get("headless")).lower() == "false",
        repr(fake_login_args),
    )
    report.check(
        "登录子进程实际收到 --timeout 420",
        isinstance(fake_login_args, dict) and int(fake_login_args.get("timeout") or 0) == 420,
        repr(fake_login_args),
    )

    # 7. 慢速登录取消
    slow_login = ctx.call_no_busy("POST", "/api/accounts/wb/login")
    wb_login_id = slow_login["login_id"]

    def _wb_status():
        data = ctx.call("GET", f"/api/accounts/wb/login/{wb_login_id}")
        return data if data["status"] in ("waiting_qr", "waiting_scan") else None

    ctx.poll(_wb_status, "轮询 wb 登录进入等待扫码", timeout=20.0)
    # 登录/检测按平台解耦：同平台 test 被拒（平台文案），其它平台 test 允许
    status_wb_test, data_wb_test, text_wb_test = ctx.request("POST", "/api/accounts/wb/test")
    wb_test_err = (data_wb_test or {}).get("error") or {}
    report.check(
        "登录进行中同平台 test 返回 409 互斥（BUSY/ACCOUNT_BUSY + 平台/账号占用文案）",
        status_wb_test == 409
        and wb_test_err.get("code") in ("BUSY", "ACCOUNT_BUSY")
        and any(
            token in str(wb_test_err.get("message") or "")
            for token in ("该平台", "该账号", "正在", "占用")
        ),
        f"status={status_wb_test} body={text_wb_test[:300]}",
    )
    cross_test = ctx.call_no_busy("POST", "/api/accounts/ks/test", timeout=30.0)
    report.check(
        "登录进行中其它平台 test 允许（按平台解耦，返回真实结果）",
        cross_test.get("ok") is True and int(cross_test.get("duration_ms") or 0) >= 1000,
        repr(cross_test),
    )
    cancelled = ctx.call("POST", f"/api/accounts/wb/login/{wb_login_id}/cancel")
    report.check("取消登录返回 {ok:true}", cancelled.get("ok") is True, repr(cancelled))

    def _wb_cancelled():
        data = ctx.call("GET", f"/api/accounts/wb/login/{wb_login_id}")
        return data if data["status"] == "cancelled" else None

    wb_final = ctx.poll(_wb_cancelled, "轮询 wb 登录到 cancelled", timeout=20.0)
    report.check("取消后登录状态 cancelled", wb_final["status"] == "cancelled", repr(wb_final))
    wb_account = ctx.call("GET", "/api/accounts")["accounts"][4]
    report.check("取消登录不产生 logged_in=true", wb_account["logged_in"] is not True, repr(wb_account))

    # 8. 过期登录
    expired_login = ctx.call_no_busy("POST", "/api/accounts/dy/login")
    dy_login_id = expired_login["login_id"]

    def _dy_expired():
        data = ctx.call("GET", f"/api/accounts/dy/login/{dy_login_id}")
        return data if _is_login_terminal(str(data["status"])) else None

    dy_final = ctx.poll(_dy_expired, "轮询 dy 登录到 expired", timeout=25.0)
    report.check("二维码过期登录最终状态 expired", dy_final["status"] == "expired", repr(dy_final))
    dy_account = ctx.call("GET", "/api/accounts")["accounts"][1]
    report.check("过期登录不产生 logged_in=true", dy_account["logged_in"] is not True, repr(dy_account))

    # 8.1 need_manual 人工接管（fake manual 场景；kind=sms）：状态透传、不当作失败、完成后 confirmed
    ks_login = ctx.call_no_busy("POST", "/api/accounts/ks/login")
    ks_login_id = ks_login["login_id"]
    ks_statuses: List[str] = []

    def _ks_need_manual():
        data = ctx.call("GET", f"/api/accounts/ks/login/{ks_login_id}")
        ks_statuses.append(str(data.get("status")))
        return data if data.get("status") == "need_manual" else None

    manual_state = ctx.poll(_ks_need_manual, "轮询 ks 登录到 need_manual", timeout=25.0)
    report.check(
        "need_manual 状态透传给 API（不当作失败）",
        manual_state["status"] == "need_manual",
        json.dumps(manual_state, ensure_ascii=False)[:800],
    )
    report.check(
        "need_manual 携带 kind=sms/message/manual_required/need_manual 对象",
        manual_state.get("kind") == "sms"
        and manual_state.get("manual_required") is True
        and isinstance(manual_state.get("message"), str)
        and "验证" in str(manual_state.get("message"))
        and isinstance(manual_state.get("need_manual"), dict)
        and manual_state["need_manual"].get("kind") == "sms"
        and manual_state["need_manual"].get("active") is True,
        json.dumps(manual_state, ensure_ascii=False)[:800],
    )
    report.check(
        "need_manual 期间二维码仍在（qr 非空可继续展示）",
        isinstance(manual_state.get("qr"), dict) and manual_state["qr"].get("seq") is not None,
        repr(manual_state.get("qr"))[:300],
    )

    def _ks_recovered():
        data = ctx.call("GET", f"/api/accounts/ks/login/{ks_login_id}")
        ks_statuses.append(str(data.get("status")))
        return data if data.get("status") in ("waiting_scan", "waiting_qr") else None

    ctx.poll(_ks_recovered, "轮询人工验证完成后 waiting_scan 恢复", timeout=15.0)
    report.check("人工验证完成后 waiting_scan 正常恢复（need_manual 解除）", True)

    def _ks_terminal():
        data = ctx.call("GET", f"/api/accounts/ks/login/{ks_login_id}")
        ks_statuses.append(str(data.get("status")))
        return data if _is_login_terminal(str(data.get("status"))) else None

    ks_final = ctx.poll(_ks_terminal, "轮询 ks 人工接管完成后 confirmed", timeout=25.0)
    report.check(
        "人工接管完成后状态 confirmed（need_manual 不作为失败）",
        ks_final["status"] == "confirmed",
        json.dumps(ks_final, ensure_ascii=False)[:600],
    )
    report.check(
        "人工接管期间状态未变成 failed/expired",
        "failed" not in ks_statuses and "expired" not in ks_statuses,
        str(ks_statuses),
    )
    ks_account = ctx.call("GET", "/api/accounts")["accounts"][2]
    report.check(
        "人工接管登录确认后 ks logged_in=true",
        ks_account["logged_in"] is True,
        repr(ks_account),
    )

    # 8.2 老版本 engine 的 need_manual 不带 kind：sidecar 从 message 兜底推断（bili=manual_nokind）
    bili_login = ctx.call_no_busy("POST", "/api/accounts/bili/login")
    bili_login_id = bili_login["login_id"]

    def _bili_need_manual():
        data = ctx.call("GET", f"/api/accounts/bili/login/{bili_login_id}")
        return data if data.get("status") == "need_manual" else None

    bili_manual_state = ctx.poll(_bili_need_manual, "轮询 bili 登录到 need_manual", timeout=25.0)
    report.check(
        "engine 未带 kind 时按 message 兜底推断为 sms",
        bili_manual_state.get("kind") == "sms"
        and isinstance(bili_manual_state.get("need_manual"), dict),
        json.dumps(bili_manual_state, ensure_ascii=False)[:600],
    )
    ctx.call("POST", f"/api/accounts/bili/login/{bili_login_id}/cancel")
    report.check("need_manual 会话可取消（取消登录接口可用）", True)

    # 9. 测试连接（同步）
    test_result = ctx.call_no_busy("POST", "/api/accounts/xhs/test")
    report.check(
        "POST /api/accounts/xhs/test 返回 {ok,message,duration_ms}",
        test_result.get("ok") is True
        and isinstance(test_result.get("message"), str)
        and isinstance(test_result.get("duration_ms"), int),
        repr(test_result),
    )
    report.check("测试连接 ok=true 后账号仍为已登录", ctx.call("GET", "/api/accounts")["accounts"][0]["logged_in"] is True)
    fake_test_args = read_fake_engine_args(ctx.data_dir, "test", "xhs")
    report.check(
        "test 子进程实际收到 --headless true（保持无头）",
        isinstance(fake_test_args, dict) and str(fake_test_args.get("headless")).lower() == "true",
        repr(fake_test_args),
    )

    # 9.1 测试连接并发语义：不同平台可并行、同平台串行；run 仍全局保守
    # FAKE_TEST_SLEEP=1.2 让每个 test 模拟真实耗时（引擎实际 test 也要 6~15s）
    def _post_test(platform: str):
        started = time.monotonic()
        result = ctx.call_no_busy("POST", f"/api/accounts/{platform}/test", timeout=30.0)
        return result, time.monotonic() - started

    parallel_started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        dy_future = pool.submit(_post_test, "dy")
        time.sleep(0.3)  # 等 dy 进入「检测中」
        # 同平台并发第二个 → 409 BUSY（平台检测文案）
        status_same, data_same, text_same = ctx.request("POST", "/api/accounts/dy/test")
        # run 仍全局保守：任意 test 进行中 → 409 全局文案
        status_run, data_run, _ = ctx.request(
            "POST",
            "/api/runs",
            body={"keywords": ["并发检测"], "platforms": ["xhs"], "max_notes": 1},
        )
        # 不同平台第三个 test 可与 dy 并发
        ks_future = pool.submit(_post_test, "ks")
        dy_test, dy_elapsed = dy_future.result(timeout=60)
        ks_test, ks_elapsed = ks_future.result(timeout=60)
    parallel_elapsed = time.monotonic() - parallel_started
    report.check(
        "不同平台 test 并发且都拿到真实结果（各自耗时≥1s）",
        dy_test.get("ok") is True
        and ks_test.get("ok") is True
        and int(dy_test.get("duration_ms") or 0) >= 1000
        and int(ks_test.get("duration_ms") or 0) >= 1000,
        repr({"dy": dy_test, "ks": ks_test}),
    )
    report.check(
        "两个 test 真实重叠（wall < 串行耗时之和的 85%，证明非全局串行）",
        parallel_elapsed < (dy_elapsed + ks_elapsed) * 0.85,
        f"wall={parallel_elapsed:.2f}s dy={dy_elapsed:.2f}s ks={ks_elapsed:.2f}s",
    )
    same_error = (data_same or {}).get("error") or {}
    report.check(
        "同平台并发第二个 test 返回 409 互斥（BUSY/ACCOUNT_BUSY + 平台/账号占用文案）",
        status_same == 409
        and same_error.get("code") in ("BUSY", "ACCOUNT_BUSY")
        and any(
            token in str(same_error.get("message") or "")
            for token in ("该平台", "该账号", "正在", "占用")
        ),
        f"status={status_same} body={text_same[:300]}",
    )
    run_error = (data_run or {}).get("error") or {}
    report.check(
        "test 进行中创建 run 返回 409 互斥（全局文案）",
        status_run == 409
        and run_error.get("code") in ("BUSY", "ACCOUNT_BUSY")
        and any(
            token in str(run_error.get("message") or "")
            for token in ("登录/采集", "登录", "采集", "任务")
        ),
        f"status={status_run} body={json.dumps(data_run, ensure_ascii=False)[:300]}",
    )

    # 9.2 login 与 test 按平台解耦（修复“检测中不能登录其它平台”的 0s BUSY）
    # 场景 A：xhs test 进行中 → login xhs 409（该平台正在检测）；login dy 允许（200 starting）
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        xhs_test_future = pool.submit(_post_test, "xhs")
        time.sleep(0.3)  # 等 xhs 进入「检测中」
        status_x_login, data_x_login, text_x_login = ctx.request(
            "POST", "/api/accounts/xhs/login"
        )
        status_dy_login, data_dy_login, text_dy_login = ctx.request(
            "POST", "/api/accounts/dy/login"
        )
        xhs_test, _xhs_elapsed = xhs_test_future.result(timeout=60)
    x_login_err = (data_x_login or {}).get("error") or {}
    report.check(
        "xhs test 进行中 → login xhs 返回 409 互斥（平台/账号占用文案）",
        status_x_login == 409
        and x_login_err.get("code") in ("BUSY", "ACCOUNT_BUSY")
        and any(
            token in str(x_login_err.get("message") or "")
            for token in ("该平台", "该账号", "正在", "占用")
        ),
        f"status={status_x_login} body={text_x_login[:300]}",
    )
    report.check(
        "xhs test 进行中 → login dy 允许（200 starting，不被检测阻塞）",
        status_dy_login == 200
        and (data_dy_login or {}).get("status") == "starting"
        and isinstance((data_dy_login or {}).get("login_id"), str),
        f"status={status_dy_login} body={text_dy_login[:300]}",
    )
    report.check(
        "xhs test 与 dy login 并行期间 xhs test 仍返回真实结果",
        xhs_test.get("ok") is True and int(xhs_test.get("duration_ms") or 0) >= 1000,
        repr(xhs_test),
    )
    dy_login_id_a = (data_dy_login or {}).get("login_id")
    if dy_login_id_a:
        ctx.call("POST", f"/api/accounts/dy/login/{dy_login_id_a}/cancel")

        def _dy_a_terminal():
            data = ctx.call("GET", f"/api/accounts/dy/login/{dy_login_id_a}")
            return data if _is_login_terminal(str(data.get("status"))) else None

        ctx.poll(_dy_a_terminal, "轮询场景 A 的 dy 登录会话结束", timeout=20.0)

    # 场景 B：login dy 进行中 → test dy 409（该平台正在登录）；test xhs 允许（不 BUSY）
    start_dy = ctx.call_no_busy("POST", "/api/accounts/dy/login")
    dy_login_id_b = start_dy["login_id"]
    status_dy_test, data_dy_test, text_dy_test = ctx.request("POST", "/api/accounts/dy/test")
    dy_test_err = (data_dy_test or {}).get("error") or {}
    report.check(
        "login dy 进行中 → test dy 返回 409 互斥（平台/账号占用文案）",
        status_dy_test == 409
        and dy_test_err.get("code") in ("BUSY", "ACCOUNT_BUSY")
        and any(
            token in str(dy_test_err.get("message") or "")
            for token in ("该平台", "该账号", "正在", "占用")
        ),
        f"status={status_dy_test} body={text_dy_test[:300]}",
    )
    cross_test = ctx.call_no_busy("POST", "/api/accounts/xhs/test", timeout=30.0)
    report.check(
        "login dy 进行中 → test xhs 允许（200，返回真实结果）",
        cross_test.get("ok") is True and int(cross_test.get("duration_ms") or 0) >= 1000,
        repr(cross_test),
    )
    ctx.call("POST", f"/api/accounts/dy/login/{dy_login_id_b}/cancel")

    # v2 口径（0.5 起）：没有登录态的账号不再进调度（legacy 兜底只对旧数据生效）。
    # dy 这里先走一次「测试连接」拿到登录态，让本用例继续覆盖「单平台失败但任务继续」的链路
    # （该平台失败由 fake engine 的 FAIL_ONCE 触发）。
    dy_ready = ctx.call_no_busy("POST", "/api/accounts/dy/test")
    report.check(
        "调度前置：dy 测试连接成功（未登录账号不参与调度）",
        dy_ready.get("ok") is True,
        repr(dy_ready)[:300],
    )

    # 10. 创建采集任务（xhs 成功 + dy 失败 → 串行继续，最终 run=done）
    create_body = {
        "keywords": ["示例科技", "舆情监测"],
        "platforms": ["xhs", "dy"],
        "max_notes": 5,
        "comments": True,
        "max_comments": 10,
        "name": "selftest",
    }
    created = ctx.call_no_busy("POST", "/api/runs", body=create_body)
    report.check(
        "POST /api/runs 返回 {run_id,status:running}",
        created.get("status") == "running" and isinstance(created.get("run_id"), str),
        repr(created),
    )
    run_id = created["run_id"]
    detail = ctx.call("GET", f"/api/runs/{run_id}")
    report.check(
        "GET /api/runs/{run_id} 字段符合契约（旧字段为子集，允许账号池追加字段）",
        {"run_id", "status", "created_at", "finished_at", "params", "platforms", "excel_path"}.issubset(
            set(detail.keys())
        )
        and detail["status"] == "running"
        and detail["excel_path"] is None
        and [item["platform"] for item in detail["platforms"]] == ["xhs", "dy"]
        and all(
            {
                "platform", "label", "status", "progress", "stage",
                "saved_items", "message", "started_at", "finished_at",
            }.issubset(set(item.keys()))
            for item in detail["platforms"]
        ),
        json.dumps(detail, ensure_ascii=False)[:1200],
    )
    report.check(
        "任务详情 params 回显请求参数",
        detail["params"].get("keywords") == create_body["keywords"]
        and detail["params"].get("max_notes") == 5,
        repr(detail["params"]),
    )

    def _run_finished():
        data = ctx.call("GET", f"/api/runs/{run_id}")
        return data if data["status"] in ("done", "failed", "cancelled") else None

    run_detail = ctx.poll(_run_finished, "轮询采集任务结束", timeout=90.0)
    report.check(
        "部分平台失败时 run 最终仍为 done（契约 §2.2）",
        run_detail["status"] == "done",
        repr(run_detail["status"]),
    )
    platform_map = {item["platform"]: item for item in run_detail["platforms"]}
    report.check(
        "xhs 平台采集成功：done/progress=100/saved_items>=1",
        platform_map["xhs"]["status"] == "done"
        and platform_map["xhs"]["progress"] == 100
        and platform_map["xhs"]["saved_items"] >= 1,
        repr(platform_map.get("xhs")),
    )
    report.check(
        "dy 平台失败但任务继续：failed 且 message 来自 engine",
        platform_map["dy"]["status"] == "failed" and "模拟采集失败" in platform_map["dy"]["message"],
        repr(platform_map.get("dy")),
    )
    report.check("平台 label 已返回中文名", platform_map["xhs"]["label"] == "小红书", repr(platform_map["xhs"]["label"]))
    report.check(
        "run 详情记录平台 started_at/finished_at",
        isinstance(platform_map["xhs"]["started_at"], int)
        and isinstance(platform_map["xhs"]["finished_at"], int),
        repr(platform_map["xhs"]),
    )

    # 11. 日志
    logs = ctx.call("GET", f"/api/runs/{run_id}/logs?tail=200")
    lines = logs.get("lines") or []
    report.check(
        "GET /api/runs/{run_id}/logs 返回 {lines:[{ts,level,text}]}",
        isinstance(lines, list)
        and len(lines) > 0
        and all(
            isinstance(item, dict)
            and isinstance(item.get("ts"), int)
            and isinstance(item.get("level"), str)
            and isinstance(item.get("text"), str)
            for item in lines
        ),
        f"lines={len(lines)}",
    )
    all_text = "\n".join(item["text"] for item in lines)
    report.check(
        "原始引擎日志（中文）被落盘并可通过 API 读取",
        "FakeEngine" in all_text or "模拟" in all_text or "采集" in all_text,
        all_text[:500],
    )
    # 11.1 增量日志（M5a）：环形缓冲 + after_line（运行中走内存 sink，不走文件回退）
    live_tail = ctx.call("GET", f"/api/runs/{run_id}/logs?tail=3")
    live_next = live_tail.get("next_line")
    live_after = ctx.call(
        "GET", f"/api/runs/{run_id}/logs?after_line={max(0, int(live_next or 0) - 2)}"
    )
    report.check(
        "运行中 logs 增量：after_line 只返回该行之后的新行 + next_line 不回退",
        isinstance(live_next, int)
        and live_next >= 1
        and isinstance(live_after.get("lines"), list)
        and all(isinstance(item.get("text"), str) for item in live_after["lines"])
        and live_after.get("next_line") == live_next
        and len(live_after["lines"]) <= 2,
        f"next_line={live_next} after={json.dumps(live_after, ensure_ascii=False)[:300]}",
    )

    # 12. 任务列表
    runs = ctx.call("GET", "/api/runs").get("runs") or []
    report.check(
        "GET /api/runs 能列出刚完成的任务",
        any(item.get("run_id") == run_id for item in runs),
        repr([item.get("run_id") for item in runs])[:500],
    )
    summary = next(item for item in runs if item.get("run_id") == run_id)
    report.check(
        "run 摘要包含 run_id/status/created_at/keywords/platforms/excel_path",
        {"run_id", "status", "created_at", "keywords", "platforms", "excel_path"}.issubset(summary.keys()),
        repr(summary),
    )
    report.check(
        "run 摘要 platforms 与详情同构（desktop renderer 可直接复用）",
        isinstance(summary.get("params"), dict)
        and isinstance(summary.get("platforms"), list)
        and len(summary["platforms"]) == 2
        and all(
            isinstance(item, dict) and "status" in item and "saved_items" in item
            for item in summary["platforms"]
        ),
        repr(summary)[:800],
    )

    # 13. 导出 / 预览：真实 exporter 未就绪时断言 503；就绪时做集成校验（失败仅 WARN）
    if ctx.real_exporter:
        status, data, text = ctx.request("POST", f"/api/runs/{run_id}/export")
        if status == 200 and isinstance(data, dict) and data.get("ok") is True:
            file_path = Path(str(data.get("file_path") or ""))
            report.check(
                "真实 exporter 导出：文件存在且 sheets/total_rows 合法",
                file_path.is_file()
                and isinstance(data.get("sheets"), list)
                and int(data.get("total_rows") or 0) >= 1,
                f"status={status} body={text[:600]}",
            )
            report.check(
                "导出后 run.excel_path 回写",
                ctx.call("GET", f"/api/runs/{run_id}").get("excel_path") == str(data.get("file_path")),
                text[:300],
            )
            result["real_excel_path"] = str(data.get("file_path"))
        else:
            report.warn(f"真实 exporter 导出未通过（不阻塞 sidecar 自测）：{text[:500]}")
        status, data, text = ctx.request("GET", f"/api/runs/{run_id}/data?limit=5")
        if status == 200 and isinstance(data, dict) and isinstance(data.get("sheets"), list):
            report.check(
                "真实 exporter 预览返回 sheets[{name,columns,rows}]",
                all(
                    isinstance(sheet.get("name"), str)
                    and isinstance(sheet.get("columns"), list)
                    and isinstance(sheet.get("rows"), list)
                    for sheet in data["sheets"]
                ),
                text[:600],
            )
        else:
            report.warn(f"真实 exporter 预览未通过：{text[:500]}")
    else:
        report.skip("真实 exporter 未就绪：导出集成校验跳过（阶段 2 用 _fake_exporter 验证 sidecar 导出链路）")
        ctx.call_error(
            "POST", f"/api/runs/{run_id}/export", expect_status=503, expect_code="EXPORTER_UNAVAILABLE"
        )
        report.check("exporter 未就绪时导出返回 503 EXPORTER_UNAVAILABLE", True)
        ctx.call_error(
            "GET", f"/api/runs/{run_id}/data", expect_status=503, expect_code="EXPORTER_UNAVAILABLE"
        )
        report.check("exporter 未就绪时预览返回 503 EXPORTER_UNAVAILABLE", True)

    # 14. open-file 安全边界
    ctx.call_error(
        "POST",
        "/api/system/open-file",
        expect_status=403,
        expect_code="FORBIDDEN",
        body={"path": str(ctx.data_dir / "app.db")},
    )
    report.check("open-file 拒绝 data-dir 根目录文件（403 FORBIDDEN）", True)
    ctx.call_error(
        "POST",
        "/api/system/open-file",
        expect_status=403,
        expect_code="FORBIDDEN",
        body={"path": str(ctx.data_dir / ".." / "outside.xlsx")},
    )
    report.check("open-file 拒绝越界相对路径（403 FORBIDDEN）", True)
    ctx.call_error(
        "POST",
        "/api/system/open-file",
        expect_status=404,
        expect_code="FILE_NOT_FOUND",
        body={"path": str(ctx.data_dir / "exports" / "not_exists.xlsx")},
    )
    report.check("open-file 对 exports 下不存在文件返回 404 FILE_NOT_FOUND", True)

    # 15. 停止采集 + 互斥
    slow_body = {
        "keywords": ["停止测试"],
        "platforms": ["xhs", "dy"],
        "max_notes": 3,
        "comments": False,
        "max_comments": 0,
    }
    slow_run = ctx.call_no_busy("POST", "/api/runs", body=slow_body)
    slow_run_id = slow_run["run_id"]
    report.check("慢速任务创建返回 running", slow_run["status"] == "running", repr(slow_run))

    def _slow_running():
        data = ctx.call("GET", f"/api/runs/{slow_run_id}")
        return data if data["platforms"][0]["status"] == "running" else None

    ctx.poll(_slow_running, "轮询慢速任务进入 running", timeout=20.0)
    ctx.call_error_any(
        "POST", "/api/runs", expect_status=409, expect_codes=("BUSY", "ACCOUNT_BUSY"), body=create_body
    )
    report.check("采集进行中再创建任务返回 409 互斥（BUSY/ACCOUNT_BUSY）", True)
    ctx.call_error_any(
        "POST", "/api/accounts/xhs/login", expect_status=409, expect_codes=("BUSY", "ACCOUNT_BUSY")
    )
    report.check("采集进行中再发起登录返回 409 互斥（BUSY/ACCOUNT_BUSY）", True)
    ctx.call_error_any(
        "POST", "/api/accounts/ks/test", expect_status=409, expect_codes=("BUSY", "ACCOUNT_BUSY")
    )
    report.check("采集进行中 test 返回 409 互斥（BUSY/ACCOUNT_BUSY）", True)
    ctx.call_error("POST", f"/api/runs/{slow_run_id}/export", expect_status=409, expect_code="RUN_RUNNING")
    report.check("运行中导出返回 409 RUN_RUNNING", True)
    ctx.call_error_any(
        "DELETE", "/api/accounts/xhs", expect_status=409, expect_codes=("BUSY", "ACCOUNT_BUSY")
    )
    report.check("采集进行中删除登录态返回 409 互斥（BUSY/ACCOUNT_BUSY）", True)

    stopped = ctx.call("POST", f"/api/runs/{slow_run_id}/stop")
    report.check("POST /api/runs/{id}/stop 返回 {ok:true}", stopped.get("ok") is True, repr(stopped))

    def _slow_cancelled():
        data = ctx.call("GET", f"/api/runs/{slow_run_id}")
        return data if data["status"] in ("cancelled", "done", "failed") else None

    slow_final = ctx.poll(_slow_cancelled, "轮询慢速任务停止", timeout=30.0)
    report.check("停止后任务状态 cancelled", slow_final["status"] == "cancelled", repr(slow_final["status"]))
    slow_platform = slow_final["platforms"][0]
    report.check(
        "停止后当前平台标记 failed/已取消",
        slow_platform["status"] == "failed" and "取消" in slow_platform["message"],
        repr(slow_platform),
    )
    skipped_platform = slow_final["platforms"][1]
    report.check(
        "停止后未执行平台标记 skipped",
        skipped_platform["status"] == "skipped" and "取消" in skipped_platform["message"],
        repr(skipped_platform),
    )
    report.check("重复 stop 幂等返回 ok", ctx.call("POST", f"/api/runs/{slow_run_id}/stop").get("ok") is True)

    # 16. 清除登录态
    deleted = ctx.call_no_busy("DELETE", "/api/accounts/dy")
    report.check("DELETE /api/accounts/dy 返回 {ok:true}", deleted.get("ok") is True, repr(deleted))
    dy_account = ctx.call("GET", "/api/accounts")["accounts"][1]
    report.check(
        "清除登录态后 dy logged_in=false 且 profile_exists=false",
        dy_account["logged_in"] is False and dy_account["profile_exists"] is False,
        repr(dy_account),
    )

    # 16.5 断点续采 resume：同 run 只续跑未完成平台
    import state as state_mod

    # v2 口径（0.5 起）：冷却中 / 未登录的账号不会被调度（legacy 兜底只对旧数据生效）。
    # 前面的「停止任务」用例会把 xhs 打进 30s 冷却，这里先各做一次「测试连接」清冷却并确认登录态，
    # 让本用例聚焦「续跑未完成平台」本身。
    for _platform in ("xhs", "dy"):
        _reset = ctx.call_no_busy("POST", f"/api/accounts/{_platform}/test")
        report.check(
            f"续采前置：{_platform} 测试连接成功（清冷却/登录态）",
            _reset.get("ok") is True,
            repr(_reset)[:200],
        )

    resume_create_body = {
        "keywords": ["续采测试"],
        "platforms": ["xhs", "dy", "ks"],
        "max_notes": 3,
        "comments": False,
        "max_comments": 0,
        "name": "resume-selftest",
    }
    run3 = ctx.call_no_busy("POST", "/api/runs", body=resume_create_body)
    rid3 = run3["run_id"]

    def _run3_finished():
        data = ctx.call("GET", f"/api/runs/{rid3}")
        return data if data["status"] in ("done", "failed", "cancelled") else None

    detail3 = ctx.poll(_run3_finished, "轮询续采测试 run 首次结束", timeout=90.0)
    p3 = {item["platform"]: item for item in detail3["platforms"]}
    report.check(
        "续采测试 run 首跑：xhs done / dy failed（FAIL_ONCE 第 2 次）/ ks done",
        p3["xhs"]["status"] == "done"
        and p3["dy"]["status"] == "failed"
        and p3["ks"]["status"] == "done",
        repr({key: item["status"] for key, item in p3.items()}),
    )
    # 直接改 DB 模拟「程序异常关闭」：ks=skipped、run=failed；xhs saved=10、dy saved=7 作为续采基线
    store = state_mod.StateStore(ctx.data_dir / "app.db")
    try:
        store.update_platform(rid3, "ks", status="skipped", message="模拟异常关闭，未执行", finished_at=state_mod.now_ts())
        store.update_platform(rid3, "xhs", saved_items=10, message="异常关闭前已采 10 条")
        store.update_platform(rid3, "dy", saved_items=7, message="已采 7 条后中断")
        store.update_run(rid3, status="failed", finished_at=state_mod.now_ts(), error="部分平台失败")
        run_raw_before = store.get_run(rid3)
    finally:
        store.close()
    started_ts_before = int(run_raw_before["started_ts"])
    before_map = {item["platform"]: item for item in run_raw_before["platforms"]}
    report.check(
        "续采前模拟状态：xhs done(saved=10) / dy failed(saved=7) / ks skipped",
        before_map["xhs"]["status"] == "done"
        and int(before_map["xhs"]["saved_items"]) == 10
        and before_map["dy"]["status"] == "failed"
        and before_map["ks"]["status"] == "skipped",
        repr({key: (item["status"], item["saved_items"]) for key, item in before_map.items()}),
    )

    # A) 显式 platforms=["dy"] 续采：只跑 dy；其他平台历史失败 → run=failed，便于再次续采
    # run3 首跑里 dy 失败会进 30s 冷却；v2 账号在冷却期内不进调度（legacy 曾无视冷却）。
    # 续采前先清一次冷却（测试连接成功 = 登录态 ok + cooldown 清零），否则 resume 会被挡在选号阶段。
    _reset_dy = ctx.call_no_busy("POST", "/api/accounts/dy/test")
    report.check("续采前置：dy 清冷却成功", _reset_dy.get("ok") is True, repr(_reset_dy)[:160])

    resume_a = ctx.call("POST", f"/api/runs/{rid3}/resume", body={"platforms": ["dy"]})
    report.check(
        "resume（显式 platforms=['dy']）返回 running 且 resumed_platforms=['dy']",
        resume_a.get("status") == "running"
        and resume_a.get("run_id") == rid3
        and resume_a.get("resumed_platforms") == ["dy"],
        repr(resume_a),
    )
    # 并发续采：真并发时必须是 409 RUN_BUSY；但 fake engine 很快，首次续采可能已经结束，
    # 此时允许 200——关键不变量是「已 done 的平台不会被再次续跑」。两种结果都视为通过，分别断言。
    dup_status, dup_data, dup_text = ctx.request(
        "POST", f"/api/runs/{rid3}/resume", body={"platforms": ["dy"]}
    )
    if dup_status == 409:
        dup_code = (dup_data.get("error") or {}).get("code")
        report.check(
            "续采进行中再次 resume 返回 409 RUN_BUSY",
            dup_code == "RUN_BUSY",
            f"{dup_code}/{dup_text[:160]}",
        )
    else:
        report.check(
            "续采已结束再次 resume 允许 200，且不重复跑已完成平台",
            dup_status == 200 and "xhs" not in (dup_data.get("resumed_platforms") or []),
            f"status={dup_status} body={dup_text[:200]}",
        )

    def _run3_not_running():
        data = ctx.call("GET", f"/api/runs/{rid3}")
        return data if data["status"] != "running" else None

    after_a = ctx.poll(_run3_not_running, "轮询 dy 续采结束", timeout=60.0)
    pa = {item["platform"]: item for item in after_a["platforms"]}
    report.check(
        "显式续采 dy：dy done、xhs 未变(saved=10)、ks 仍 skipped、run=failed（可继续续采）",
        pa["dy"]["status"] == "done"
        and pa["xhs"]["status"] == "done"
        and int(pa["xhs"]["saved_items"]) == 10
        and pa["xhs"]["started_at"] == before_map["xhs"]["started_at"]
        and pa["ks"]["status"] == "skipped"
        and after_a["status"] == "failed",
        json.dumps(
            {key: {k: item[k] for k in ("status", "saved_items", "started_at")} for key, item in pa.items()},
            ensure_ascii=False,
        )[:700],
    )
    report.check(
        "续采保留 saved_items 基线（dy 从 7 续采不倒退）",
        int(pa["dy"]["saved_items"]) >= 7,
        f"dy saved_items={pa['dy']['saved_items']}",
    )

    # B) 默认（不传 body / 不传 platforms）→ 只续跑剩余未完成平台 ks → 全 run done
    status_b, data_b, text_b = ctx.request("POST", f"/api/runs/{rid3}/resume")
    report.check(
        "resume 不传 body（默认续跑未完成平台）→ 200 且 resumed_platforms=['ks']",
        status_b == 200 and (data_b or {}).get("resumed_platforms") == ["ks"],
        f"status={status_b} body={text_b[:300]}",
    )
    after_b = ctx.poll(_run3_not_running, "轮询 ks 续采结束", timeout=60.0)
    pb = {item["platform"]: item for item in after_b["platforms"]}
    report.check(
        "默认续采后全 run done（xhs/dy/ks 全 done）",
        after_b["status"] == "done" and all(item["status"] == "done" for item in after_b["platforms"]),
        repr({key: item["status"] for key, item in pb.items()}),
    )
    store = state_mod.StateStore(ctx.data_dir / "app.db")
    try:
        run_raw_after = store.get_run(rid3)
    finally:
        store.close()
    report.check(
        "续采复用同一 run_id 与原始 started_ts",
        run_raw_after["run_id"] == rid3
        and int(run_raw_after["started_ts"]) == started_ts_before,
        f"run_id={run_raw_after['run_id']} started_ts={run_raw_after['started_ts']} (原 {started_ts_before})",
    )
    log_text = (ctx.data_dir / "logs" / f"run-{rid3}.log").read_text(encoding="utf-8", errors="replace")
    report.check(
        "续采只重跑选中平台（日志：xhs 1 次、dy 2 次、ks 2 次）",
        log_text.count("开始采集 platform=xhs") == 1
        and log_text.count("开始采集 platform=dy") == 2
        and log_text.count("开始采集 platform=ks") == 2,
        f"xhs={log_text.count('开始采集 platform=xhs')} dy={log_text.count('开始采集 platform=dy')} ks={log_text.count('开始采集 platform=ks')}",
    )

    # C) 全 run 已 done → 再 resume 409 NO_PENDING_PLATFORMS；未知平台 422；未知 run 404
    ctx.call_error("POST", f"/api/runs/{rid3}/resume", expect_status=409, expect_code="NO_PENDING_PLATFORMS")
    report.check("全 run done 后 resume 返回 409 NO_PENDING_PLATFORMS", True)
    ctx.call_error(
        "POST",
        f"/api/runs/{rid3}/resume",
        expect_status=422,
        expect_code="INVALID_PLATFORM",
        body={"platforms": ["not_a_platform_in_run"]},
    )
    report.check("resume 显式未知平台返回 422 INVALID_PLATFORM", True)
    ctx.call_error("POST", "/api/runs/run_not_exists/resume", expect_status=404, expect_code="RUN_NOT_FOUND")
    report.check("resume 未知 run 返回 404 RUN_NOT_FOUND", True)

    # D) 续采后导出：包含首批已完成平台数据 + 续采平台数据（同一 started_ts 口径）
    if ctx.real_exporter:
        exported3 = ctx.call("POST", f"/api/runs/{rid3}/export")
        fp3 = Path(str(exported3.get("file_path") or ""))
        sheets3 = {item["name"]: int(item.get("rows") or 0) for item in exported3.get("sheets") or []}
        report.check(
            "续采后导出包含首批已完成平台 + 续采平台数据（小红书>0 且 抖音>0）",
            fp3.is_file() and sheets3.get("小红书", 0) > 0 and sheets3.get("抖音", 0) > 0,
            f"sheets={sheets3} file={fp3.name}",
        )
    else:
        report.skip("真实 exporter 未就绪：续采导出验证跳过")

    # 17. 错误体 / 404
    ctx.call_error(
        "GET",
        "/api/accounts/unknown_platform/login/login_x",
        expect_status=404,
        expect_code="PLATFORM_NOT_FOUND",
    )
    report.check("未知平台返回 404 PLATFORM_NOT_FOUND", True)
    ctx.call_error("GET", "/api/runs/run_not_exists", expect_status=404, expect_code="RUN_NOT_FOUND")
    report.check("未知 run 返回 404 RUN_NOT_FOUND", True)
    ctx.call_error(
        "GET",
        "/api/accounts/xhs/login/login_not_exists",
        expect_status=404,
        expect_code="LOGIN_NOT_FOUND",
    )
    report.check("未知 login_id 返回 404 LOGIN_NOT_FOUND", True)
    ctx.call_error(
        "POST",
        "/api/runs",
        expect_status=400,
        expect_code="INVALID_PARAMS",
        body={"keywords": [], "platforms": ["xhs"], "max_notes": 5},
    )
    report.check("空 keywords 返回 400 INVALID_PARAMS", True)
    ctx.call_error(
        "POST",
        "/api/runs",
        expect_status=400,
        expect_code="INVALID_PLATFORM",
        body={"keywords": ["a"], "platforms": ["not_a_platform"], "max_notes": 5},
    )
    report.check("非法平台返回 400 INVALID_PLATFORM", True)

    # 18. CORS：OPTIONS 预检免鉴权
    resp = ctx.client.request(
        "OPTIONS",
        ctx.base_url + "/api/config",
        headers={
            "Origin": "file://",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    header_names = {key.lower() for key in resp.headers.keys()}
    report.check(
        "CORS 预检请求返回 2xx 且带 access-control-allow-origin",
        200 <= resp.status_code < 300 and "access-control-allow-origin" in header_names,
        f"status={resp.status_code} headers={dict(resp.headers)}",
    )

    # 19. 报告中心：用户模型 API 直连（fake LLM）+ fake report_data，本地模板渲染
    fake_llm = ctx.llm_server

    def _report_wait(report_id: str, timeout: float = 40.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: Dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = ctx.call("GET", f"/api/reports/{report_id}")
            if last.get("status") in ("completed", "degraded", "failed"):
                return last
            time.sleep(0.2)
        raise SelfTestFailure(f"等待报告生成超时：{report_id} status={last.get('status')}")

    fake_llm.reset_records()
    report_cfg = ctx.call("GET", "/api/reports/config")
    report.check(
        "GET /api/reports/config：configured/provider_name/model/base_url 脱敏 + report_data 就绪",
        report_cfg.get("configured") is True
        and report_cfg.get("report_data_ready") is True
        and report_cfg.get("provider_name") == "selftest-fake-provider"
        and report_cfg.get("model") == "fake-model"
        and str(report_cfg.get("base_url") or "").startswith("http://127.0.0.1:")
        and report_cfg.get("degraded_fallback") is True
        and "selftest-fake-key" not in json.dumps(report_cfg, ensure_ascii=False),
        json.dumps(report_cfg, ensure_ascii=False)[:700],
    )
    caps = ctx.call("GET", "/api/reports/capabilities")
    report.check(
        "GET /api/reports/capabilities 测试默认 provider（ok/model/elapsed_ms）",
        caps.get("ok") is True
        and caps.get("model") == "fake-model"
        and isinstance(caps.get("elapsed_ms"), int)
        and (caps.get("capabilities") or {}).get("narrative", {}).get("endpoint")
        == "local-llm",
        json.dumps(caps, ensure_ascii=False)[:600],
    )
    connection_ok = ctx.call("GET", "/api/reports/connection")
    report.check(
        "GET /api/reports/connection（已配置）返回 configured/connected/model/provider/local_fallback",
        connection_ok.get("ok") is True
        and connection_ok.get("configured") is True
        and connection_ok.get("connected") is True
        and connection_ok.get("model") == "fake-model"
        and connection_ok.get("provider_name") == "selftest-fake-provider"
        and connection_ok.get("narrative_available") is True
        and connection_ok.get("narrative_endpoint") == "local-llm"
        and connection_ok.get("local_fallback") is True,
        json.dumps(connection_ok, ensure_ascii=False)[:700],
    )
    created_report = ctx.call(
        "POST",
        "/api/reports",
        expect=202,
        body={
            "window_start": "2026-11-01",
            "window_end": "2026-11-09",
            "keywords": ["示例科技"],
            "platforms": ["xhs", "dy"],
        },
    )
    report_id = str(created_report.get("report_id") or "")
    report.check(
        "POST /api/reports 返回 queued + report_id + 请求窗口",
        report_id.startswith("rp_")
        and created_report.get("status") == "queued"
        and created_report.get("window_start") == "2026-11-01"
        and created_report.get("window_end") == "2026-11-09",
        repr(created_report),
    )
    report_final = _report_wait(report_id)
    report.check(
        "报告后台生成完成（completed/degraded、progress=100、v1 html_available）",
        report_final.get("status") in ("completed", "degraded")
        and int(report_final.get("progress") or 0) == 100
        and int(report_final.get("current_version") or 0) == 1
        and report_final.get("html_available") is True
        and len(report_final.get("versions") or []) == 1,
        json.dumps(report_final, ensure_ascii=False)[:800],
    )
    report.check(
        "报告详情 meta（window/keywords/platforms/counts/source/narrative_generated_by）",
        isinstance(report_final.get("title"), str)
        and str(report_final.get("title")).endswith("舆情监测周报")
        and isinstance(report_final.get("meta"), dict)
        and ((report_final["meta"].get("window") or {}).get("start")) == "2026-11-01"
        and report_final["meta"].get("keywords") == ["示例科技"]
        and report_final["meta"].get("platforms") == ["xhs", "dy"]
        and isinstance((report_final["meta"].get("counts") or {}).get("total"), int)
        and report_final["meta"].get("source") == "local-ai"
        and report_final.get("narrative_generated_by") == "local-llm",
        json.dumps(report_final.get("meta"), ensure_ascii=False)[:700],
    )
    report.check(
        "GET /api/reports/{id} 返回 file_path（最新版 HTML 绝对路径）",
        isinstance(report_final.get("file_path"), str)
        and Path(str(report_final["file_path"])).is_file()
        and str(report_final["file_path"]).endswith("v1.html")
        and Path(str(report_final["file_path"])).is_absolute(),
        repr(report_final.get("file_path")),
    )
    narrative_package = fake_llm.narrative_requests[-1] if fake_llm.narrative_requests else {}
    report.check(
        "模型收到报告数据包（report_meta window/keywords + stats）",
        len(fake_llm.narrative_requests) >= 1
        and ((narrative_package.get("report_meta") or {}).get("window") or {}).get("start")
        == "2026-11-01"
        and (narrative_package.get("report_meta") or {}).get("keywords") == ["示例科技"]
        and isinstance(narrative_package.get("stats"), dict),
        json.dumps(narrative_package, ensure_ascii=False)[:600],
    )
    html_path_v1 = ctx.data_dir / "reports" / report_id / "v1.html"
    html_v1_text = html_path_v1.read_text(encoding="utf-8") if html_path_v1.is_file() else ""
    report.check(
        "HTML 落盘 <data-dir>/reports/<id>/v1.html 且由本地模板渲染",
        html_path_v1.is_file()
        and "示例科技" in html_v1_text
        and ("本地模板" in html_v1_text or "section-overview" in html_v1_text)
        and (ctx.data_dir / "reports" / report_id / "narrative_v1.json").is_file(),
        f"bytes={len(html_v1_text)}",
    )
    report.check(
        "报告生成调用了情感打标（fake LLM 每批 ≤50 条）",
        len(fake_llm.sentiment_batches) >= 1
        and all(len(batch) <= 50 for batch in fake_llm.sentiment_batches),
        f"batches={fake_llm.sentiment_batch_sizes()}",
    )
    listed_reports = ctx.call("GET", "/api/reports")["reports"]
    listed_item = next(
        (item for item in listed_reports if item.get("report_id") == report_id), {}
    )
    report.check(
        "GET /api/reports 列表包含新报告且带 file_path",
        any(
            item.get("report_id") == report_id
            and item.get("status") in ("completed", "degraded")
            for item in listed_reports
        )
        and isinstance(listed_item.get("file_path"), str)
        and Path(str(listed_item["file_path"])).is_file(),
        repr(listed_item.get("file_path")),
    )
    html_body = ctx.call("GET", f"/api/reports/{report_id}/html")
    expected_sha = hashlib.sha256(str(html_body.get("html")).encode("utf-8")).hexdigest()
    report.check(
        "GET /api/reports/{id}/html 返回 v1 html 且 sha256/bytes 一致",
        html_body.get("version") == 1
        and "示例科技" in str(html_body.get("html"))
        and html_body.get("html_sha256") == expected_sha
        and int(html_body.get("bytes") or 0) == len(str(html_body.get("html")).encode("utf-8")),
        repr(html_body)[:300],
    )
    opened = ctx.call("POST", f"/api/reports/{report_id}/open")
    report.check(
        "POST /api/reports/{id}/open 返回本地 file_path",
        opened.get("ok") is True and Path(str(opened.get("file_path"))).is_file(),
        repr(opened),
    )
    revise_resp = ctx.call(
        "POST",
        f"/api/reports/{report_id}/revise",
        expect=202,
        body={"instruction": "把情感分布改成环形图，补充下周建议", "base_version": 1},
    )
    report.check(
        "POST /api/reports/{id}/revise 返回 queued + base/target 版本",
        revise_resp.get("report_id") == report_id
        and revise_resp.get("base_version") == 1
        and revise_resp.get("target_version") == 2,
        repr(revise_resp),
    )
    revised_final = _report_wait(report_id)
    report.check(
        "修订后生成 v2（current_version=2、versions 含 v1/v2、v2.html 落盘）",
        int(revised_final.get("current_version") or 0) == 2
        and len(revised_final.get("versions") or []) >= 2
        and (ctx.data_dir / "reports" / report_id / "v2.html").is_file(),
        json.dumps(revised_final, ensure_ascii=False)[:800],
    )
    report.check(
        "修订后 GET detail.file_path 指向 v2 HTML",
        isinstance(revised_final.get("file_path"), str)
        and str(revised_final["file_path"]).endswith("v2.html")
        and Path(str(revised_final["file_path"])).is_file(),
        repr(revised_final.get("file_path")),
    )
    revise_payload = fake_llm.narrative_requests[-1] if fake_llm.narrative_requests else {}
    report.check(
        "修订请求带 instruction + previous_narrative 传给模型",
        revise_payload.get("instruction") == "把情感分布改成环形图，补充下周建议"
        and isinstance(revise_payload.get("previous_narrative"), dict),
        json.dumps(revise_payload, ensure_ascii=False)[:500],
    )
    html_v2 = ctx.call("GET", f"/api/reports/{report_id}/html?version=2")
    report.check(
        "GET /api/reports/{id}/html?version=2 返回 v2 内容（含修订说明）",
        html_v2.get("version") == 2
        and "把情感分布改成环形图，补充下周建议" in str(html_v2.get("html")),
        repr(html_v2)[:300],
    )
    ctx.call_error(
        "GET", "/api/reports/rp_not_exists_selftest", expect_status=404, expect_code="REPORT_NOT_FOUND"
    )
    report.check("未知报告返回 404 REPORT_NOT_FOUND", True)
    # 默认关键词：不传 keywords → DB distinct source_keyword（不硬编码品牌词）
    import sqlite3 as _sqlite3

    media_db_path = ctx.data_dir / "crawler" / "media_crawler.db"
    expected_keywords: List[str] = []
    if media_db_path.is_file():
        _conn = _sqlite3.connect(str(media_db_path))
        try:
            for _table in (
                "xhs_note",
                "douyin_aweme",
                "kuaishou_video",
                "bilibili_video",
                "weibo_note",
                "tieba_note",
                "zhihu_content",
            ):
                try:
                    _rows = _conn.execute(
                        f"SELECT DISTINCT source_keyword FROM {_table} "
                        "WHERE source_keyword IS NOT NULL AND TRIM(source_keyword) <> ''"
                    ).fetchall()
                except _sqlite3.Error:
                    continue
                for (_kw,) in _rows:
                    _text = str(_kw or "").strip()
                    if _text and _text not in expected_keywords:
                        expected_keywords.append(_text)
                if len(expected_keywords) >= 20:
                    break
        finally:
            _conn.close()
    default_created = ctx.call(
        "POST",
        "/api/reports",
        expect=202,
        body={"window_start": "2026-11-01", "window_end": "2026-11-09"},
    )
    report.check(
        "POST /api/reports 不传 keywords → 默认取 DB distinct source_keyword（不硬编码）",
        bool(expected_keywords)
        and default_created.get("keywords") == expected_keywords[:20],
        f"api={default_created.get('keywords')} db={expected_keywords[:20]}",
    )
    default_final = _report_wait(str(default_created.get("report_id")))
    report.check(
        "默认关键词报告生成完成且 HTML 落盘",
        default_final.get("status") in ("completed", "degraded")
        and default_final.get("html_available") is True,
        json.dumps(default_final, ensure_ascii=False)[:500],
    )
    default_package_keywords = (
        (fake_llm.narrative_requests[-1].get("report_meta") or {}).get("keywords")
        if fake_llm.narrative_requests
        else None
    )
    report.check(
        "默认关键词原样传入模型数据包",
        default_package_keywords == expected_keywords[:20],
        repr(default_package_keywords)[:400],
    )

    # 模型调用失败（HTTP 500）→ 本地模板兜底 degraded（不标 failed）
    fake_llm.mode = "fail"
    fallback_created = ctx.call(
        "POST",
        "/api/reports",
        expect=202,
        body={
            "window_start": "2026-11-01",
            "window_end": "2026-11-09",
            "keywords": ["示例科技"],
            "platforms": ["xhs", "dy"],
        },
    )
    fallback_id = str(fallback_created.get("report_id") or "")
    fallback_final = _report_wait(fallback_id)
    fallback_reason = str(fallback_final.get("ai_fallback_reason") or "")
    report.check(
        "模型 API 500 → 本地模板兜底 degraded（不标 failed、source=local-template）",
        fallback_final.get("status") == "degraded"
        and (fallback_final.get("meta") or {}).get("source") == "local-template"
        and fallback_final.get("narrative_generated_by") == "local-template"
        and "模型 API" in fallback_reason
        and fallback_final.get("html_available") is True
        and isinstance(fallback_final.get("file_path"), str)
        and Path(str(fallback_final["file_path"])).is_file(),
        json.dumps(fallback_final, ensure_ascii=False)[:800],
    )
    local_html_path = ctx.data_dir / "reports" / fallback_id / "v1.html"
    local_html = local_html_path.read_text(encoding="utf-8") if local_html_path.is_file() else ""
    report.check(
        "本地模板 HTML 含标题/窗口/数据概览/情感分布/平台说明",
        "本地模板" in local_html
        and "示例科技" in local_html
        and "2026-11-01" in local_html
        and "数据概览" in local_html
        and "情感分布" in local_html
        and "平台分布" in local_html,
        local_html[:400],
    )
    # 模型输出非法 JSON → 本地模板兜底 degraded（原因含 LLM_PARSE）
    fake_llm.mode = "invalid_json"
    invalid_created = ctx.call(
        "POST",
        "/api/reports",
        expect=202,
        body={"window_start": "2026-11-01", "window_end": "2026-11-09", "keywords": ["示例科技"]},
    )
    invalid_final = _report_wait(str(invalid_created.get("report_id")))
    report.check(
        "模型输出非法 JSON → 本地模板兜底 degraded（原因含 LLM_PARSE）",
        invalid_final.get("status") == "degraded"
        and (invalid_final.get("meta") or {}).get("source") == "local-template"
        and "LLM_PARSE" in str(invalid_final.get("ai_fallback_reason") or ""),
        json.dumps(invalid_final, ensure_ascii=False)[:700],
    )
    fake_llm.mode = "ok"
    # 降级报告修订：模型恢复后重新 AI 生成 completed/local-ai，v2 HTML 含修订说明
    local_revise = ctx.call(
        "POST",
        f"/api/reports/{fallback_id}/revise",
        expect=202,
        body={"instruction": "补充下周建议，突出正面事件", "base_version": 1},
    )
    local_revised = _report_wait(fallback_id)
    local_v2_path = ctx.data_dir / "reports" / fallback_id / "v2.html"
    local_v2_html = local_v2_path.read_text(encoding="utf-8") if local_v2_path.is_file() else ""
    report.check(
        "降级报告可修订 v2（模型恢复 → AI 生成 completed/local-ai + 修订说明入 HTML）",
        local_revise.get("target_version") == 2
        and int(local_revised.get("current_version") or 0) == 2
        and local_revised.get("status") in ("completed", "degraded")
        and "补充下周建议" in local_v2_html
        and local_v2_path.is_file(),
        json.dumps(local_revised, ensure_ascii=False)[:700],
    )
    result["report_id"] = report_id

    result["run_id"] = run_id
    result["accounts"] = ctx.call("GET", "/api/accounts")["accounts"]
    return result


# ---------------------------------------------------------------------------
# Phase 2：重启恢复 + 导出链路（_fake_exporter）
# ---------------------------------------------------------------------------
def _craft_interrupted_run(data_dir: Path) -> str:
    """构造一条「上次 sidecar 崩溃残留的 running 任务」。

    上一次 sidecar 是被 ``taskkill /F /T`` 强杀的：Windows 下 ``-wal``/``-shm`` 的句柄与共享内存
    映射释放有延迟，紧接着用新连接把同一份库切到 WAL 会偶发
    ``sqlite3.OperationalError: disk I/O error``（同一份库事后单独打开完全正常，
    ``PRAGMA integrity_check`` 也是 ok）。因此建连做有限次退避重试，避免自测假红。
    """
    import state as state_mod

    store = None
    last_exc: Optional[BaseException] = None
    for attempt in range(5):
        try:
            store = state_mod.StateStore(data_dir / "app.db")
            break
        except sqlite3.OperationalError as exc:  # 仅这一类别重试：强杀后 WAL/SHM 尚未释放
            last_exc = exc
            time.sleep(0.2 * (attempt + 1))
    if store is None:
        raise last_exc if last_exc is not None else RuntimeError("StateStore 打开失败")
    try:
        run_id = "run_interrupted_selftest"
        started_ts = int(time.time())
        store.create_run(
            run_id=run_id,
            params={
                "keywords": ["中断测试"],
                "platforms": ["xhs"],
                "max_notes": 1,
                "comments": False,
                "max_comments": 0,
            },
            keywords=["中断测试"],
            platforms=["xhs"],
            started_ts=started_ts,
            name="interrupted",
        )
        store.update_platform(run_id, "xhs", status="running", started_at=started_ts)
        return run_id
    finally:
        store.close()


def phase2(ctx: Ctx, phase1_result: Dict[str, Any]) -> None:
    report = ctx.report
    run_id = phase1_result["run_id"]
    print("\n[阶段 2] 重启恢复 / 历史任务 / 导出（_fake_exporter）/ open-file")

    # 1. 历史任务可见
    runs = ctx.call("GET", "/api/runs").get("runs") or []
    run_ids = [item.get("run_id") for item in runs]
    report.check("重启后 /api/runs 仍能列出历史任务", run_id in run_ids, repr(run_ids)[:500])
    detail = ctx.call("GET", f"/api/runs/{run_id}")
    report.check(
        "重启后历史任务详情仍为 done 且 xhs=done",
        detail["status"] == "done"
        and {item["platform"]: item["status"] for item in detail["platforms"]}.get("xhs") == "done",
        json.dumps(detail, ensure_ascii=False)[:800],
    )

    # 2. running 残留 → failed/interrupted
    interrupted_id = "run_interrupted_selftest"
    interrupted = ctx.call("GET", f"/api/runs/{interrupted_id}")
    report.check(
        "重启后残留 running 任务置为 failed（error=interrupted）",
        interrupted["status"] == "failed"
        and any(item["platform"] == "xhs" and item["status"] == "failed" for item in interrupted["platforms"]),
        json.dumps(interrupted, ensure_ascii=False)[:800],
    )
    report.check(
        "重启后不存在仍为 running 的历史任务",
        all(item.get("status") != "running" for item in runs),
        repr([(item.get("run_id"), item.get("status")) for item in runs])[:500],
    )

    # 3. 账号登录态持久化（phase1 中 xhs 为已登录）
    accounts = ctx.call("GET", "/api/accounts")["accounts"]
    xhs = accounts[0]
    report.check(
        "重启后 xhs 登录态持久化（logged_in=true）",
        xhs["logged_in"] is True and isinstance(xhs["last_checked_at"], int),
        repr(xhs),
    )

    # 4. 导出链路（注入 _fake_exporter）
    exported = ctx.call("POST", f"/api/runs/{run_id}/export")
    report.check(
        "导出返回 {ok,file_path,sheets,total_rows}",
        exported.get("ok") is True
        and isinstance(exported.get("file_path"), str)
        and isinstance(exported.get("sheets"), list)
        and isinstance(exported.get("total_rows"), int),
        repr(exported)[:800],
    )
    file_path = Path(str(exported["file_path"]))
    report.check("导出文件真实存在", file_path.is_file(), str(file_path))
    report.check(
        "导出文件位于 <data-dir>/exports 下",
        str(file_path.resolve()).startswith(str((ctx.data_dir / "exports").resolve())),
        str(file_path),
    )
    report.check(
        "导出 sheets 每项含 name/rows 且 total_rows>=1",
        len(exported["sheets"]) >= 1
        and all(
            isinstance(sheet.get("name"), str) and isinstance(sheet.get("rows"), int)
            for sheet in exported["sheets"]
        )
        and int(exported["total_rows"]) >= 1,
        repr(exported["sheets"]),
    )
    detail = ctx.call("GET", f"/api/runs/{run_id}")
    report.check(
        "导出后 run.excel_path 记录文件路径",
        detail["excel_path"] == str(exported["file_path"]),
        repr(detail["excel_path"]),
    )
    summary = next(
        (item for item in ctx.call("GET", "/api/runs").get("runs") or [] if item.get("run_id") == run_id),
        {},
    )
    report.check(
        "导出后 run 摘要中的 excel_path 同步更新",
        summary.get("excel_path") == str(exported["file_path"]),
        repr(summary)[:500],
    )
    # 6. 报告中心持久化：重启后 meta.json 可读、本地 HTML 仍可访问
    report_id = (phase1_result or {}).get("report_id")
    if report_id:
        listed_reports = ctx.call("GET", "/api/reports").get("reports") or []
        report.check(
            "重启后 /api/reports 列表仍包含历史报告",
            any(item.get("report_id") == report_id for item in listed_reports),
            repr(listed_reports)[:600],
        )
        report_detail = ctx.call("GET", f"/api/reports/{report_id}")
        report.check(
            "重启后报告详情可读（completed、v2、html_available）",
            report_detail.get("status") in ("completed", "degraded")
            and int(report_detail.get("current_version") or 0) == 2
            and report_detail.get("html_available") is True
            and len(report_detail.get("versions") or []) >= 2,
            json.dumps(report_detail, ensure_ascii=False)[:800],
        )
        report.check(
            "重启后历史报告 title/meta 可读（window/keywords/counts/source）",
            isinstance(report_detail.get("title"), str)
            and str(report_detail.get("title")).endswith("舆情监测周报")
            and isinstance(report_detail.get("meta"), dict)
            and ((report_detail["meta"].get("window") or {}).get("start")) == "2026-11-01"
            and report_detail["meta"].get("keywords") == ["示例科技"]
            and isinstance((report_detail["meta"].get("counts") or {}).get("total"), int)
            and report_detail["meta"].get("source") in ("local-ai", "local-template"),
            json.dumps(report_detail.get("meta"), ensure_ascii=False)[:700],
        )
        report.check(
            "重启后报告 detail.file_path 指向本地 v2 HTML",
            isinstance(report_detail.get("file_path"), str)
            and str(report_detail["file_path"]).endswith("v2.html")
            and Path(str(report_detail["file_path"])).is_file(),
            repr(report_detail.get("file_path")),
        )
        report_html = ctx.call("GET", f"/api/reports/{report_id}/html?version=2")
        report.check(
            "重启后本地报告 HTML 仍可读取（v2）",
            report_html.get("version") == 2
            and len(str(report_html.get("html") or "")) > 1000,
            repr(report_html)[:300],
        )

    # 5. 日志：重启后从 logs/run-<run_id>.log 读取历史日志
    old_logs = ctx.call("GET", f"/api/runs/{run_id}/logs?tail=50")
    old_lines = old_logs.get("lines") or []
    report.check(
        "重启后仍可读取历史任务日志（文件回退）",
        isinstance(old_lines, list)
        and len(old_lines) > 0
        and all(
            isinstance(item.get("ts"), int)
            and isinstance(item.get("level"), str)
            and isinstance(item.get("text"), str)
            for item in old_lines
        ),
        f"lines={len(old_lines)}",
    )

    # 6. 预览
    preview = ctx.call("GET", f"/api/runs/{run_id}/data?limit=5")
    sheets = preview.get("sheets")
    report.check(
        "GET /api/runs/{id}/data 返回 sheets[{name,columns,rows}]",
        isinstance(sheets, list)
        and len(sheets) >= 1
        and all(
            isinstance(sheet.get("name"), str)
            and isinstance(sheet.get("columns"), list)
            and isinstance(sheet.get("rows"), list)
            for sheet in sheets
        ),
        json.dumps(preview, ensure_ascii=False)[:800],
    )

    # 6.1 采集动态内容流 GET /api/runs/{id}/feed（真实读 fake engine 写入的 media_crawler.db）
    feed_status, feed, feed_text = ctx.request("GET", f"/api/runs/{run_id}/feed")
    report.check(
        "GET /api/runs/{id}/feed 返回 200 且响应为 JSON（无 NaN）",
        feed_status == 200 and isinstance(feed, dict) and "NaN" not in feed_text,
        feed_text[:500],
    )
    report.check(
        "GET /api/runs/{id}/feed 契约字段：run_id/status/keywords/counts/items/truncated(+next_seq)",
        set(feed.keys()) == {"run_id", "status", "keywords", "counts", "items", "truncated", "next_seq"}
        and feed.get("run_id") == run_id
        and feed.get("status") == "done"
        and feed.get("keywords") == ["示例科技", "舆情监测"]
        and isinstance(feed.get("counts"), dict)
        and isinstance(feed.get("items"), list)
        and isinstance(feed.get("truncated"), bool)
        and isinstance(feed.get("next_seq"), int),
        json.dumps(feed, ensure_ascii=False)[:900],
    )
    feed_items = feed.get("items") or []
    item_keys = {
        "platform", "platform_label", "kind", "id", "seq", "title", "text", "author", "url",
        "published_at", "collected_at", "metrics", "comment_total", "comments",
    }
    report.check(
        "feed items 非空且每条字段齐全（kind=content，含新增 seq）",
        len(feed_items) >= 1
        and all(
            set(item.keys()) == item_keys and item.get("kind") == "content"
            for item in feed_items
        ),
        json.dumps(feed_items[:1], ensure_ascii=False)[:900],
    )
    collected_times = [item.get("collected_at") for item in feed_items]
    report.check(
        "feed items 按采集时间倒序（最新采集在前）",
        collected_times == sorted(collected_times, reverse=True),
        repr(collected_times)[:600],
    )
    xhs_items = [item for item in feed_items if item.get("platform") == "xhs"]
    report.check(
        "feed xhs 内容中文 title/text 非空，url/时间/指标字段合法",
        len(xhs_items) >= 1
        and all(
            item["platform_label"] == "小红书"
            and item["title"].strip()
            and item["text"].strip()
            and item["id"].strip()
            and item["url"].startswith("https://example.com/xhs/")
            and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", item["published_at"])
            and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", item["collected_at"])
            and set(item["metrics"].keys()) == {"likes", "comments", "collects", "shares", "plays"}
            and isinstance(item["metrics"]["likes"], int)
            and item["metrics"]["plays"] is None
            and isinstance(item["comment_total"], int)
            for item in xhs_items
        ),
        json.dumps(xhs_items[:2], ensure_ascii=False)[:900],
    )
    commented_items = [item for item in feed_items if int(item.get("comment_total") or 0) > 0]
    report.check(
        "feed 内容附带评论（字段齐全、条数不超 comments、comment_total 为全量）",
        len(commented_items) >= 1
        and all(
            1 <= len(item["comments"]) <= min(int(item["comment_total"]), 3)
            and all(
                set(comment.keys()) == {"id", "author", "text", "likes", "published_at"}
                and comment["text"].strip()
                and isinstance(comment["likes"], int)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", comment["published_at"])
                for comment in item["comments"]
            )
            for item in commented_items
        ),
        json.dumps(commented_items[:1], ensure_ascii=False)[:1000],
    )
    feed_counts = feed.get("counts") or {}
    by_platform = feed_counts.get("by_platform") or {}
    report.check(
        "feed counts 与 items/by_platform 一致（全量计数）",
        feed_counts.get("content") == len(feed_items)
        and feed_counts.get("content") == sum(int(p.get("content") or 0) for p in by_platform.values())
        and feed_counts.get("comments") == sum(int(item.get("comment_total") or 0) for item in feed_items)
        and feed_counts.get("comments") == sum(int(p.get("comments") or 0) for p in by_platform.values())
        and (by_platform.get("xhs") or {}).get("content") == len(xhs_items)
        and isinstance(feed_counts.get("comments"), int)
        and feed_counts.get("comments") >= 1,
        json.dumps(feed_counts, ensure_ascii=False)[:700],
    )
    clamped_feed = ctx.call("GET", f"/api/runs/{run_id}/feed?limit=0&comments=1")
    report.check(
        "feed limit=0 clamp 为 1、comments=1 限制评论明细（counts/comment_total 仍全量）",
        len(clamped_feed.get("items") or []) == 1
        and clamped_feed.get("truncated") is True
        and (clamped_feed.get("counts") or {}).get("content") == feed_counts.get("content")
        and all(len(item.get("comments") or []) == 1 for item in clamped_feed["items"]),
        json.dumps(clamped_feed, ensure_ascii=False)[:900],
    )
    wide_feed = ctx.call("GET", f"/api/runs/{run_id}/feed?limit=999&comments=0")
    report.check(
        "feed limit=999 clamp 100 且 comments=0 不返回评论明细",
        len(wide_feed.get("items") or []) == len(feed_items)
        and wide_feed.get("truncated") is False
        and all(
            item.get("comments") == [] and int(item.get("comment_total") or 0) >= 1
            for item in wide_feed["items"]
        ),
        json.dumps(
            {"items": len(wide_feed.get("items") or []), "truncated": wide_feed.get("truncated")},
            ensure_ascii=False,
        )[:400],
    )
    interrupted_feed = ctx.call("GET", f"/api/runs/{interrupted_id}/feed")
    report.check(
        "feed 对无数据 run 返回 200 空内容流（DB 无匹配不报 500）",
        interrupted_feed.get("items") == []
        and (interrupted_feed.get("counts") or {}).get("content") == 0
        and (interrupted_feed.get("counts") or {}).get("comments") == 0,
        json.dumps(interrupted_feed, ensure_ascii=False)[:500],
    )
    ctx.call_error(
        "GET", "/api/runs/run_xxx/feed", expect_status=404, expect_code="RUN_NOT_FOUND"
    )
    report.check("未知 run 的 feed 返回 404 RUN_NOT_FOUND", True)

    # 6.2 增量（M5a）：since_seq 值域/边界 + logs?after_line 增量
    seqs = sorted(int(item.get("seq") or 0) for item in feed_items)
    max_seq = seqs[-1] if seqs else 0
    report.check(
        "feed 首屏（不传 since_seq）行为与改造前一致：items 倒序 + counts 全量 + next_seq=最大 seq",
        seqs == list(range(1, len(feed_items) + 1))
        and feed.get("next_seq") == max_seq == len(feed_items)
        and feed_counts.get("content") == len(feed_items)
        and collected_times == sorted(collected_times, reverse=True),
        json.dumps(
            {"seqs": seqs, "next_seq": feed.get("next_seq"), "counts": feed_counts},
            ensure_ascii=False,
        )[:500],
    )
    tail_increment = ctx.call("GET", f"/api/runs/{run_id}/feed?since_seq={max_seq}")
    report.check(
        "feed since_seq=末尾 → items 为空且 next_seq 不变",
        tail_increment.get("items") == []
        and tail_increment.get("next_seq") == max_seq
        and (tail_increment.get("counts") or {}).get("content") == feed_counts.get("content"),
        json.dumps(tail_increment, ensure_ascii=False)[:600],
    )
    mid_seq = max_seq // 2
    mid_increment = ctx.call("GET", f"/api/runs/{run_id}/feed?since_seq={mid_seq}&limit=100")
    mid_items = mid_increment.get("items") or []
    mid_pairs = [(int(item.get("seq") or 0), item.get("id")) for item in mid_items]
    expected_pairs = sorted(
        (int(item.get("seq") or 0), item.get("id"))
        for item in feed_items
        if int(item.get("seq") or 0) > mid_seq
    )
    report.check(
        "feed since_seq=中间 → 只返回其后条目（seq 升序）+ counts 仍为全量",
        mid_seq >= 1
        and mid_pairs == expected_pairs
        and len(mid_pairs) >= 1
        and mid_increment.get("next_seq") == max_seq
        and mid_increment.get("truncated") is False
        and (mid_increment.get("counts") or {}).get("content") == feed_counts.get("content")
        and (mid_increment.get("counts") or {}).get("comments") == feed_counts.get("comments"),
        json.dumps(
            {"mid": mid_seq, "pairs": mid_pairs, "expected": expected_pairs,
             "next_seq": mid_increment.get("next_seq")},
            ensure_ascii=False,
        )[:700],
    )
    clamped_increment = ctx.call("GET", f"/api/runs/{run_id}/feed?since_seq=0&limit=0")
    clamped_items = clamped_increment.get("items") or []
    report.check(
        "feed 增量 + limit clamp(0→1) → truncated=true 且 next_seq 指向已返回最后一条",
        len(clamped_items) == 1
        and clamped_increment.get("truncated") is True
        and clamped_increment.get("next_seq") == int(clamped_items[0].get("seq") or 0)
        and (clamped_increment.get("counts") or {}).get("content") == feed_counts.get("content"),
        json.dumps(clamped_increment, ensure_ascii=False)[:700],
    )
    for bad_cursor in ("-1", "abc", "1.5"):
        ctx.call_error(
            "GET",
            f"/api/runs/{run_id}/feed?since_seq={bad_cursor}",
            expect_status=400,
            expect_code="invalid_params",
        )
    report.check("feed 非法 since_seq（负数/非整数）→ 400 invalid_params", True)
    log_tail = ctx.call("GET", f"/api/runs/{run_id}/logs?tail=200")
    tail_log_lines = log_tail.get("lines") or []
    log_next = log_tail.get("next_line")
    last_line_text = tail_log_lines[-1]["text"] if tail_log_lines else ""
    log_last_increment = ctx.call(
        "GET", f"/api/runs/{run_id}/logs?after_line={max(0, int(log_next or 0) - 2)}"
    )
    log_ahead = ctx.call("GET", f"/api/runs/{run_id}/logs?after_line={int(log_next or 0)}")
    log_from_head = ctx.call("GET", f"/api/runs/{run_id}/logs?after_line=0")
    head_lines = log_from_head.get("lines") or []
    report.check(
        "logs?after_line 增量与 next_line 正确（只返回该行之后的新行、末尾为空、游标不回退）",
        isinstance(log_next, int)
        and log_next >= len(tail_log_lines) >= 1
        and [line.get("text") for line in (log_last_increment.get("lines") or [])]
        in ([], [last_line_text])
        and log_last_increment.get("next_line") == log_next
        and log_ahead.get("lines") == []
        and int(log_ahead.get("next_line") or 0) >= log_next
        and len(head_lines) >= 1
        and head_lines[-1].get("text") == last_line_text
        and log_from_head.get("next_line") == log_next,
        json.dumps(
            {"next_line": log_next, "last": log_last_increment.get("lines"),
             "ahead": log_ahead.get("lines"), "head_len": len(head_lines)},
            ensure_ascii=False,
        )[:700],
    )

    # 7. open-file：允许 exports 下真实文件
    opened = ctx.call("POST", "/api/system/open-file", body={"path": str(file_path)})
    report.check("open-file 允许打开 exports 下导出文件（返回 {ok:true}）", opened.get("ok") is True, repr(opened))
    ctx.call_error(
        "POST",
        "/api/system/open-file",
        expect_status=403,
        expect_code="FORBIDDEN",
        body={"path": str(ctx.data_dir / "logs" / "sidecar.log")},
    )
    report.check("open-file 拒绝 exports 之外的文件（403 FORBIDDEN）", True)

    # 8. 鉴权在重启后仍然生效
    ctx.call_error("GET", "/api/runs", expect_status=401, expect_code="UNAUTHORIZED", auth=False)

    # 9. 空日志 / 不存在资源的错误体
    ctx.call_error("GET", "/api/runs/run_xxx/logs", expect_status=404, expect_code="RUN_NOT_FOUND")
    report.check("未知 run 的 logs 返回 404 RUN_NOT_FOUND", True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="sidecar 离线自测（不联网）")
    parser.add_argument("--keep", action="store_true", help="保留临时 data-dir（便于排查）")
    parser.add_argument("--python", default=None, help="覆盖启动 sidecar 的 python（默认当前解释器）")
    parser.add_argument(
        "--simulate-missing-exporter",
        action="store_true",
        help="模拟 exporter.py 未就绪（强制走 503 EXPORTER_UNAVAILABLE 降级分支）",
    )
    return parser.parse_args(argv)


def _seed_llm_provider(
    data_dir: Path,
    base_url: str,
    *,
    name: str = "selftest-fake-provider",
    kind: str = "openai_compatible",
    model: str = "fake-model",
    api_key: str = "selftest-fake-key-1234",
) -> None:
    """往临时 data_dir 的 app.db 写入默认模型 API 配置（报告中心直连 selftest fake LLM）。"""
    import state as state_mod
    from llm_client import LLMService

    store = state_mod.StateStore(data_dir / "app.db")
    try:
        LLMService(None, store).create_provider(
            {
                "name": name,
                "kind": kind,
                "base_url": base_url,
                "model": model,
                "api_key": api_key,
            }
        )
    finally:
        store.close()


def _phase1_env(simulate_missing_exporter: bool = False) -> Dict[str, str]:
    env = {
        "FAKE_ENGINE": "1",
        "FAKE_LOGIN_SCENARIO": "wb=slow,dy=expired,ks=manual,bili=manual_nokind",
        "FAKE_NEED_MANUAL_KIND": "sms",
        "FAKE_TEST_RESULT": "true",
        # 模拟真实耗时检测，用于验证「不同平台 test 可并行、同平台串行」
        "FAKE_TEST_SLEEP": "1.2",
        # dy 前 2 次 crawl 失败（第 1 次用于常规 run 的部分失败；第 2 次用于续采场景），之后成功
        "FAKE_CRAWL_FAIL_ONCE": "dy:2",
        # 该阶段要验证「失败→续采」语义，关闭平台自动重试，避免自动重试吃掉失败
        "SIDECAR_AUTO_RETRY_MAX": "0",
        # 报告中心：用 fake report_data 替身；防轮询太长/适配超时测试
        "SIDECAR_REPORT_DATA_MODULE": "_fake_report_data",
        "SIDECAR_REPORT_TIMEOUT": "8",
        "SIDECAR_REPORT_POLL_INTERVAL": "0.2",
    }
    if simulate_missing_exporter:
        # 指向不存在的模块名，让 sidecar 的 load_exporter() 走降级分支
        env["SIDECAR_EXPORTER_MODULE"] = "_exporter_not_ready_for_selftest"
    return env


def _phase2_env() -> Dict[str, str]:
    return {
        "FAKE_ENGINE": "1",
        "SIDECAR_EXPORTER_MODULE": "_fake_exporter",
        "SIDECAR_REPORT_DATA_MODULE": "_fake_report_data",
    }


def _check_versions(report: "Report") -> None:
    """版本号一致性：``version.FALLBACK_VERSION`` 必须等于 ``app/desktop/package.json`` 的 version。

    历史上版本号散落在多处（sidecar ``0.6.0`` / 报告管线 ``0.3.0`` / 渲染层兜底 ``0.1.0``），
    界面显示的版本和 package.json 对不上。现在唯一来源是 package.json（主进程通过
    ``FS_APP_VERSION`` 注入 sidecar），本断言防止回退值再次漂移。
    """
    import version as version_mod

    try:
        pkg = PROJECT_ROOT / "app" / "desktop" / "package.json"
        pkg_version = json.loads(pkg.read_text(encoding="utf-8")).get("version", "")
    except Exception as exc:  # noqa: BLE001
        report.check("版本号一致性：可读取 app/desktop/package.json", False, repr(exc))
        return
    report.check(
        "版本号一致性：sidecar 回退版本 == package.json version",
        version_mod.FALLBACK_VERSION == pkg_version,
        f"package.json={pkg_version} / fallback={version_mod.FALLBACK_VERSION}（改版本号需同步这两处）",
    )


def main(argv: Optional[List[str]] = None) -> int:
    for _stream in (sys.stdout, sys.stderr):
        try:  # Windows 控制台默认 GBK，统一 UTF-8 防止中文/符号打印异常
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    args = parse_args(argv)
    python = args.python or sys.executable
    report = Report()
    _check_versions(report)
    data_dir = Path(tempfile.mkdtemp(prefix="sidecar_selftest_")).resolve()
    if args.simulate_missing_exporter:
        real_exporter = False
        exporter_reason = "--simulate-missing-exporter（显式模拟 exporter 未就绪）"
    else:
        real_exporter, exporter_reason = detect_real_exporter()

    print("=" * 78)
    print("sidecar 离线自测（FAKE_ENGINE=1，不联网、不启动浏览器）")
    print("=" * 78)
    print(f"[环境] python       = {python}")
    print(f"[环境] 临时 data-dir = {data_dir}")
    print(f"[环境] 项目根目录    = {PROJECT_ROOT}")
    print("[环境] 登录模式       = 有头窗口（--headless false，fake engine 不真的开浏览器）")
    if real_exporter:
        print("[环境] 真实 exporter = 已就绪（阶段 1 会做集成校验，失败仅 WARN）")
    else:
        print(f"[环境] 真实 exporter = 未就绪（{exporter_reason}）")
        print("[环境]                 导出链路将用 _fake_exporter 严格验证（阶段 2）")

    phase1_result: Dict[str, Any] = {}
    sidecar: Optional[SidecarProcess] = None
    ctx: Optional[Ctx] = None
    sidecar2: Optional[SidecarProcess] = None
    ctx2: Optional[Ctx] = None
    sidecar3: Optional[SidecarProcess] = None
    ctx3: Optional[Ctx] = None
    phase3_dir: Optional[Path] = None
    sidecar4: Optional[SidecarProcess] = None
    ctx4: Optional[Ctx] = None
    phase4_dir: Optional[Path] = None
    llm_server: Optional[FakeLLMServer] = None
    success = False
    try:
        # 假模型 API（OpenAI 兼容 + Anthropic）：报告中心直连用户模型，不联网
        llm_server = FakeLLMServer().start()
        print(f"[环境] 假模型 API（OpenAI/Anthropic）= {llm_server.base_url}")
        phase0_config_unit(report, data_dir)
        phase0_report_unit(report, data_dir, llm_server)
        phase0_feature_units(report)
        report.check(
            "采集自动重试分类：Page.goto/net::ERR_CONNECTION_CLOSED 可重试",
            er._is_retryable_crawl_error(
                'Error: Page.goto: net::ERR_CONNECTION_CLOSED at https://www.weibo.com/'
            ) is True,
        )
        report.check(
            "采集自动重试分类：RetryError/DataFetchError 可重试",
            er._is_retryable_crawl_error(
                'RetryError: RetryError[<Future state=finished raised DataFetchError>]'
            ) is True,
        )
        report.check(
            "采集自动重试分类：未登录/扫码等人工错误不自动重试",
            er._is_retryable_crawl_error("未登录或登录已失效") is False
            and er._is_retryable_crawl_error("扫码超时，请重新扫码") is False,
        )
        token = "selftest-" + uuid.uuid4().hex[:16]
        env1 = _phase1_env(simulate_missing_exporter=args.simulate_missing_exporter)
        # 报告中心直连 fake LLM：把默认 provider 写进临时 data-dir 的 app.db
        _seed_llm_provider(data_dir, llm_server.base_url)
        sidecar = SidecarProcess(
            python,
            token,
            data_dir,
            env1,
            label="phase1",
        )
        sidecar.start()
        port, ready_line = sidecar.wait_ready()
        report.check("启动后 stdout 打印 ##SIDECAR_READY##{\"port\": N}", port > 0, ready_line)
        report.check(
            "ready 行 JSON 含正整数 port 且只监听 127.0.0.1",
            READY_RE.match(ready_line) is not None and isinstance(port, int),
            ready_line,
        )
        ctx = Ctx(
            f"http://127.0.0.1:{port}", token, data_dir, report, real_exporter, llm_server
        )
        phase1_result = phase1(ctx)
        ctx.close()
        ctx = None
        sidecar.stop()
        sidecar = None

        # 构造一条“上次 sidecar 崩溃残留的 running 任务”
        _craft_interrupted_run(data_dir)
        token2 = "selftest-" + uuid.uuid4().hex[:16]
        env2 = _phase2_env()
        sidecar2 = SidecarProcess(python, token2, data_dir, env2, label="phase2")
        sidecar2.start()
        port2, ready_line2 = sidecar2.wait_ready()
        report.check("重启 sidecar 成功（ready 行正常）", port2 > 0, ready_line2)
        ctx2 = Ctx(
            f"http://127.0.0.1:{port2}", token2, data_dir, report, False, llm_server
        )
        phase2(ctx2, phase1_result)
        ctx2.close()
        ctx2 = None
        sidecar2.stop()
        sidecar2 = None

        # phase3：未配置模型 API（无 provider）→ 本地模板兜底（不再 409）
        phase3_dir = Path(tempfile.mkdtemp(prefix="sidecar_selftest_phase3_")).resolve()
        token3 = "selftest-" + uuid.uuid4().hex[:16]
        phase3_env = {
            "FAKE_ENGINE": "1",
            "SIDECAR_REPORT_DATA_MODULE": "_fake_report_data",
            "SIDECAR_REPORT_TIMEOUT": "8",
            "SIDECAR_REPORT_POLL_INTERVAL": "0.2",
        }
        sidecar3 = SidecarProcess(
            python, token3, phase3_dir, phase3_env, label="phase3-unconfigured"
        )
        sidecar3.start()
        port3, ready_line3 = sidecar3.wait_ready()
        report.check("未配置判定的 phase3 sidecar 启动成功", port3 > 0, ready_line3)
        ctx3 = Ctx(
            f"http://127.0.0.1:{port3}", token3, phase3_dir, report, False, None
        )
        unconfigured_cfg = ctx3.call("GET", "/api/reports/config")
        report.check(
            "未配置模型 API：GET /api/reports/config configured=false + base_url/provider_name 为空",
            unconfigured_cfg.get("configured") is False
            and unconfigured_cfg.get("base_url") == ""
            and unconfigured_cfg.get("provider_name") == ""
            and unconfigured_cfg.get("degraded_fallback") is True,
            json.dumps(unconfigured_cfg, ensure_ascii=False)[:500],
        )
        unconfigured_conn = ctx3.call("GET", "/api/reports/connection")
        report.check(
            "GET /api/reports/connection（未配置）200 且 configured/connected=false + error + local_fallback",
            unconfigured_conn.get("ok") is False
            and unconfigured_conn.get("configured") is False
            and unconfigured_conn.get("connected") is False
            and "未配置模型 API" in str(unconfigured_conn.get("error") or "")
            and unconfigured_conn.get("local_fallback") is True,
            json.dumps(unconfigured_conn, ensure_ascii=False)[:600],
        )
        report.check(
            "未配置时 GET /api/reports 返回空列表",
            ctx3.call("GET", "/api/reports") == {"reports": []},
        )
        local_created = ctx3.call(
            "POST",
            "/api/reports",
            expect=202,
            body={
                "window_start": "2026-11-01",
                "window_end": "2026-11-09",
                "keywords": ["示例科技"],
                "platforms": ["xhs", "dy"],
            },
        )
        report.check(
            "未配置时 POST /api/reports 返回 202（本地模板兜底，不再 409）",
            str(local_created.get("report_id") or "").startswith("rp_")
            and local_created.get("status") == "queued",
            repr(local_created),
        )
        local_id = str(local_created.get("report_id") or "")

        def _local_report_wait(report_id: str, timeout: float = 40.0) -> Dict[str, Any]:
            deadline = time.monotonic() + timeout
            last: Dict[str, Any] = {}
            while time.monotonic() < deadline:
                last = ctx3.call("GET", f"/api/reports/{report_id}")
                if last.get("status") in ("completed", "degraded", "failed"):
                    return last
                time.sleep(0.2)
            raise SelfTestFailure(f"等待本地模板报告超时：{report_id}")

        local_final = _local_report_wait(local_id)
        report.check(
            "未配置 → 本地模板生成完成（degraded、local-template、HTML/file_path/meta 正常）",
            local_final.get("status") == "degraded"
            and local_final.get("narrative_generated_by") == "local-template"
            and (local_final.get("meta") or {}).get("source") == "local-template"
            and "未配置模型 API" in str(local_final.get("ai_fallback_reason") or "")
            and local_final.get("html_available") is True
            and isinstance(local_final.get("file_path"), str)
            and Path(str(local_final["file_path"])).is_file()
            and isinstance(local_final.get("title"), str),
            json.dumps(local_final, ensure_ascii=False)[:900],
        )
        local_html_path2 = phase3_dir / "reports" / local_id / "v1.html"
        local_html2 = local_html_path2.read_text(encoding="utf-8") if local_html_path2.is_file() else ""
        report.check(
            "未配置本地模板 HTML 含统计条目（标题/窗口/条数/情感/平台）",
            "本地模板" in local_html2
            and "示例科技" in local_html2
            and "2026-11-01" in local_html2
            and "数据概览" in local_html2
            and "情感分布" in local_html2,
            local_html2[:400],
        )
        local_list = ctx3.call("GET", "/api/reports").get("reports") or []
        local_item = next((item for item in local_list if item.get("report_id") == local_id), {})
        report.check(
            "未配置报告出现在列表且带 title/meta（历史标题不再为空）",
            isinstance(local_item.get("title"), str)
            and str(local_item.get("title")).endswith("舆情监测周报")
            and isinstance((local_item.get("meta") or {}).get("counts"), dict)
            and (local_item.get("meta") or {}).get("source") == "local-template",
            repr(local_item)[:700],
        )
        local_revise = ctx3.call(
            "POST",
            f"/api/reports/{local_id}/revise",
            expect=202,
            body={"instruction": "补充下周建议", "base_version": 1},
        )
        local_revised = _local_report_wait(local_id)
        report.check(
            "未配置本地报告可修订重渲染 v2（无需模型 API）",
            local_revise.get("target_version") == 2
            and int(local_revised.get("current_version") or 0) == 2
            and local_revised.get("status") == "degraded"
            and (phase3_dir / "reports" / local_id / "v2.html").is_file(),
            json.dumps(local_revised, ensure_ascii=False)[:700],
        )
        ctx3.close()
        ctx3 = None
        sidecar3.stop()
        sidecar3 = None
        shutil.rmtree(phase3_dir, ignore_errors=True)
        phase3_dir = None

        # phase4：已配置但模型 API 不可达（连接被拒）→ 本地模板兜底
        phase4_dir = Path(tempfile.mkdtemp(prefix="sidecar_selftest_phase4_")).resolve()
        token4 = "selftest-" + uuid.uuid4().hex[:16]
        phase4_env = {
            "FAKE_ENGINE": "1",
            "SIDECAR_REPORT_DATA_MODULE": "_fake_report_data",
            "SIDECAR_REPORT_TIMEOUT": "8",
            "SIDECAR_REPORT_POLL_INTERVAL": "0.2",
        }
        # 默认 provider 指向不可达端口（127.0.0.1:1）：验证生成时网络失败自动降级
        _seed_llm_provider(
            phase4_dir,
            "http://127.0.0.1:1",
            name="unreachable-provider",
            model="unreachable-model",
        )
        sidecar4 = SidecarProcess(
            python, token4, phase4_dir, phase4_env, label="phase4-unreachable"
        )
        sidecar4.start()
        port4, ready_line4 = sidecar4.wait_ready()
        report.check("模型 API 不可达判定的 phase4 sidecar 启动成功", port4 > 0, ready_line4)
        ctx4 = Ctx(
            f"http://127.0.0.1:{port4}", token4, phase4_dir, report, False, llm_server
        )
        unreachable_conn = ctx4.call("GET", "/api/reports/connection")
        report.check(
            "GET /api/reports/connection（已配置）configured/connected=true + base_url 脱敏 + provider",
            unreachable_conn.get("configured") is True
            and unreachable_conn.get("connected") is True
            and unreachable_conn.get("provider_name") == "unreachable-provider"
            and str(unreachable_conn.get("base_url") or "").startswith("http://127.0.0.1"),
            json.dumps(unreachable_conn, ensure_ascii=False)[:600],
        )
        unreachable_created = ctx4.call(
            "POST",
            "/api/reports",
            expect=202,
            body={
                "window_start": "2026-11-01",
                "window_end": "2026-11-09",
                "keywords": ["示例科技"],
            },
        )
        unreachable_id = str(unreachable_created.get("report_id") or "")
        unreachable_final: Dict[str, Any] = {}
        deadline4 = time.monotonic() + 40
        while time.monotonic() < deadline4:
            unreachable_final = ctx4.call("GET", f"/api/reports/{unreachable_id}")
            if unreachable_final.get("status") in ("completed", "degraded", "failed"):
                break
            time.sleep(0.2)
        report.check(
            "模型 API 不可达 → 本地模板兜底 degraded（source=local-template、原因含模型 API、file_path 非空）",
            unreachable_final.get("status") == "degraded"
            and (unreachable_final.get("meta") or {}).get("source") == "local-template"
            and "模型 API" in str(unreachable_final.get("ai_fallback_reason") or "")
            and isinstance(unreachable_final.get("file_path"), str)
            and Path(str(unreachable_final["file_path"])).is_file(),
            json.dumps(unreachable_final, ensure_ascii=False)[:700],
        )
        ctx4.close()
        ctx4 = None
        sidecar4.stop()
        sidecar4 = None
        shutil.rmtree(phase4_dir, ignore_errors=True)
        phase4_dir = None
        success = True
    except SelfTestFailure as exc:
        report.failed.append(str(exc))
        print(f"\n[FAIL] {exc}")
        traceback.print_exc(limit=6)
    except Exception as exc:  # noqa: BLE001
        report.failed.append(f"{type(exc).__name__}: {exc}")
        print(f"\n[FAIL] 未预期异常：{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        for item in (ctx, ctx2, ctx3, ctx4):
            if item is not None:
                item.close()
        for item in (sidecar, sidecar2, sidecar3, sidecar4):
            if item is not None:
                item.stop()
        for directory in (phase3_dir, phase4_dir):
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
        if llm_server is not None:
            print(
                "[llm] fake 模型 API 收到请求：ping=%d / narrative=%d / sentiment_batches=%s"
                % (
                    llm_server.ping_count,
                    len(llm_server.narrative_requests),
                    llm_server.sentiment_batch_sizes(),
                )
            )
            llm_server.stop()
        print("\n" + "=" * 78)
        print("sidecar 原始 stderr（末尾 12 行）：")
        for item in (sidecar2, sidecar, sidecar3, sidecar4):
            if item is None:
                continue
            tail = item.stderr_text().splitlines()[-12:]
            if tail:
                print(f"--- {item.label} ---")
                for line in tail:
                    print(f"    {line}")
        if success and not args.keep:
            shutil.rmtree(data_dir, ignore_errors=True)
            print(f"[清理] 已删除临时 data-dir：{data_dir}")
        else:
            print(f"[保留] 临时 data-dir（排查用）：{data_dir}")
        print("=" * 78)
        print(
            f"结果：PASS {report.passed} / FAIL {len(report.failed)} / "
            f"WARN {len(report.warned)} / SKIP {len(report.skipped)}"
        )
        if report.failed:
            for item in report.failed:
                print(f"  - FAIL: {item}")
        if report.warned:
            for item in report.warned:
                print(f"  - WARN: {item}")
        if report.skipped:
            for item in report.skipped:
                print(f"  - SKIP: {item}")
        print("=" * 78)

    ok = success and not report.failed
    if ok:
        print("[OK] sidecar 离线自测通过")
        return 0
    print("[FAIL] sidecar 离线自测失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())



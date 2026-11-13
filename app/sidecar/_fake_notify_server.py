# -*- coding: utf-8 -*-
"""离线测试替身（舆见 OpinionLens · 通知后端）。

提供两个互不联网的 fake：

* :class:`FakeNotifyServer` —— 进程内 ``ThreadingHTTPServer`` + 随机端口，模拟企业微信 /
  Server酱 / Bark / 通用 Webhook 的 HTTP 响应；支持 ``ok`` / ``fail_500`` / ``fail_401`` /
  ``fail_then_ok`` / ``bad_errcode`` / ``delay`` 故障模式，并记录每个请求（方法/路径/body）。
  测试时用环境变量注入 ``NOTIFY_TEST_HTTP_BASE=<base_url>`` 让内置通道指向它。
* :class:`FakeSMTPRecorder` —— monkeypatch ``smtplib.SMTP`` / ``SMTP_SSL`` 的假 SMTP 客户端，
  记录 EmailMessage 与登录参数，支持认证失败/连接失败模拟（不监听端口、不发真邮件）。

注意：这是测试基础设施，不参与 sidecar 运行期逻辑。
"""
from __future__ import annotations

import json
import smtplib
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

__all__ = ["FakeNotifyServer", "FakeSMTPRecorder", "wait_for"]


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    """轮询等待条件成立（测试辅助；不 sleep 过久）。"""
    deadline = time.time() + max(0.0, float(timeout))
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 等待期间异常视为未满足
            pass
        time.sleep(interval)
    try:
        return bool(predicate())
    except Exception:  # noqa: BLE001
        return False


class FakeNotifyServer:
    """本地 Webhook 假服务端（随机端口，完全离线）。"""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self._httpd = ThreadingHTTPServer((host, 0), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.mode = "ok"  # ok | fail_500 | fail_401 | fail_then_ok | bad_errcode | delay
        self.fail_times = 0  # fail_then_ok 模式下剩余失败次数
        self.delay_s = 0.0
        self.records: List[Dict[str, Any]] = []
        self._requests = 0
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None

    # ---------------- 生命周期 ----------------
    def start(self) -> "FakeNotifyServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="fake-notify", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    def reset(self) -> None:
        with self._lock:
            self.records = []
            self._requests = 0
            self.mode = "ok"
            self.fail_times = 0
            self.delay_s = 0.0

    # ---------------- 测试控制 ----------------
    def set_mode(self, mode: str, fail_times: int = 1) -> "FakeNotifyServer":
        with self._lock:
            self.mode = str(mode or "ok")
            self.fail_times = int(fail_times)
        return self

    @property
    def requests_count(self) -> int:
        with self._lock:
            return self._requests

    def requests(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.records)

    def last_request(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self.records[-1] if self.records else None

    def paths(self) -> List[str]:
        with self._lock:
            return [str(item.get("path") or "") for item in self.records]

    def bodies(self) -> List[Any]:
        with self._lock:
            return [item.get("json") for item in self.records]

    # ---------------- HTTP handler ----------------
    def _make_handler(self):
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # noqa: D102 - 静默访问日志
                return

            def _handle(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                outer._on_request(self, raw)

            do_GET = _handle
            do_POST = _handle

        return _Handler

    def _on_request(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:
        parsed = urllib.parse.urlsplit(handler.path)
        body_text = raw.decode("utf-8", "replace") if raw else ""
        try:
            body_json = json.loads(body_text) if body_text else None
        except (TypeError, ValueError):
            body_json = None
        record = {
            "method": handler.command,
            "path": parsed.path,
            "query": parsed.query,
            "body": body_text,
            "json": body_json,
            "headers": dict(handler.headers.items()),
            "ts": time.time(),
        }
        with self._lock:
            self._requests += 1
            self.records.append(record)
            mode = self.mode
            delay_s = self.delay_s
            if mode == "fail_then_ok":
                if self.fail_times > 0:
                    self.fail_times -= 1
                    mode = "fail_500"
                else:
                    mode = "ok"
        if delay_s:
            time.sleep(float(delay_s))
        if mode == "fail_500":
            self._respond(handler, 500, {"error": "fake server error"})
            return
        if mode == "fail_401":
            self._respond(handler, 401, {"error": "fake unauthorized"})
            return
        if mode == "bad_errcode":
            # HTTP 200 但业务错误码：用于测试“永久失败”分支（企业微信 errcode / Server酱 code）
            self._respond(handler, 200, {"errcode": 40001, "errmsg": "invalid key", "code": 1})
            return
        payload = self._success_payload(parsed.path)
        self._respond(handler, 200, payload)

    @staticmethod
    def _success_payload(path: str) -> Dict[str, Any]:
        if path.endswith("/cgi-bin/webhook/send"):
            return {"errcode": 0, "errmsg": "ok"}
        if path.endswith(".send"):
            return {"code": 0, "message": "ok", "data": {"pushid": "fake"}}
        return {"code": 200, "message": "success"}

    @staticmethod
    def _respond(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            handler.send_response(int(status))
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):  # 客户端超时提前断开
            pass


class FakeSMTPRecorder:
    """monkeypatch ``smtplib.SMTP`` / ``smtplib.SMTP_SSL`` 的假 SMTP（不发真邮件）。"""

    def __init__(self) -> None:
        self.instances: List[Any] = []
        self.messages: List[Any] = []
        self.logins: List[tuple] = []
        self.mode = "ok"  # ok | auth_fail | disconnect | connect_error
        self._installed = False
        self._original_smtp: Any = None
        self._original_smtp_ssl: Any = None

    # ---------------- 生命周期 ----------------
    def install(self) -> "FakeSMTPRecorder":
        if self._installed:
            return self
        import notify_channels  # 延迟导入，确保 sys.path 已就绪

        self._original_smtp = smtplib.SMTP
        self._original_smtp_ssl = smtplib.SMTP_SSL
        recorder = self

        class _FakeClient:
            def __init__(self, host: str, port: int = 0, timeout: Any = None, **kwargs: Any) -> None:
                if recorder.mode == "connect_error":
                    raise ConnectionRefusedError("fake smtp connect refused")
                self.host = host
                self.port = port
                self.timeout = timeout
                self.context = kwargs.get("context")
                self.started_tls = False
                self.quit_called = False
                self.closed = False
                recorder.instances.append(self)

            def ehlo(self) -> tuple:
                return (250, b"fake ehlo")

            def starttls(self, context: Any = None) -> tuple:
                self.started_tls = True
                return (220, b"fake starttls")

            def login(self, username: str, password: str) -> tuple:
                if recorder.mode == "auth_fail":
                    raise smtplib.SMTPAuthenticationError(535, b"fake auth failed")
                recorder.logins.append((username, password))
                return (235, b"fake auth ok")

            def send_message(self, message: Any, from_addr: Any = None, to_addrs: Any = None) -> Dict[str, Any]:
                if recorder.mode == "disconnect":
                    raise smtplib.SMTPServerDisconnected("fake disconnect")
                recorder.messages.append(message)
                return {}

            def quit(self) -> tuple:
                self.quit_called = True
                return (221, b"bye")

            def close(self) -> None:
                self.closed = True

            def __enter__(self) -> "_FakeClient":
                return self

            def __exit__(self, *_exc: Any) -> bool:
                self.close()
                return False

        smtplib.SMTP_SSL = _FakeClient  # type: ignore[assignment]
        smtplib.SMTP = _FakeClient  # type: ignore[assignment]
        notify_channels.smtplib.SMTP_SSL = _FakeClient  # type: ignore[attr-defined]
        notify_channels.smtplib.SMTP = _FakeClient  # type: ignore[attr-defined]
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        smtplib.SMTP = self._original_smtp  # type: ignore[assignment]
        smtplib.SMTP_SSL = self._original_smtp_ssl  # type: ignore[assignment]
        try:
            import notify_channels

            notify_channels.smtplib.SMTP = self._original_smtp  # type: ignore[attr-defined]
            notify_channels.smtplib.SMTP_SSL = self._original_smtp_ssl  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        self._installed = False

    # ---------------- 断言辅助 ----------------
    def reset(self) -> None:
        self.instances = []
        self.messages = []
        self.logins = []
        self.mode = "ok"

    @property
    def sent_count(self) -> int:
        return len(self.messages)

    def last_message(self) -> Optional[Any]:
        return self.messages[-1] if self.messages else None

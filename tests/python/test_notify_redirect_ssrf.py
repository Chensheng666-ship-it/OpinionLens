# -*- coding: utf-8 -*-
"""通知出站不得跟随未再次校验的重定向（生产模式）。

保存 Webhook / Bark 时只检查一次 https 和主机名。发送时 ``urllib.request.urlopen``
会跟随 301/302/303（POST 会改成不带正文的 GET）以及 GET 的 307/308。
这些用例要求：已经接受的那个地址可以连，重定向指向的其它地址不能连。
只把原来的 URL 再校验一遍仍然会失败，因为第二跳的主机、协议或地址不同。

在漏洞还在时，下面大多数用例会失败。修掉「校验一次然后跟随重定向」之后才会通过。

运行（需要 unshare、openssl；脚本会自己进入用户网络命名空间，不改宿主机的 /etc/hosts）::

    python3 tests/python/test_notify_redirect_ssrf.py
"""
from __future__ import annotations

import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SIDECAR = ROOT / "app" / "sidecar"
if str(SIDECAR) not in sys.path:
    sys.path.insert(0, str(SIDECAR))

NS_FLAG = "OL_REDIRECT_NS"
# 通道密钥字段只用这个占位符，不是任何真实凭据。
PLACEHOLDER_SECRET = "PLACEHOLDER"
TASK_NAME = "TASKPLACEHOLDERNAME"
KEYWORD = "KWPLACEHOLDERWORD"


def _reexec_into_namespace() -> None:
    if os.environ.get(NS_FLAG) == "1":
        return
    env = os.environ.copy()
    env[NS_FLAG] = "1"
    for key in list(env):
        if "proxy" in key.lower():
            env.pop(key, None)
    env.pop("NOTIFY_TEST_MODE", None)
    env.pop("NOTIFY_TEST_HTTP_BASE", None)
    script = str(Path(__file__).resolve())
    os.execvpe(
        "unshare",
        ["unshare", "--user", "--map-root-user", "--mount", "--net", sys.executable, script],
        env,
    )


class HitLog:
    def __init__(self) -> None:
        self._items: list[dict] = []
        self._lock = threading.Lock()

    def add(self, item: dict) -> None:
        with self._lock:
            self._items.append(dict(item))

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items]


class RedirectBox:
    def __init__(self) -> None:
        self.code = 200
        self.location = ""


class TrackingServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], label: str, hits: HitLog, box: RedirectBox):
        self.label = label
        self.hits = hits
        self.box = box
        super().__init__(address, _Handler)

    def get_request(self):
        while True:
            try:
                sock, addr = super().get_request()
                break
            except ssl.SSLError:
                continue
        self.hits.add(
            {
                "label": self.label,
                "phase": "accept",
                "peer": "%s:%s" % (addr[0], addr[1]),
                "method": "",
                "path": "",
                "host": "",
                "body": "",
            }
        )
        return sock, addr


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = raw.decode("utf-8", "replace")
        server: TrackingServer = self.server  # type: ignore[assignment]
        server.hits.add(
            {
                "label": server.label,
                "phase": "http",
                "peer": "",
                "method": self.command,
                "path": self.path,
                "host": self.headers.get("Host") or "",
                "body": body[:500],
            }
        )
        location = server.box.location
        if location:
            payload = b"redirect"
            self.send_response(int(server.box.code))
            self.send_header("Location", location)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            return
        payload = b'{"errcode":0,"code":0,"message":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, fmt: str, *args) -> None:
        return


def _bring_up_loopback(extra_addrs: tuple[str, ...]) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    flags = struct.unpack("16sH14s", fcntl_ioctl(sock, 0x8913, struct.pack("16sH14s", b"lo", 0, b"\x00" * 14)))[1]
    fcntl_ioctl(sock, 0x8914, struct.pack("16sH14s", b"lo", flags | 0x1, b"\x00" * 14))
    index = socket.if_nametoindex("lo")
    netlink = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0)
    netlink.bind((0, 0))
    for ip in extra_addrs:
        raw = socket.inet_aton(ip)

        def rta(kind: int, data: bytes) -> bytes:
            pad = (4 - (len(data) % 4)) % 4
            return struct.pack("HH", 4 + len(data), kind) + data + b"\x00" * pad

        payload = struct.pack("BBBBI", socket.AF_INET, 32, 0, 0, index) + rta(2, raw) + rta(1, raw)
        header = struct.pack("IHHII", 16 + len(payload), 20, 1 | 4 | 0x400 | 0x200, 1, 0)
        netlink.send(header + payload)
        reply = netlink.recv(4096)
        if len(reply) >= 20 and struct.unpack_from("H", reply, 4)[0] == 2:
            err = struct.unpack_from("i", reply, 16)[0]
            if err not in (0, -17):  # 0 ok, -17 EEXIST
                raise OSError(-err, "netlink RTM_NEWADDR %s" % ip)


def fcntl_ioctl(sock: socket.socket, request: int, buf: bytes) -> bytes:
    import fcntl

    return fcntl.ioctl(sock, request, buf)


def _write_cert(directory: Path) -> tuple[Path, Path, Path]:
    ca_key = directory / "ca.key"
    ca_pem = directory / "ca.pem"
    srv_key = directory / "srv.key"
    srv_csr = directory / "srv.csr"
    srv_pem = directory / "srv.pem"
    ext = directory / "san.cnf"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(ca_key), "-out", str(ca_pem),
            "-days", "2", "-nodes", "-subj", "/CN=OpinionLens Redirect Test CA",
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "openssl", "req", "-newkey", "rsa:2048",
            "-keyout", str(srv_key), "-out", str(srv_csr),
            "-nodes", "-subj", "/CN=notifyattacker",
        ],
        check=True,
        capture_output=True,
    )
    ext.write_text(
        "subjectAltName="
        "DNS:notifyattacker,DNS:othernotify,DNS:publicother,"
        "DNS:qyapi.weixin.qq.com,DNS:sctapi.ftqq.com,"
        "IP:127.0.0.1,IP:127.0.0.2,IP:8.8.8.8\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "openssl", "x509", "-req", "-in", str(srv_csr),
            "-CA", str(ca_pem), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(srv_pem), "-days", "2", "-extfile", str(ext),
        ],
        check=True,
        capture_output=True,
    )
    return ca_pem, srv_pem, srv_key


def _mount_hosts(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "127.0.0.1 localhost notifyattacker qyapi.weixin.qq.com",
                "127.0.0.2 othernotify sctapi.ftqq.com",
                "8.8.8.8 publicother",
                "",
            ]
        ),
        encoding="utf-8",
    )
    subprocess.run(["mount", "--bind", str(path), "/etc/hosts"], check=True)


def _serve(label: str, host: str, port: int, hits: HitLog, tls: tuple[Path, Path] | None) -> tuple[TrackingServer, RedirectBox]:
    box = RedirectBox()
    server = TrackingServer((host, port), label, hits, box)
    if tls is not None:
        cert, key = tls
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, name="notify-" + label, daemon=True).start()
    return server, box


def _describe(hits: list[dict]) -> str:
    parts = []
    for item in hits:
        if item["phase"] == "accept":
            parts.append("%s:accept" % item["label"])
            continue
        path = item["path"].replace(PLACEHOLDER_SECRET, "<channel-secret>")
        parts.append("%s:%s %s host=%s" % (item["label"], item["method"], path, item["host"]))
    return "; ".join(parts) if parts else "(no connection)"


class Suite:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def ok(self, name: str) -> None:
        self.passed += 1
        print("  [PASS] %s" % name)

    def fail(self, name: str, detail: str) -> None:
        self.failed.append(name)
        print("  [FAIL] %s: %s" % (name, detail))


def main() -> int:
    _reexec_into_namespace()
    os.environ.pop("NOTIFY_TEST_MODE", None)
    os.environ.pop("NOTIFY_TEST_HTTP_BASE", None)
    for key in list(os.environ):
        if "proxy" in key.lower():
            os.environ.pop(key, None)

    import notify_channels
    import notify_service
    from state import StateStore

    if notify_channels.test_mode():
        print("NOTIFY_TEST_MODE is set; this file must run in production mode")
        return 2

    extra = ("192.168.99.10", "10.1.2.3", "169.254.169.254", "127.0.0.2", "8.8.8.8")
    _bring_up_loopback(extra)
    work = Path(tempfile.mkdtemp(prefix="notify-redirect-"))
    ca_pem, srv_pem, srv_key = _write_cert(work)
    os.environ["SSL_CERT_FILE"] = str(ca_pem)
    # urllib 在第一次 urlopen 时创建全局 opener，并在那时把默认 CA 读进 SSLContext。
    # 证书文件必须在任何出站请求之前设好。
    _mount_hosts(work / "hosts")

    hits = HitLog()
    tls = (srv_pem, srv_key)
    _webhook, webhook_box = _serve("webhook", "127.0.0.1", 8443, hits, tls)
    _other, other_box = _serve("other", "127.0.0.2", 8443, hits, tls)
    _pinned, pinned_box = _serve("pinned", "127.0.0.1", 443, hits, tls)
    _sct, sct_box = _serve("sct", "127.0.0.2", 443, hits, tls)
    _public, public_box = _serve("publicother", "8.8.8.8", 8445, hits, tls)
    _loop_https, loop_https_box = _serve("loop-https", "127.0.0.1", 8444, hits, tls)
    sinks = {
        "loop-http": _serve("loop-http", "127.0.0.1", 18080, hits, None)[1],
        "lan": _serve("lan", "192.168.99.10", 18081, hits, None)[1],
        "link-local": _serve("link-local", "169.254.169.254", 18082, hits, None)[1],
        "rfc1918": _serve("rfc1918", "10.1.2.3", 18083, hits, None)[1],
        "downgrade": _serve("downgrade", "127.0.0.1", 18084, hits, None)[1],
    }
    boxes = [webhook_box, other_box, pinned_box, sct_box, public_box, loop_https_box, *sinks.values()]

    db = StateStore(work / "app.db")
    service = notify_service.NotifyService(SimpleNamespace(data_dir=work), db)
    service.start(start_worker=True)
    service.update_settings(
        {"enabled": True, "privacy_mode": "detailed", "quiet_hours": [], "muted_until": 0}
    )
    suite = Suite()

    def begin() -> None:
        for box in boxes:
            box.code = 200
            box.location = ""
        hits.clear()

    def arm(box: RedirectBox, code: int, location: str) -> None:
        box.code = code
        box.location = location

    def assert_only(name: str, allowed: set[str], body_needles: tuple[str, ...] = ()) -> None:
        seen = hits.snapshot()
        bad = [item for item in seen if item["label"] not in allowed]
        authorized = [item for item in seen if item["label"] in allowed and item["phase"] == "http"]
        problems = []
        if not authorized:
            problems.append("saved endpoint was not requested, so the send path did not run")
        if bad:
            problems.append("redirect target was contacted: " + _describe(bad))
        if body_needles:
            blob = "\n".join(item["body"] for item in authorized)
            missing = [needle for needle in body_needles if needle not in blob]
            if missing:
                problems.append("authorized request did not contain %s" % ", ".join(missing))
        if problems:
            suite.fail(name, " | ".join(problems) + " || all=" + _describe(seen))
        else:
            suite.ok(name)

    def send_saved(channel_type: str, secret: str, config: dict, title: str, body: str):
        return notify_channels.send(
            {"type": channel_type, "config": config, "secret": secret},
            title,
            body,
            timeout=4,
            test_mode_=False,
        )

    # --- 直接填写 http 回环在保存时就会被拒绝。重定向绕过的是这道检查。 ---
    begin()
    try:
        notify_channels.validate_config(
            "webhook", {}, "http://127.0.0.1/hook", test_mode_=False
        )
    except notify_channels.ChannelConfigError:
        suite.ok("save rejects an http loopback webhook")
    else:
        suite.fail("save rejects an http loopback webhook", "validate_config accepted it")

    saved = notify_channels.validate_config(
        "webhook", {}, "https://notifyattacker:8443/hook", test_mode_=False
    )
    if saved == {}:
        suite.ok("save accepts the https webhook the attacker gives the user")
    else:
        suite.fail("save accepts the https webhook the attacker gives the user", repr(saved))

    # 1. 采集完成事件 → worker → webhook。302 到 http 回环。
    begin()
    arm(webhook_box, 302, "http://127.0.0.1:18080/from-event")
    channel = service.create_channel(
        {
            "type": "webhook",
            "name": "hook",
            "secret": "https://notifyattacker:8443/hook",
            "config": {},
        }
    )
    service.on_event(
        {
            "type": "run.finished",
            "severity": "info",
            "ts": int(time.time()),
            "dedupe_key": "redirect-event-%s" % time.time(),
            "data": {
                "name": TASK_NAME,
                "keywords": [KEYWORD],
                "keywords_count": 1,
                "platforms_total": 1,
                "platforms_ok": 1,
                "saved_items": 1,
            },
        }
    )
    deadline = time.time() + 8
    while time.time() < deadline:
        rows = service.state.query("SELECT status FROM notify_outbox")
        if rows and all(str(row["status"]) not in ("pending", "sending") for row in rows):
            break
        time.sleep(0.05)
    time.sleep(0.2)
    assert_only(
        "crawl event webhook does not follow 302 to http://127.0.0.1",
        {"webhook"},
        (TASK_NAME, KEYWORD),
    )

    # 2. 通道测试按钮。303 到局域网地址。
    begin()
    arm(webhook_box, 303, "http://192.168.99.10:18081/from-test")
    service.test_channel(str(channel["channel_id"]))
    assert_only("channel test button does not follow 303 to a private address", {"webhook"})

    # 3–8. 同一条已保存的 https URL，不同的重定向目标。
    # 两跳用来卡住「只把原 URL 再查一次」和「只检查第一跳是 https」。
    cases = [
        (
            "webhook 301 does not follow to link-local metadata",
            webhook_box,
            301,
            "http://169.254.169.254:18082/latest/meta-data",
            {"webhook"},
        ),
        (
            "webhook 302 does not follow to RFC1918",
            webhook_box,
            302,
            "http://10.1.2.3:18083/internal",
            {"webhook"},
        ),
        (
            "webhook does not follow a second hop after an https redirect",
            None,
            302,
            "",
            {"webhook"},
        ),
        (
            "webhook does not follow a redirect to a different public https host",
            webhook_box,
            302,
            "https://publicother:8445/elsewhere",
            {"webhook"},
        ),
        (
            "webhook does not follow a scheme downgrade on the saved host",
            webhook_box,
            302,
            "http://notifyattacker:18084/plain",
            {"webhook"},
        ),
        (
            "webhook does not follow a redirect to https://127.0.0.1",
            webhook_box,
            302,
            "https://127.0.0.1:8444/loop",
            {"webhook"},
        ),
        (
            "webhook POST 307 is not delivered to the redirect target",
            webhook_box,
            307,
            "http://127.0.0.1:18080/post-body",
            {"webhook"},
        ),
    ]
    for name, box, code, location, allowed in cases:
        begin()
        if name.startswith("webhook does not follow a second hop"):
            arm(webhook_box, 302, "https://othernotify:8443/next")
            arm(other_box, 302, "http://127.0.0.1:18080/final")
        else:
            arm(box, code, location)
        send_saved(
            "webhook",
            "https://notifyattacker:8443/hook",
            {},
            "标题",
            "正文",
        )
        assert_only(name, allowed)

    # Bark：自建 https 主机，GET，301/302/303/307/308 都会被跟随。
    bark_config = notify_channels.validate_config(
        "bark",
        {"server_url": "https://notifyattacker"},
        PLACEHOLDER_SECRET,
        test_mode_=False,
    )
    bark = service.create_channel(
        {
            "type": "bark",
            "name": "bark",
            "secret": PLACEHOLDER_SECRET,
            "config": {"server_url": "https://notifyattacker"},
        }
    )
    begin()
    arm(pinned_box, 302, "http://127.0.0.1:18080/bark")
    service.test_channel(str(bark["channel_id"]))
    assert_only("bark test button does not follow 302 to http://127.0.0.1", {"pinned"})

    for name, code, location in (
        ("bark 307 does not follow to a private address", 307, "http://192.168.99.10:18081/bark"),
        ("bark 308 does not follow to link-local", 308, "http://169.254.169.254:18082/bark"),
        ("bark 302 does not follow to a different public host", 302, "https://publicother:8445/bark"),
    ):
        begin()
        arm(pinned_box, code, location)
        send_saved("bark", PLACEHOLDER_SECRET, bark_config, "标题", "正文")
        assert_only(name, {"pinned"})

    # 企业微信 / Server酱 的主机是代码写死的，但走同一个 _request。
    # 上游一旦返回重定向，客户端同样会连到下一个地址。
    notify_channels.validate_config("wecom_bot", {}, PLACEHOLDER_SECRET, test_mode_=False)
    wecom = service.create_channel(
        {"type": "wecom_bot", "name": "wecom", "secret": PLACEHOLDER_SECRET, "config": {}}
    )
    begin()
    arm(pinned_box, 302, "http://169.254.169.254:18082/wecom")
    service.test_channel(str(wecom["channel_id"]))
    assert_only("wecom does not follow 302 to link-local", {"pinned"})

    begin()
    arm(pinned_box, 303, "https://publicother:8445/wecom")
    send_saved("wecom_bot", PLACEHOLDER_SECRET, {}, "标题", "正文")
    assert_only("wecom does not follow 303 to a different host", {"pinned"})

    notify_channels.validate_config("serverchan", {}, PLACEHOLDER_SECRET, test_mode_=False)
    sct = service.create_channel(
        {"type": "serverchan", "name": "sct", "secret": PLACEHOLDER_SECRET, "config": {}}
    )
    begin()
    arm(sct_box, 303, "http://10.1.2.3:18083/sct")
    service.test_channel(str(sct["channel_id"]))
    assert_only("serverchan does not follow 303 to RFC1918", {"sct"})

    begin()
    arm(sct_box, 302, "http://127.0.0.1:18080/sct")
    send_saved("serverchan", PLACEHOLDER_SECRET, {}, "标题", "正文")
    assert_only("serverchan does not follow 302 to http://127.0.0.1", {"sct"})

    service.shutdown()
    db.close()
    print("\npassed=%s failed=%s" % (suite.passed, len(suite.failed)))
    return 1 if suite.failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        raise SystemExit(2)

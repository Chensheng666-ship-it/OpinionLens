# -*- coding: utf-8 -*-
"""模型接口地址里的用户名、密码和敏感查询参数不得明文落库、返回或写日志。

用户在「设置 → 模型 API」保存 OpenAI 兼容 / DeepSeek / Anthropic 地址时，
专用 API Key 会走 secret_store.protect，但写在 base_url 里的用户信息
（userinfo）和敏感查询参数目前原样进入 llm_providers，并随列表接口和
mask_base_url 的 netloc 流出。

本文件是修复规格：漏洞还在时这些断言失败；地址拆出的秘密改走与 API Key
相同的 protect 存储、对外只剩 scheme/host/port（路径可以保留）、调用模型时
再拼回之后，断言通过。

运行::

    python tests/python/test_endpoint_credential_storage.py
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import shutil
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_DIR = ROOT / "app" / "sidecar"
FIXTURES_DIR = ROOT / "tests" / "fixtures"
for _path in (FIXTURES_DIR, SIDECAR_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from _fake_llm_server import FAKE_MODEL, FakeLLMServer  # noqa: E402
from api_providers import router as providers_router  # noqa: E402
from engine_runner import ServiceError, SidecarConfig  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from llm_client import LLMService, LocalLLMClient, mask_base_url  # noqa: E402
from main import setup_logging  # noqa: E402
from report_manager import ReportManager  # noqa: E402
from secret_store import unprotect  # noqa: E402
import state as state_mod  # noqa: E402

# 明显的占位符，不是真实凭证。
USERINFO_NAME = "placeholder-userinfo-name"
USERINFO_SECRET = "placeholder-userinfo-secret"
PCT_SECRET = "placeholder@pct"
QUERY_SECRET = "placeholder-query-secret"
UPDATE_SECRET = "placeholder-update-secret"
DEDICATED_KEY = "placeholder-dedicated-key"
BENIGN_QUERY = ("region", "demo")
LOCAL_BEARER = "placeholder-bearer"
# 与通知通道对查询参数的分类一致，并加上模型中转站常用的 api_key。
SENSITIVE_QUERY_NAMES = (
    "key",
    "token",
    "access_token",
    "api_key",
    "sendkey",
    "send_key",
    "device_key",
    "secret",
    "password",
    "passwd",
    "pwd",
    "auth",
)


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []

    def check(self, name: str, condition: bool) -> None:
        if condition:
            self.passed += 1
            print(f"  [PASS] {name}")
            return
        self.failed.append(name)
        print(f"  [FAIL] {name}")

    def section(self, title: str) -> None:
        print(f"\n[{title}]")


def _endpoint(
    host: str,
    *,
    username: str = "",
    password: str = "",
    path: str = "/v1",
    query: Sequence[Tuple[str, str]] = (),
    port: Optional[int] = None,
    encode_userinfo: bool = False,
) -> str:
    hostport = f"{host}:{port}" if port else host
    userinfo = ""
    if username or password:
        user = quote(username, safe="") if encode_userinfo else username
        secret = quote(password, safe="") if encode_userinfo else password
        userinfo = f"{user}:{secret}@" if secret else f"{user}@"
    query_text = "&".join(f"{key}={value}" for key, value in query)
    return f"http://{userinfo}{hostport}{path}" + (f"?{query_text}" if query_text else "")


def _needles(*values: str) -> Tuple[str, ...]:
    found: List[str] = []
    for value in values:
        if value and value not in found:
            found.append(value)
            encoded = quote(value, safe="")
            if encoded != value and encoded not in found:
                found.append(encoded)
    return tuple(found)


def _contains(blob: Any, needle: str) -> bool:
    if blob is None:
        return False
    if isinstance(blob, bytes):
        return needle.encode("utf-8") in blob
    return needle in str(blob)


def _absent(blob: Any, needles: Iterable[str]) -> bool:
    return all(not _contains(blob, needle) for needle in needles)


def _database_bytes(db_path: Path) -> bytes:
    blob = b""
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db_path) + suffix) if suffix else db_path
        if path.is_file():
            blob += path.read_bytes()
    return blob


def _logical_rows(db_path: Path) -> List[Dict[str, Any]]:
    conn = state_mod.sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.row_factory = state_mod.sqlite3.Row
        rows = conn.execute(
            "SELECT provider_id, name, kind, base_url, model, secret_enc, extra_json, "
            "last_test_msg FROM llm_providers"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _public_base_urls(payload: Any) -> List[str]:
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "base_url" and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    return found


def _no_embedded_secret(url: str, needles: Iterable[str]) -> bool:
    if not _absent(url, needles):
        return False
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        return False
    sensitive = {name.lower() for name in SENSITIVE_QUERY_NAMES}
    for key in parse_qs(parsed.query, keep_blank_values=True):
        if key.lower() in sensitive:
            return False
    return True


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _basic_parts(header: str) -> Tuple[str, str]:
    scheme, _, token = str(header or "").partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return "", ""
    try:
        raw = base64.b64decode(token.strip(), validate=True)
        text = raw.decode("utf-8")
    except (ValueError, UnicodeError):
        return "", ""
    username, sep, password = text.partition(":")
    if not sep:
        return "", ""
    return username, password


def _request_query(path: str) -> Dict[str, List[str]]:
    parsed = urlsplit(path if "://" in path else f"http://recorded{path}")
    return {key: values for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}


def _make_app(llm: LLMService, token: str) -> FastAPI:
    """挂上真实的 providers 路由，并复述 main.create_app 的 Bearer 校验。

    中间件与 app/sidecar/main.py 的 auth_middleware 相同：只放行或返回 401，
    不改响应体。模型列表里的地址是否带出秘密，由 llm_client 的返回值决定。
    """
    app = FastAPI()
    app.state.llm = llm
    app.state.token = token

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if request.method == "OPTIONS" or path == "/health" or not path.startswith("/api"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        expected = f"Bearer {token}"
        try:
            authorized = bool(token) and hmac.compare_digest(
                header.encode("utf-8"), expected.encode("utf-8")
            )
        except (TypeError, ValueError):
            authorized = False
        if not authorized:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "UNAUTHORIZED", "message": "缺少或无效的访问令牌"}},
            )
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(providers_router)
    return app


def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {LOCAL_BEARER}"}


def _flush_logs() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def _log_text(cfg: SidecarConfig) -> str:
    _flush_logs()
    path = cfg.logs_dir / "sidecar.log"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _silence_stderr_logs() -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if type(handler) is logging.StreamHandler:
            root.removeHandler(handler)


def run() -> Tuple[int, int]:
    check = Checker()
    tmp_root = Path(tempfile.mkdtemp(prefix="endpoint_secret_")).resolve()
    server: Optional[FakeLLMServer] = None
    manager: Optional[ReportManager] = None
    store: Optional[state_mod.StateStore] = None
    try:
        cfg = SidecarConfig(
            data_dir=tmp_root / "data",
            mc_root=tmp_root / "MediaCrawler",
            python=Path(sys.executable),
            engine_entry=tmp_root / "engine.py",
        )
        cfg.ensure_dirs()
        setup_logging(cfg, "INFO")
        _silence_stderr_logs()
        server = FakeLLMServer().start()
        store = state_mod.StateStore(cfg.app_db)
        llm = LLMService(cfg, store)
        manager = ReportManager(cfg, llm=llm, report_data_loader=lambda: (None, "unused"))
        client_view = LocalLLMClient(cfg, llm=llm)
        http = TestClient(_make_app(llm, LOCAL_BEARER))
        listened = urlsplit(server.base_url)
        host = str(listened.hostname or "127.0.0.1")
        port = int(listened.port or 0)

        check.section("设置接口保存带用户信息的地址")
        userinfo_url = _endpoint(
            str(host), username=USERINFO_NAME, password=USERINFO_SECRET, port=int(port)
        )
        userinfo_needles = _needles(USERINFO_NAME, USERINFO_SECRET)
        anonymous = http.post(
            "/api/llm/providers",
            json={
                "name": "匿名",
                "kind": "openai_compatible",
                "base_url": userinfo_url,
                "model": FAKE_MODEL,
            },
        )
        check.check(
            "未带本地 Bearer 的创建请求被拒绝，且响应不含接口地址中的用户信息",
            anonymous.status_code == 401 and _absent(anonymous.text, userinfo_needles),
        )
        created = http.post(
            "/api/llm/providers",
            headers=_auth_headers(),
            json={
                "name": "中转站",
                "kind": "openai_compatible",
                "base_url": userinfo_url,
                "model": FAKE_MODEL,
                "api_key": DEDICATED_KEY,
            },
        )
        created_body = created.json() if created.headers.get("content-type", "").startswith("application/json") else {}
        check.check("已登录的设置接口接受带用户信息的中转地址", created.status_code == 200)
        provider_id = str((created_body.get("provider") or {}).get("provider_id") or "")
        listed = http.get("/api/llm/providers", headers=_auth_headers())
        listed_body = listed.json() if listed.status_code == 200 else {}
        public_blobs = [created.text, listed.text, json.dumps(created_body, ensure_ascii=False)]
        check.check(
            "创建和列表响应不包含用户名或密码",
            created.status_code == 200
            and listed.status_code == 200
            and all(_absent(blob, userinfo_needles) for blob in public_blobs)
            and all(
                _no_embedded_secret(url, userinfo_needles)
                for url in _public_base_urls(created_body) + _public_base_urls(listed_body)
            ),
        )
        rows = _logical_rows(cfg.app_db)
        stored_urls = [str(row.get("base_url") or "") for row in rows]
        stored_messages = [str(row.get("last_test_msg") or "") for row in rows]
        check.check(
            "数据库里的 base_url / last_test_msg / 其它文本列不含用户名或密码明文",
            bool(rows)
            and all(_absent(json.dumps(row, ensure_ascii=False), userinfo_needles) for row in rows)
            and all(_no_embedded_secret(url, userinfo_needles) for url in stored_urls)
            and all(_absent(message, userinfo_needles) for message in stored_messages)
            and _absent(_database_bytes(cfg.app_db), userinfo_needles),
        )
        protected = [str(row.get("secret_enc") or "") for row in rows if row.get("provider_id") == provider_id]
        dedicated_plain = ""
        try:
            dedicated_plain = unprotect(protected[0]) if protected else ""
        except Exception:  # noqa: BLE001 - 保护格式不对由下面的断言表达
            dedicated_plain = ""
        check.check(
            "专用 API Key 仍只经 protect 存储，数据库文件里没有它的明文",
            bool(protected)
            and protected[0] != DEDICATED_KEY
            and (protected[0].startswith("dpapi:") or protected[0].startswith("b64:"))
            and dedicated_plain == DEDICATED_KEY
            and _absent(_database_bytes(cfg.app_db), (DEDICATED_KEY,))
            and _absent(created.text, (DEDICATED_KEY,)),
        )
        check.check(
            "mask_base_url 与创建日志只保留 scheme 和 host，不保留用户名密码",
            _no_embedded_secret(mask_base_url(userinfo_url), userinfo_needles)
            and _absent(_log_text(cfg), userinfo_needles),
        )

        check.section("调用模型时重新拼回用户信息")
        server.reset_records()
        tested = http.post(
            f"/api/llm/providers/{provider_id}/test", headers=_auth_headers()
        )
        tested_body = tested.json() if tested.headers.get("content-type", "").startswith("application/json") else {}
        record = server.records()[-1] if server.records() else {}
        seen_user, seen_password = _basic_parts(
            str((record.get("headers") or {}).get("authorization") or "")
        )
        check.check(
            "测试连接会把地址中的用户信息交给模型服务，且响应和日志不再带出该秘密",
            tested.status_code == 200
            and tested_body.get("ok") is True
            and seen_user == USERINFO_NAME
            and seen_password == USERINFO_SECRET
            and _absent(tested.text, userinfo_needles)
            and _absent(_log_text(cfg), userinfo_needles)
            and _absent(_database_bytes(cfg.app_db), userinfo_needles),
        )

        check.section("敏感查询参数")
        query_url = _endpoint(
            str(host),
            port=int(port),
            query=(("api_key", QUERY_SECRET), BENIGN_QUERY),
        )
        query_needles = _needles(QUERY_SECRET)
        query_created = http.post(
            "/api/llm/providers",
            headers=_auth_headers(),
            json={
                "name": "查询参数中转",
                "kind": "openai_compatible",
                "base_url": query_url,
                "model": FAKE_MODEL,
                "api_key": DEDICATED_KEY,
            },
        )
        query_body = (
            query_created.json()
            if query_created.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        query_id = str((query_body.get("provider") or {}).get("provider_id") or "")
        query_rows = [
            row for row in _logical_rows(cfg.app_db) if row.get("provider_id") == query_id
        ]
        check.check(
            "api_key 查询参数不进入数据库明文，列表响应也不返回它",
            query_created.status_code == 200
            and _absent(query_created.text, query_needles)
            and all(
                _no_embedded_secret(url, query_needles) for url in _public_base_urls(query_body)
            )
            and bool(query_rows)
            and all(_absent(json.dumps(row, ensure_ascii=False), query_needles) for row in query_rows)
            and _absent(_database_bytes(cfg.app_db), query_needles)
            and _absent(_log_text(cfg), query_needles),
        )
        server.reset_records()
        query_tested = http.post(
            f"/api/llm/providers/{query_id}/test", headers=_auth_headers()
        )
        query_tested_body = (
            query_tested.json()
            if query_tested.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        query_record = server.records()[-1] if server.records() else {}
        query_path = str(query_record.get("path") or "")
        query_params = _request_query(query_path)
        authorization = str((query_record.get("headers") or {}).get("authorization") or "")
        check.check(
            "调用时敏感查询参数以独立查询参数拼回，专用 API Key 仍走 Bearer",
            query_tested.status_code == 200
            and query_tested_body.get("ok") is True
            and urlsplit(query_path if "://" in query_path else f"http://recorded{query_path}").path.endswith(
                "/v1/chat/completions"
            )
            and query_params.get("api_key") == [QUERY_SECRET]
            and query_params.get(BENIGN_QUERY[0]) == [BENIGN_QUERY[1]]
            and authorization == f"Bearer {DEDICATED_KEY}"
            and _absent(query_tested.text, query_needles)
            and _absent(_log_text(cfg), query_needles),
        )

        other_query_ok = True
        for name in SENSITIVE_QUERY_NAMES:
            if name == "api_key":
                continue
            secret = f"placeholder-query-{name}"
            response = http.post(
                "/api/llm/providers",
                headers=_auth_headers(),
                json={
                    "name": f"query-{name}",
                    "kind": "openai_compatible",
                    "base_url": _endpoint("relay.example", query=((name, secret), BENIGN_QUERY)),
                    "model": FAKE_MODEL,
                },
            )
            body = response.json() if response.status_code == 200 else {}
            row_text = json.dumps(_logical_rows(cfg.app_db), ensure_ascii=False)
            if not (
                response.status_code == 200
                and _absent(response.text, _needles(secret))
                and all(_no_embedded_secret(url, _needles(secret)) for url in _public_base_urls(body))
                and _absent(row_text, _needles(secret))
                and _absent(_database_bytes(cfg.app_db), _needles(secret))
                and _absent(_log_text(cfg), _needles(secret))
            ):
                other_query_ok = False
        check.check(
            "其余敏感查询参数（token/key/password 等）同样不进明文库、响应和日志",
            other_query_ok,
        )

        check.section("更新、其它协议，以及百分号编码的用户信息")
        clean = http.post(
            "/api/llm/providers",
            headers=_auth_headers(),
            json={
                "name": "待更新",
                "kind": "openai_compatible",
                "base_url": "https://relay.example/v1",
                "model": FAKE_MODEL,
            },
        )
        clean_body = clean.json() if clean.status_code == 200 else {}
        clean_id = str((clean_body.get("provider") or {}).get("provider_id") or "")
        updated = http.patch(
            f"/api/llm/providers/{clean_id}",
            headers=_auth_headers(),
            json={
                "base_url": _endpoint(
                    "relay.example", username=USERINFO_NAME, password=UPDATE_SECRET
                )
            },
        )
        updated_body = updated.json() if updated.status_code == 200 else {}
        update_needles = _needles(USERINFO_NAME, UPDATE_SECRET)
        update_rows = [
            row for row in _logical_rows(cfg.app_db) if row.get("provider_id") == clean_id
        ]
        check.check(
            "PATCH base_url 同样不把用户信息写入明文列或响应",
            updated.status_code == 200
            and _absent(updated.text, update_needles)
            and all(_no_embedded_secret(url, update_needles) for url in _public_base_urls(updated_body))
            and bool(update_rows)
            and all(_absent(json.dumps(row, ensure_ascii=False), update_needles) for row in update_rows)
            and _absent(_database_bytes(cfg.app_db), update_needles),
        )

        kind_ok = True
        for kind in ("deepseek", "anthropic"):
            secret = f"placeholder-kind-{kind}"
            response = http.post(
                "/api/llm/providers",
                headers=_auth_headers(),
                json={
                    "name": kind,
                    "kind": kind,
                    "base_url": _endpoint("relay.example", username=USERINFO_NAME, password=secret),
                    "model": FAKE_MODEL,
                },
            )
            body = response.json() if response.status_code == 200 else {}
            if not (
                response.status_code == 200
                and _absent(response.text, _needles(secret))
                and _absent(json.dumps(_logical_rows(cfg.app_db), ensure_ascii=False), _needles(secret))
                and _absent(_database_bytes(cfg.app_db), _needles(secret))
                and all(_no_embedded_secret(url, _needles(USERINFO_NAME, secret)) for url in _public_base_urls(body))
            ):
                kind_ok = False
        check.check("DeepSeek 与 Anthropic 保存地址时同样剥离用户信息", kind_ok)

        encoded_url = _endpoint(
            str(host),
            username=USERINFO_NAME,
            password=PCT_SECRET,
            port=int(port),
            encode_userinfo=True,
        )
        encoded_needles = _needles(USERINFO_NAME, PCT_SECRET)
        encoded = http.post(
            "/api/llm/providers",
            headers=_auth_headers(),
            json={
                "name": "编码后的用户信息",
                "kind": "openai_compatible",
                "base_url": encoded_url,
                "model": FAKE_MODEL,
            },
        )
        encoded_body = encoded.json() if encoded.status_code == 200 else {}
        encoded_id = str((encoded_body.get("provider") or {}).get("provider_id") or "")
        check.check(
            "百分号编码的密码不以编码形式或解码形式出现在库和响应里",
            encoded.status_code == 200
            and _absent(encoded.text, encoded_needles)
            and _absent(_database_bytes(cfg.app_db), encoded_needles)
            and _absent(json.dumps(_logical_rows(cfg.app_db), ensure_ascii=False), encoded_needles)
            and _absent(_log_text(cfg), encoded_needles),
        )
        server.reset_records()
        encoded_tested = http.post(
            f"/api/llm/providers/{encoded_id}/test", headers=_auth_headers()
        )
        encoded_record = server.records()[-1] if server.records() else {}
        encoded_user, encoded_password = _basic_parts(
            str((encoded_record.get("headers") or {}).get("authorization") or "")
        )
        encoded_tested_body = (
            encoded_tested.json()
            if encoded_tested.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        check.check(
            "调用时使用解码后的用户信息，而不是把编码留在日志或错误信息里",
            encoded_tested.status_code == 200
            and encoded_tested_body.get("ok") is True
            and encoded_user == USERINFO_NAME
            and encoded_password == PCT_SECRET
            and _absent(encoded_tested.text, encoded_needles)
            and _absent(_log_text(cfg), encoded_needles),
        )

        anthropic = http.post(
            "/api/llm/providers",
            headers=_auth_headers(),
            json={
                "name": "anthropic-live",
                "kind": "anthropic",
                "base_url": _endpoint(
                    str(host), username=USERINFO_NAME, password=USERINFO_SECRET, port=int(port)
                ),
                "model": FAKE_MODEL,
            },
        )
        anthropic_body = anthropic.json() if anthropic.status_code == 200 else {}
        anthropic_id = str((anthropic_body.get("provider") or {}).get("provider_id") or "")
        server.reset_records()
        anthropic_tested = http.post(
            f"/api/llm/providers/{anthropic_id}/test", headers=_auth_headers()
        )
        anthropic_record = server.records()[-1] if server.records() else {}
        anthropic_user, anthropic_password = _basic_parts(
            str((anthropic_record.get("headers") or {}).get("authorization") or "")
        )
        anthropic_path = str(anthropic_record.get("path") or "")
        anthropic_body_tested = (
            anthropic_tested.json()
            if anthropic_tested.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        check.check(
            "Anthropic 调用同样拼回用户信息，请求路径落到 /v1/messages",
            anthropic_tested.status_code == 200
            and anthropic_body_tested.get("ok") is True
            and anthropic_user == USERINFO_NAME
            and anthropic_password == USERINFO_SECRET
            and urlsplit(
                anthropic_path if "://" in anthropic_path else f"http://recorded{anthropic_path}"
            ).path.endswith("/v1/messages"),
        )

        check.section("失败结果、报告配置和脱敏视图")
        refused = _closed_port()
        refused_secret = "placeholder-refused-secret"
        refused_needles = _needles(USERINFO_NAME, refused_secret)
        refused_created = http.post(
            "/api/llm/providers",
            headers=_auth_headers(),
            json={
                "name": "不可达",
                "kind": "openai_compatible",
                "base_url": _endpoint(
                    "127.0.0.1",
                    username=USERINFO_NAME,
                    password=refused_secret,
                    port=refused,
                ),
                "model": FAKE_MODEL,
            },
        )
        refused_body = refused_created.json() if refused_created.status_code == 200 else {}
        refused_id = str((refused_body.get("provider") or {}).get("provider_id") or "")
        refused_tested = http.post(
            f"/api/llm/providers/{refused_id}/test", headers=_auth_headers()
        )
        refused_rows = [
            row for row in _logical_rows(cfg.app_db) if row.get("provider_id") == refused_id
        ]
        check.check(
            "连接失败写入的 last_test_msg、错误响应和日志不含用户信息",
            refused_created.status_code == 200
            and refused_tested.status_code == 200
            and _absent(refused_tested.text, refused_needles)
            and bool(refused_rows)
            and all(_absent(str(row.get("last_test_msg") or ""), refused_needles) for row in refused_rows)
            and _absent(_database_bytes(cfg.app_db), refused_needles)
            and _absent(_log_text(cfg), refused_needles),
        )

        summary = llm.config_summary()
        info = manager.config_info()
        connection = manager.connection_info()
        masked_view = client_view.cfg.masked_base_url
        disclosed = json.dumps(
            {"summary": summary, "info": info, "connection": connection, "masked": masked_view},
            ensure_ascii=False,
        )
        check.check(
            "报告配置、连接状态和 masked_base_url 不含默认提供方地址里的用户信息",
            _absent(disclosed, userinfo_needles)
            and _no_embedded_secret(str(summary.get("base_url") or ""), userinfo_needles)
            and _no_embedded_secret(str(info.get("base_url") or ""), userinfo_needles)
            and _no_embedded_secret(str(connection.get("base_url") or ""), userinfo_needles)
            and _no_embedded_secret(masked_view, userinfo_needles),
        )
    except Exception as exc:  # noqa: BLE001 - 不打印异常文本，其中可能含地址
        check.failed.append(f"未预期异常：{type(exc).__name__}")
        print(f"\n[FAIL] 未预期异常：{type(exc).__name__}")
    finally:
        if manager is not None:
            try:
                manager.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass
        if store is not None:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp_root, ignore_errors=True)
    print("\n" + "=" * 78)
    print(f"结果：PASS {check.passed} / FAIL {len(check.failed)}")
    for item in check.failed:
        print(f"  - FAIL: {item}")
    print("=" * 78)
    return check.passed, len(check.failed)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _passed, failed = run()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

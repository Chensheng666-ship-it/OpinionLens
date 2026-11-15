# -*- coding: utf-8 -*-
"""
test_notify.py — 舆见 OpinionLens 通知后端离线自测（不联网、不发真实邮件、不启动 sidecar）

覆盖：
  1. 通道配置校验：5 类通道、URL 白名单（生产仅 https）、测试模式（NOTIFY_TEST_MODE）、
     SMTP 字段别名（from/to）、Bark 自建服务器、密钥自动提取、错误脱敏；
  2. 通道 CRUD API：创建/编辑/删除、密钥只写不读、PATCH 缺省保留密钥、clear_secret、
     key_masked（secret_store.mask_secret）、非法配置 422、不存在 404；
  3. 事件 → outbox：9 类默认规则种子、规则开关/min_severity/通道绑定、dedupe 幂等、
     notify.enabled 总开关、无通道不落库；
  4. 静默时段：22:00-08:00 默认抑制 info/warn、error 穿透、自定义窗口、muted_until、
     filtered 记录与去重；
  5. 隐私渲染：minimal 不含关键词原文/昵称/正文/任务名/本地路径；standard/detailed 边界；
  6. 发送管线：成功 sent、可重试失败退避 15/30/60/120/240s、永久失败不重试、
     重试耗尽、网络错误分类、通道熔断 30min、令牌桶 10 条/分；
  7. worker 线程：事件入队后后台自动发送、shutdown 幂等、停服后事件忽略；
  8. 测试发送 API：单通道/全部通道、失败返回 ok=false 不抛 500、SMTP fake（UTF-8 主题、
     授权码登录、认证失败/连接失败分类）；
  9. outbox/logs/settings/rules API：包裹键、重试、404/409/422 错误体、日志不含明文密钥。

运行：
  python app/sidecar/test_notify.py

也可被其它测试模块调用：``passed, failed = run(report=None)``。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 说明：NOTIFY_TEST_MODE / NOTIFY_TEST_HTTP_BASE 只在 run() 执行期间注入并在结束时恢复，
# 避免被 selftest 等模块 import 时污染进程环境（生产模式校验仍按真实环境走）。

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import notify_channels  # noqa: E402
import notify_service  # noqa: E402
import secret_store  # noqa: E402
from _fake_notify_server import FakeNotifyServer, FakeSMTPRecorder, wait_for  # noqa: E402
from api_notify import router as notify_router  # noqa: E402
from engine_runner import ServiceError  # noqa: E402
from state import StateStore  # noqa: E402

_failures = 0
_passes = 0

TEST_ENV = notify_channels.TEST_ENV
TEST_HTTP_BASE_ENV = notify_channels.TEST_HTTP_BASE_ENV


# ---------------------------------------------------------------------------
# 断言/输出（沿用项目现有 test_report_data.py 风格）
# ---------------------------------------------------------------------------
def check(name: str, condition: bool, detail: str = "") -> None:
    global _failures, _passes
    if condition:
        _passes += 1
    else:
        _failures += 1
        print(f"  [FAIL] {name}" + (f"：{detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


def _finish(service: Any) -> None:
    try:
        service.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        service.state.close()
    except Exception:  # noqa: BLE001
        pass


def make_service(root: str, name: str, worker: bool = False, quiet_off: bool = False) -> Any:
    """构造测试用 NotifyService。

    quiet_off=True 时关闭默认静默时段（22:00-08:00），避免测试结果依赖运行机器时区：
    发送/重试/worker/多通道等用例在 UTC 环境（如 CI）凌晨运行会被静默过滤。
    """
    state = StateStore(os.path.join(root, f"{name}.db"))
    service = notify_service.NotifyService(
        SimpleNamespace(data_dir=Path(root)), state
    )
    service.start(start_worker=worker)
    if quiet_off:
        service.update_settings({"quiet_hours": [], "muted_until": 0, "enabled": True})
    return service


def make_api(service: Any) -> TestClient:
    app = FastAPI()
    app.state.service = SimpleNamespace(state=service.state)
    app.state.notify = service
    app.include_router(notify_router)

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request: Any, exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return TestClient(app)


def _json(resp: Any) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _outbox(svc: Any, status: Optional[str] = None) -> List[Dict[str, Any]]:
    if status:
        return svc.state.query(
            "SELECT * FROM notify_outbox WHERE status = ? ORDER BY rowid ASC", (status,)
        )
    return svc.state.query("SELECT * FROM notify_outbox ORDER BY rowid ASC")


def _rewind(svc: Any, outbox_id: str) -> None:
    svc.state.execute(
        "UPDATE notify_outbox SET next_attempt_ts = ? WHERE outbox_id = ?",
        (int(time.time()) - 1, outbox_id),
    )


def make_event(
    event_type: str = "run.finished",
    severity: str = "info",
    dedupe: Optional[str] = None,
    ts: Optional[int] = None,
    **data: Any,
) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "run_id": "run_test01",
        "name": "秘密任务",
        "keywords_count": 2,
        "platforms_total": 7,
        "platforms_ok": 7,
        "platforms_failed": 0,
        "platforms_skipped": 0,
        "saved_items": 123,
        "duration_s": 192,
        "error": "",
        "platform": "",
        "account_id": "acc_1",
        "account_alias": "市场部-微博-01",
        "report_id": "",
        "version": 0,
        "title": "",
        "message": "",
    }
    base.update(data)
    return {
        "type": event_type,
        "severity": severity,
        "dedupe_key": dedupe or f"{event_type}:{uuid.uuid4().hex[:12]}",
        "ts": int(ts if ts is not None else time.time()),
        "data": base,
    }


def _local_ts(hour: int, minute: int) -> int:
    return int(datetime(2026, 11, 12, hour, minute, 0).timestamp())


def add_webhook_channel(
    svc: Any, server: FakeNotifyServer, name: str = "测试 Webhook", path: str = "/hook"
) -> Dict[str, Any]:
    return svc.create_channel(
        {
            "type": "webhook",
            "name": name,
            "config": {},
            "secret": server.base_url + path,
        }
    )


# ---------------------------------------------------------------------------
# 1) 通道配置校验 / URL 白名单 / 错误脱敏
# ---------------------------------------------------------------------------
def test_validation(root: str, server: FakeNotifyServer) -> None:
    section("1) 通道配置校验 / URL 白名单 / 脱敏")
    catalog = notify_channels.channel_type_catalog()
    types = {item["type"] for item in catalog}
    check("通道类型注册表含 5 类", types == {"smtp", "wecom_bot", "serverchan", "bark", "webhook"}, str(types))
    check("通道类型目录含中文名称", all(item.get("label") for item in catalog))

    # 生产模式：只允许 https + 内置 host 白名单
    wecom_url = notify_channels.resolve_endpoint(
        "wecom_bot", {}, "abcdef123456", test_mode_=False
    )
    check("生产 wecom 使用官方 https host", wecom_url.startswith("https://qyapi.weixin.qq.com/"), wecom_url)
    check("生产 wecom URL 含 key 参数", "key=abcdef123456" in wecom_url)
    safe = notify_channels.safe_endpoint("wecom_bot", {}, "abcdef123456", test_mode_=False)
    check("wecom 日志端点不含 key", "abcdef" not in safe and "?" not in safe, safe)
    sct_url = notify_channels.resolve_endpoint(
        "serverchan", {}, "SCTtest123456", test_mode_=False
    )
    check("生产 serverchan 使用官方 https host", sct_url.startswith("https://sctapi.ftqq.com/"), sct_url)
    bark_url = notify_channels.resolve_endpoint(
        "bark", {"group": "舆情", "level": "active"}, "devicekey1234", "标题", "正文", test_mode_=False
    )
    check("生产 bark 默认 api.day.app", bark_url.startswith("https://api.day.app/devicekey1234/"), bark_url)
    bark_custom = notify_channels.resolve_endpoint(
        "bark", {"server_url": "https://bark.mydomain.com"}, "devicekey1234", "t", "b", test_mode_=False
    )
    check("生产 bark 支持自建 https 服务器", bark_custom.startswith("https://bark.mydomain.com/"), bark_custom)
    try:
        notify_channels.validate_config(
            "bark", {"server_url": "http://bark.mydomain.com"}, "devicekey1234", test_mode_=False
        )
        check("生产 bark 拒绝 http 自建服务器", False)
    except notify_channels.ChannelConfigError:
        check("生产 bark 拒绝 http 自建服务器", True)
    try:
        notify_channels.validate_config(
            "webhook", {}, "http://evil.example.com/hook", test_mode_=False
        )
        check("生产 webhook 拒绝 http", False)
    except notify_channels.ChannelConfigError:
        check("生产 webhook 拒绝 http", True)
    notify_channels.validate_config(
        "webhook", {}, "https://third-party.example.com/api/hook", test_mode_=False
    )
    check("生产 webhook 允许任意 https host", True)
    # 测试模式：允许 http + fake host
    test_url = notify_channels.resolve_endpoint("wecom_bot", {}, "abcdef123456", test_mode_=True)
    check("测试模式 wecom 指向本地 fake base", test_url.startswith(server.base_url), test_url)
    notify_channels.validate_config("webhook", {}, server.base_url + "/x", test_mode_=True)
    check("测试模式 webhook 允许 http", True)

    # 必填/格式校验
    def expect_error(label: str, fn: Any) -> None:
        try:
            fn()
        except notify_channels.ChannelConfigError:
            check(label, True)
        else:
            check(label, False)

    expect_error("未知通道类型被拒绝", lambda: notify_channels.validate_config("telegram", {}, "x"))
    expect_error("wecom 缺少密钥被拒绝", lambda: notify_channels.validate_config("wecom_bot", {}, ""))
    expect_error(
        "webhook 非 http(s) 被拒绝",
        lambda: notify_channels.validate_config("webhook", {}, "ftp://x/y", test_mode_=True),
    )
    expect_error(
        "SMTP 非法端口被拒绝",
        lambda: notify_channels.validate_config(
            "smtp",
            {"host": "smtp.example.com", "port": 70000, "from": "a@b.com", "to": ["c@d.com"]},
            "x",
        ),
    )
    expect_error(
        "SMTP 非法发件人被拒绝",
        lambda: notify_channels.validate_config(
            "smtp", {"host": "smtp.example.com", "from": "bad", "to": ["c@d.com"]}, "x"
        ),
    )
    expect_error(
        "SMTP 缺少收件人被拒绝",
        lambda: notify_channels.validate_config(
            "smtp", {"host": "smtp.example.com", "from": "a@b.com"}, "x"
        ),
    )
    expect_error(
        "SMTP 有用户名但缺密码被拒绝",
        lambda: notify_channels.validate_config(
            "smtp",
            {"host": "smtp.example.com", "username": "a@b.com", "from": "a@b.com", "to": ["c@d.com"]},
            "",
        ),
    )
    # 163/QQ 实测踩坑：用户名非邮箱 → 535；用户名 ≠ 发件人 → 553；中文用户名 → ascii codec 异常
    for label, username in (
        ("SMTP 用户名不是邮箱被拒绝（避免 535）", "OpinionLens"),
        ("SMTP 用户名与发件人不一致被拒绝（避免 553）", "other@b.com"),
        ("SMTP 中文用户名被拒绝（避免 ascii codec 异常）", "监控邮箱"),
    ):
        expect_error(
            label,
            lambda username=username: notify_channels.validate_config(
                "smtp",
                {"host": "smtp.example.com", "username": username, "from": "a@b.com", "to": ["c@d.com"]},
                "authcode1234",
            ),
        )
    smtp_same_case = notify_channels.validate_config(
        "smtp",
        {"host": "smtp.example.com", "username": "A@B.com", "from": "a@b.com", "to": ["c@d.com"]},
        "authcode1234",
    )
    check("SMTP 用户名与发件人大小写不同视为一致", smtp_same_case["username"] == "A@B.com")
    smtp_blank_user = notify_channels.validate_config(
        "smtp",
        {"host": "smtp.example.com", "from": "a@b.com", "to": ["c@d.com"]},
        "authcode1234",
    )
    check("SMTP 用户名留空 + 有授权码 → 按发件人登录", smtp_blank_user["username"] == "a@b.com", str(smtp_blank_user))
    normalized_smtp = notify_channels.validate_config(
        "smtp",
        {
            "host": "smtp.example.com",
            "security": "starttls",
            "username": "bot@example.com",
            "from": "bot@example.com",
            "to": "a@b.com,c@d.com",
        },
        "authcode1234",
    )
    check("SMTP starttls 默认端口 587", normalized_smtp["port"] == 587, str(normalized_smtp))
    check("SMTP 收件人字符串被拆分为数组", normalized_smtp["to"] == ["a@b.com", "c@d.com"], str(normalized_smtp["to"]))
    check("SMTP 规范化后使用 from/to 短名", "from" in normalized_smtp and "to" in normalized_smtp)
    smtp_alias = notify_channels.validate_config(
        "smtp",
        {"host": "smtp.example.com", "from_addr": "x@y.com", "to_addrs": "z@w.com"},
        "",
    )
    check("SMTP 兼容旧字段名 from_addr/to_addrs", smtp_alias["from"] == "x@y.com" and smtp_alias["to"] == ["z@w.com"])
    check("SMTP ssl 默认端口 465", notify_channels.validate_config(
        "smtp", {"host": "smtp.example.com", "from": "a@b.com", "to": ["c@d.com"]}, "")["port"] == 465)

    # 密钥自动提取（用户直接粘贴完整 URL）
    check(
        "wecom 可从完整 Webhook URL 提取 key",
        notify_channels.validate_config(
            "wecom_bot", {}, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef123456"
        ) == {},
    )
    endpoint = notify_channels.resolve_endpoint(
        "wecom_bot", {}, "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcdef123456", test_mode_=True
    )
    check("完整 URL 提取后的 key 正确进入端点", "key=abcdef123456" in endpoint, endpoint)
    check(
        "serverchan 可从完整 URL 提取 SendKey",
        "SCTtest123456" in notify_channels.resolve_endpoint(
            "serverchan", {}, "https://sctapi.ftqq.com/SCTtest123456.send", test_mode_=True
        ),
    )
    check(
        "bark 可从完整 URL 提取 device key",
        "devicekey1234" in notify_channels.resolve_endpoint(
            "bark", {}, "https://api.day.app/devicekey1234/推送/正文", test_mode_=True
        ),
    )

    # 错误脱敏
    dirty = "请求失败 key=SECRET123 url=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRET123 路径 C:\\Users\\me\\x.log"
    cleaned = notify_channels.sanitize_text(dirty, secrets=("SECRET123",))
    check("脱敏不含明文密钥", "SECRET123" not in cleaned, cleaned)
    check("脱敏隐藏 URL 查询串/路径", "?key=" not in cleaned and "/cgi-bin/webhook" not in cleaned, cleaned)
    check("脱敏移除本地路径", "C:\\Users" not in cleaned, cleaned)
    check(
        "safe_endpoint 对 serverchan 隐藏 SendKey",
        "SCTtest123456" not in notify_channels.safe_endpoint("serverchan", {}, "SCTtest123456", test_mode_=False),
    )
    check(
        "safe_endpoint 对通用 webhook 隐藏 path",
        notify_channels.safe_endpoint("webhook", {}, "https://example.com/hook/SECRET", test_mode_=False)
        == "https://example.com/***",
    )


# ---------------------------------------------------------------------------
# 2) 通道 CRUD API + 密钥脱敏
# ---------------------------------------------------------------------------
def test_channels_api(root: str, server: FakeNotifyServer) -> None:
    section("2) 通道 CRUD / 密钥只写不读 / 掩码")
    svc = make_service(root, "channels_api")
    client = make_api(svc)
    try:
        resp = client.post(
            "/api/notify/channels",
            json={"type": "webhook", "name": "运维群", "config": {}, "secret": server.base_url + "/hook"},
        )
        body = _json(resp)
        check("创建 webhook 通道返回 200", resp.status_code == 200, resp.text)
        channel = body.get("channel") or {}
        channel_id = channel.get("channel_id") or ""
        check("创建通道返回 channel_id", bool(channel_id), str(body))
        check("创建通道 secret_configured=true", channel.get("secret_configured") is True)
        check("创建通道返回掩码", channel.get("key_masked", "").startswith("****"), str(channel.get("key_masked")))
        raw_text = json.dumps(body, ensure_ascii=False)
        check("创建响应不含明文 webhook URL", server.base_url not in raw_text, raw_text[:300])
        check("创建响应不含 secret 字段", '"secret"' not in raw_text)

        resp = client.post(
            "/api/notify/channels",
            json={"type": "wecom_bot", "name": "企微群", "config": {}, "secret": "abcdef123456"},
        )
        wecom = _json(resp).get("channel") or {}
        check("创建企业微信通道成功", bool(wecom.get("channel_id")), resp.text)
        check("企业微信通道 key_masked 只露末 4 位", wecom.get("key_masked") == "****3456", str(wecom.get("key_masked")))
        # 用户直接粘贴完整 Webhook URL：落库只保留 key（掩码按 key 计算）
        resp = client.post(
            "/api/notify/channels",
            json={
                "type": "wecom_bot",
                "name": "企微粘贴URL",
                "config": {},
                "secret": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=pasted123456",
            },
        )
        pasted = _json(resp).get("channel") or {}
        check("粘贴完整 Webhook URL 也能创建", bool(pasted.get("channel_id")), resp.text)
        check("粘贴 URL 时只保存 key（掩码=****3456）", pasted.get("key_masked") == "****3456", str(pasted.get("key_masked")))
        client.delete(f"/api/notify/channels/{pasted['channel_id']}")

        config = _json(client.get("/api/notify/config"))
        check("GET config 返回品牌", config.get("brand") == "舆见 OpinionLens", str(config.get("brand")))
        check("GET config 通道数=2", len(config.get("channels") or []) == 2, str(len(config.get("channels") or [])))
        check("GET config 含 worker 统计", set(("pending", "sending", "failed", "last_sent_at")) <= set(config.get("worker") or {}))
        check("GET config 不含明文密钥", "abcdef123456" not in json.dumps(config, ensure_ascii=False))

        old_enc = svc.state.query(
            "SELECT secret_enc FROM notify_channels WHERE channel_id = ?", (channel_id,)
        )[0]["secret_enc"]
        resp = client.patch(f"/api/notify/channels/{channel_id}", json={"name": "运维群-改名"})
        updated = _json(resp).get("channel") or {}
        check("PATCH 改名成功", updated.get("name") == "运维群-改名", resp.text)
        check("PATCH 不传 secret 保留配置", updated.get("secret_configured") is True and updated.get("key_masked") == channel.get("key_masked"))
        new_enc = svc.state.query(
            "SELECT secret_enc FROM notify_channels WHERE channel_id = ?", (channel_id,)
        )[0]["secret_enc"]
        check("PATCH 不传 secret 时密文不变", old_enc == new_enc)
        check("落库的是 DPAPI/b64 密文而非明文", not str(new_enc).endswith("/hook") and str(new_enc) != "")

        resp = client.patch(
            f"/api/notify/channels/{channel_id}", json={"secret": server.base_url + "/abcdef9876"}
        )
        updated = _json(resp).get("channel") or {}
        check("PATCH 新密钥后掩码更新", updated.get("key_masked") == "****9876", str(updated.get("key_masked")))
        resp = client.patch(f"/api/notify/channels/{channel_id}", json={"clear_secret": True})
        updated = _json(resp).get("channel") or {}
        check("clear_secret 后 secret_configured=false", updated.get("secret_configured") is False)
        check("clear_secret 后 key_masked 为空", updated.get("key_masked") == "")
        # 再次设置密钥供后续测试使用
        client.patch(f"/api/notify/channels/{channel_id}", json={"secret": server.base_url + "/hook"})

        # 编辑时缺省 secret=保留；显式空串也保留
        resp = client.patch(f"/api/notify/channels/{channel_id}", json={"secret": ""})
        check("PATCH secret 空串视为保留旧密钥", (_json(resp).get("channel") or {}).get("secret_configured") is True)

        resp = client.post(
            "/api/notify/channels", json={"type": "unknown_type", "name": "x", "secret": "y"}
        )
        err = _json(resp).get("error") or {}
        check("未知通道类型返回 422", resp.status_code == 422, resp.text)
        check("未知通道类型错误码 NOTIFY_CHANNEL_INVALID", err.get("code") == "NOTIFY_CHANNEL_INVALID", str(err))

        # 生产模式（去掉测试环境变量）拒绝 http webhook
        saved = os.environ.pop(TEST_ENV, None)
        try:
            resp = client.post(
                "/api/notify/channels",
                json={"type": "webhook", "name": "http", "secret": "http://evil.example.com/hook"},
            )
            check("生产模式 API 拒绝 http webhook（422）", resp.status_code == 422, resp.text)
        finally:
            if saved is not None:
                os.environ[TEST_ENV] = saved

        resp = client.patch("/api/notify/channels/ch_missing", json={"name": "x"})
        check("PATCH 不存在的通道返回 404", resp.status_code == 404, resp.text)
        check("404 错误体含 code", (_json(resp).get("error") or {}).get("code") == "NOTIFY_CHANNEL_NOT_FOUND")

        resp = client.delete(f"/api/notify/channels/{channel_id}")
        check("DELETE 通道返回 ok", _json(resp).get("ok") is True, resp.text)
        resp = client.delete(f"/api/notify/channels/{channel_id}")
        check("重复 DELETE 返回 404", resp.status_code == 404, resp.text)
        check("删除后 config 通道数=1", len((_json(client.get("/api/notify/config")).get("channels") or [])) == 1)

        # 删除不可用通道的 pending 通知最终 failed（不阻塞队列）
        notice = _outbox(svc, "pending")
        check("当前没有意外 pending 通知", len(notice) == 0, str(len(notice)))
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# 3) 规则种子 / 匹配 / 总开关 / 通道绑定
# ---------------------------------------------------------------------------
def test_rules_and_matching(root: str, server: FakeNotifyServer) -> None:
    section("3) 默认规则种子 / 匹配 / 总开关 / 通道绑定")
    svc = make_service(root, "rules", quiet_off=True)
    client = make_api(svc)
    try:
        state = svc.state
        rows = state.query("SELECT * FROM notify_rules")
        check("启动种子 9 条默认规则", len(rows) == 9, str(len(rows)))
        seeded_types = {row["event_type"] for row in rows}
        check(
            "种子规则覆盖 9 类事件",
            seeded_types == {item[0] for item in notify_service.DEFAULT_EVENT_RULES},
            str(sorted(seeded_types)),
        )
        check("种子规则默认全部启用", all(bool(row["enabled"]) for row in rows))
        svc.start()  # 幂等
        check("重复 start 不重复种子规则", len(state.query("SELECT * FROM notify_rules")) == 9)

        rules_body = _json(client.get("/api/notify/rules"))
        check("GET rules 返回 rules 数组", isinstance(rules_body.get("rules"), list) and len(rules_body["rules"]) == 9)
        check("GET rules 返回 event_types 目录", len(rules_body.get("event_types") or []) == 9)

        channel = add_webhook_channel(svc, server)
        # 规则开关
        resp = client.put(
            "/api/notify/rules", json={"rules": [{"event_type": "run.finished", "enabled": False}]}
        )
        check("PUT rules 关闭订阅成功", resp.status_code == 200, resp.text)
        svc.on_event(make_event("run.finished", dedupe="rule-off-1"))
        check("规则关闭后事件不落库", len(_outbox(svc)) == 0)
        client.put("/api/notify/rules", json={"rules": [{"event_type": "run.finished", "enabled": True}]})

        # min_severity
        client.put(
            "/api/notify/rules",
            json={"rules": [{"event_type": "run.finished", "min_severity": "error"}]},
        )
        svc.on_event(make_event("run.finished", dedupe="sev-info-1"))
        check("min_severity=error 时 info 事件被过滤", len(_outbox(svc)) == 0)
        client.put(
            "/api/notify/rules",
            json={"rules": [{"event_type": "run.finished", "min_severity": "info"}]},
        )
        svc.on_event(make_event("run.finished", dedupe="sev-info-2"))
        check("恢复 min_severity=info 后事件落库", len(_outbox(svc)) == 1)

        # 通道绑定
        channel2 = add_webhook_channel(svc, server, name="第二通道", path="/hook2")
        client.put(
            "/api/notify/rules",
            json={"rules": [{"event_type": "run.failed", "channel_ids": [channel2["channel_id"]]}]},
        )
        svc.on_event(make_event("run.failed", severity="error", dedupe="bind-1"))
        bound = [
            item
            for item in _outbox(svc, "pending")
            if item["event_type"] == "run.failed"
        ]
        check(
            "通道绑定只写入指定通道",
            len(bound) == 1 and json.loads(bound[0]["channel_ids_json"]) == [channel2["channel_id"]],
            str(bound),
        )

        # conditions：only_if 数值条件
        client.put(
            "/api/notify/rules",
            json={
                "rules": [
                    {
                        "event_type": "run.partial",
                        "conditions": {"only_if": {"saved_items_gt": 500}},
                    }
                ]
            },
        )
        svc.on_event(
            make_event("run.partial", severity="warn", dedupe="cond-skip-1", saved_items=123)
        )
        check(
            "conditions 不满足时不落库",
            not [item for item in _outbox(svc) if item["dedupe_key"] == "cond-skip-1"],
        )
        svc.on_event(
            make_event("run.partial", severity="warn", dedupe="cond-hit-1", saved_items=600)
        )
        check(
            "conditions 满足时落库",
            len([item for item in _outbox(svc) if item["dedupe_key"] == "cond-hit-1"]) == 1,
        )
        resp = client.put(
            "/api/notify/rules",
            json={"rules": [{"event_type": "run.failed", "channel_ids": ["ch_not_exists"]}]},
        )
        check("rules 引用不存在的通道返回 422", resp.status_code == 422, resp.text)

        # 总开关
        client.put("/api/notify/settings", json={"enabled": False})
        before = len(_outbox(svc))
        svc.on_event(make_event("run.partial", severity="warn", dedupe="disabled-1"))
        check("notify.enabled=false 时事件不落库", len(_outbox(svc)) == before)
        client.put("/api/notify/settings", json={"enabled": True})

        # 未知事件类型
        before = len(_outbox(svc))
        svc.on_event({"type": "custom.unknown", "severity": "info", "dedupe_key": "unknown-1", "ts": 1, "data": {}})
        check("未知事件类型不落库", len(_outbox(svc)) == before)

        # 无通道时不落库
        for item in svc.list_channels():
            client.delete(f"/api/notify/channels/{item['channel_id']}")
        before = len(_outbox(svc))
        svc.on_event(make_event("run.finished", dedupe="no-channel-1"))
        check("没有启用通道时不落库", len(_outbox(svc)) == before)
        check("通道删除后 config 为空", len(_json(client.get("/api/notify/config"))["channels"]) == 0)
        _ = channel
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# 4) 静默时段 / 临时静音
# ---------------------------------------------------------------------------
def test_quiet_hours(root: str, server: FakeNotifyServer) -> None:
    section("4) 静默时段 / 临时静音 / filtered 记录")
    svc = make_service(root, "quiet")
    client = make_api(svc)
    try:
        add_webhook_channel(svc, server)
        # 默认 22:00-08:00 抑制 info/warn，error 穿透
        check("默认静默 22:00 抑制 info", svc._quiet_reason("info", _local_ts(22, 0)) == "quiet_hours")
        check("默认静默 23:30 抑制 warn", svc._quiet_reason("warn", _local_ts(23, 30)) == "quiet_hours")
        check("默认静默 07:59 抑制 info", svc._quiet_reason("info", _local_ts(7, 59)) == "quiet_hours")
        check("默认静默 08:00 结束（不抑制）", svc._quiet_reason("info", _local_ts(8, 0)) == "")
        check("默认静默 12:00 不抑制", svc._quiet_reason("info", _local_ts(12, 0)) == "")
        check("error 穿透静默时段", svc._quiet_reason("error", _local_ts(23, 30)) == "")

        # 自定义窗口（12:00-13:00 只抑制 info）
        client.put(
            "/api/notify/settings",
            json={"quiet_hours": [{"start": "12:00", "end": "13:00", "severity": "info"}]},
        )
        check("自定义窗口 12:30 抑制 info", svc._quiet_reason("info", _local_ts(12, 30)) == "quiet_hours")
        check("自定义窗口 12:30 不抑制 warn", svc._quiet_reason("warn", _local_ts(12, 30)) == "")
        check("自定义窗口 14:00 不抑制", svc._quiet_reason("info", _local_ts(14, 0)) == "")

        # muted_until：先清空静默时段，避免真实当前时刻落在上面 12:00-13:00 窗口里（中午跑测试会误判）
        client.put("/api/notify/settings", json={"quiet_hours": []})
        client.put("/api/notify/settings", json={"muted_until": int(time.time()) + 3600})
        check("muted_until 抑制 info", svc._quiet_reason("warn") == "muted_until")
        check("muted_until 不拦 error", svc._quiet_reason("error") == "")
        client.put("/api/notify/settings", json={"muted_until": int(time.time()) - 10})
        check("muted_until 过期后不再抑制", svc._quiet_reason("info") == "")

        # 全天静默 → info/warn 落 filtered，error 落 pending
        client.put(
            "/api/notify/settings",
            json={"quiet_hours": [{"start": "00:00", "end": "23:59", "severity": "warn"}], "muted_until": 0},
        )
        svc.on_event(make_event("run.finished", dedupe="quiet-info-1"))
        filtered = _outbox(svc, "filtered")
        check("静默期 info 事件写入 filtered 记录", len(filtered) == 1, str(len(filtered)))
        check("filtered 记录 last_error=quiet_hours", filtered and filtered[0]["last_error"] == "quiet_hours", str(filtered))
        check("静默期 info 不进入 pending", len(_outbox(svc, "pending")) == 0)
        svc.on_event(make_event("run.finished", dedupe="quiet-info-1"))
        check("filtered 记录参与 dedupe（重复事件不新增）", len(_outbox(svc, "filtered")) == 1)
        svc.on_event(make_event("run.failed", severity="error", dedupe="quiet-error-1"))
        check("静默期 error 事件正常入 pending", len(_outbox(svc, "pending")) == 1)

        # 恢复默认设置
        client.put("/api/notify/settings", json={"quiet_hours": [], "muted_until": 0, "enabled": True})
        svc.on_event(make_event("run.finished", dedupe="quiet-after-1"))
        check("关闭静默后事件正常入 pending", len(_outbox(svc, "pending")) == 2)
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# 5) 隐私渲染
# ---------------------------------------------------------------------------
def test_render_privacy(root: str, server: FakeNotifyServer) -> None:
    section("5) 隐私渲染（minimal / standard / detailed）")
    rich = make_event(
        "run.finished",
        dedupe="render-1",
        keywords=["内部敏感词"],
        nickname="张三",
        content="这是评论正文内容",
        excel_path="C:\\Users\\secret\\导出.xlsx",
        message="本地路径 C:\\Users\\secret\\x.log 写入失败",
        name="秘密任务",
        account_alias="市场部-微博-01",
    )
    rendered = notify_service.render_event(rich, "minimal")
    text = rendered["title"] + "\n" + rendered["body"]
    check("minimal 标题含事件类型与品牌", "采集完成" in rendered["title"] and "舆见 OpinionLens" in rendered["title"], rendered["title"])
    check("minimal 正文含平台统计", "平台：7/7 完成" in rendered["body"], rendered["body"][:200])
    check("minimal 正文含采集条数", "采集：123 条" in rendered["body"])
    check("minimal 正文含账号别名", "市场部-微博-01" in rendered["body"])
    check("minimal 正文含时间", "时间：" in rendered["body"])
    check("minimal 不含关键词原文", "内部敏感词" not in text)
    check("minimal 不含任务名", "秘密任务" not in text)
    check("minimal 不含昵称", "张三" not in text)
    check("minimal 不含正文内容", "评论正文内容" not in text)
    check("minimal 不含本地路径", "C:\\Users" not in text and "导出.xlsx" not in text)
    check("minimal 含品牌脱敏脚注", "舆见 OpinionLens" in rendered["body"])

    standard = notify_service.render_event(rich, "standard")
    standard_text = standard["title"] + standard["body"]
    check("standard 含任务名", "秘密任务" in standard_text)
    check("standard 仍不含关键词原文", "内部敏感词" not in standard_text)
    check("standard 仍不含昵称/正文", "张三" not in standard_text and "评论正文内容" not in standard_text)

    detailed = notify_service.render_event(rich, "detailed")
    detailed_text = detailed["title"] + detailed["body"]
    check("detailed 含关键词原文", "内部敏感词" in detailed_text)
    check("detailed 仍不含昵称/正文/本地路径", "张三" not in detailed_text and "评论正文内容" not in detailed_text and "C:\\Users" not in detailed_text)
    check("detailed 保留品牌脚注", "舆见 OpinionLens" in detailed_text)

    degraded = notify_service.render_event(
        make_event(
            "report.degraded",
            severity="warn",
            dedupe="render-report",
            report_id="rep_1",
            version=2,
            message="模型输出过短 C:\\secret\\cache.tmp",
        ),
        "minimal",
    )
    check("report.degraded 标题含降级", "报告已生成（降级）" in degraded["title"], degraded["title"])
    check("report.degraded 正文移除本地路径", "C:\\secret" not in degraded["body"] and "cache.tmp" not in degraded["body"], degraded["body"])
    check("report.degraded 正文保留可读原因", "模型输出过短" in degraded["body"])

    account = notify_service.render_event(
        make_event(
            "account.login_invalid",
            severity="error",
            dedupe="render-account",
            platform="wb",
            account_alias="市场部-微博-01",
            message="登录已失效",
        ),
        "minimal",
    )
    check("账号失效标题含事件名", "账号登录失效" in account["title"])
    check("账号失效正文含平台中文名", "微博" in account["body"])
    check("账号失效正文含处理建议", "重新扫码" in account["body"])
    manual = notify_service.render_event(
        make_event("login.need_manual", severity="warn", dedupe="render-manual", platform="xhs", kind="sms"),
        "minimal",
    )
    check("人工验证正文含验证方式", "短信验证" in manual["body"], manual["body"])

    icon = notify_service.render_event(make_event("run.failed", severity="error", dedupe="render-icon"), "minimal")
    check("error 事件标题带 ❌ 前缀", icon["title"].startswith("❌"), icon["title"])
    unknown = notify_service.render_event({"type": "foo.bar", "severity": "info", "ts": 0, "data": None}, "minimal")
    check("未知事件渲染不抛异常", isinstance(unknown.get("body"), str) and "通知事件" in unknown["title"])
    test_render = notify_service.render_event(
        {"type": "notify.test", "severity": "info", "ts": int(time.time()), "data": {"message": "测试消息"}},
        "minimal",
    )
    check("notify.test 渲染含测试标题", "通知测试" in test_render["title"])
    check("notify.test 渲染正文不泄露额外内容", len(test_render["body"]) < 800)


# ---------------------------------------------------------------------------
# 6) 发送对象（单通道成功/退避重试/永久失败/重试耗尽/网络错误）
# ---------------------------------------------------------------------------
def test_send_and_retry(root: str, server: FakeNotifyServer) -> None:
    section("6) 发送 / 退避重试 / 永久失败 / 重试耗尽")
    svc = make_service(root, "retry", quiet_off=True)
    try:
        add_webhook_channel(svc, server)
        server.reset()
        server.set_mode("ok")

        # 成功
        svc.on_event(make_event("run.finished", dedupe="send-ok-1"))
        sent_count = svc.process_pending()
        check("有 pending 时 process_pending 处理 1 条", sent_count == 1, str(sent_count))
        sent_rows = _outbox(svc, "sent")
        check("发送成功状态=sent", len(sent_rows) == 1 and sent_rows[0]["sent_at"], str(sent_rows))
        check("成功时记录 attempts=0", int(sent_rows[0]["attempts"] or 0) == 0)
        check("fake 服务端收到 1 个请求", server.requests_count == 1, str(server.requests_count))
        logs = svc.list_logs(10)
        check("发送成功写 notify_log(ok=1)", len(logs) == 1 and logs[0]["ok"] is True, str(logs))
        check("notify_log 记录 latency_ms", isinstance(logs[0]["latency_ms"], int) and logs[0]["latency_ms"] >= 0)
        last_req = server.last_request() or {}
        check("webhook POST body 含标题/来源", str((last_req.get("json") or {}).get("source")) == "舆见 OpinionLens", str(last_req.get("json")))

        # 可重试失败：15s 退避
        server.reset()
        server.set_mode("fail_500")
        svc.on_event(make_event("run.partial", severity="warn", dedupe="retry-1"))
        svc.process_pending()
        row = _outbox(svc, "pending")[0]
        oid = row["outbox_id"]
        check("5xx 失败后仍在 pending", len(_outbox(svc, "pending")) == 1)
        check("5xx 失败 attempts=1", int(row["attempts"]) == 1, str(row["attempts"]))
        delay = int(row["next_attempt_ts"] or 0) - int(time.time())
        check("首次重试退避≈15s", 13 <= delay <= 17, str(delay))
        check("失败写 notify_log(ok=0)", any(not item["ok"] for item in svc.list_logs(10) if item["outbox_id"] == oid))
        check("5xx 失败不标记 failed", len(_outbox(svc, "failed")) == 0)

        # 第二次失败：30s 退避
        _rewind(svc, oid)
        server.set_mode("fail_500")
        svc.process_pending()
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (oid,))[0]
        delay = int(row["next_attempt_ts"] or 0) - int(time.time())
        check("第二次重试退避≈30s", 28 <= delay <= 32, str(delay))
        check("第二次失败 attempts=2", int(row["attempts"]) == 2)

        # 第三次成功
        server.set_mode("ok")
        _rewind(svc, oid)
        svc.process_pending()
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (oid,))[0]
        check("退避重试后最终 sent", row["status"] == "sent", str(row))
        oid_logs = [item for item in svc.list_logs(20) if item["outbox_id"] == oid]
        check("同一 outbox 记录 3 次发送日志", len(oid_logs) == 3, str(len(oid_logs)))
        check("日志 ok 序列为 失败/失败/成功", [item["ok"] for item in reversed(oid_logs)] == [False, False, True])

        # 永久失败（4xx）不重试
        server.reset()
        server.set_mode("fail_401")
        svc.on_event(make_event("run.finished", dedupe="perm-1"))
        svc.process_pending()
        failed = _outbox(svc, "failed")
        check("401 永久失败立即 failed", len(failed) == 1, str(len(failed)))
        check("永久失败 attempts=1", int(failed[0]["attempts"] or 0) == 0)
        check("永久失败 last_error 含 HTTP 401", "HTTP 401" in failed[0]["last_error"], failed[0]["last_error"])
        check("永久失败 last_error 不含明文 URL", server.base_url not in failed[0]["last_error"])
        requests_before = server.requests_count
        _rewind(svc, failed[0]["outbox_id"])
        svc.process_pending()
        check("永久失败不自动重试", server.requests_count == requests_before, str(server.requests_count))

        # 重试耗尽（首次 + 5 次重试；每轮清空熔断/令牌桶以单独验证退避逻辑）
        server.reset()
        server.set_mode("fail_500")
        svc._circuits.clear()
        svc._buckets.clear()
        svc.on_event(make_event("run.finished", dedupe="exhaust-1"))
        svc.process_pending()
        exhaust_id = _outbox(svc, "pending")[0]["outbox_id"]
        # 第 1 次失败（attempts=1，退避 15s）+ 再 4 次失败 → attempts=5，退避 240s
        for _ in range(1, 5):
            svc._circuits.clear()
            svc._buckets.clear()
            _rewind(svc, exhaust_id)
            svc.process_pending()
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (exhaust_id,))[0]
        check("重试 5 次后状态仍 pending（等待最后一次）", row["status"] == "pending" and int(row["attempts"]) == 5, str(row))
        check("第 5 次失败退避≈240s", 235 <= int(row["next_attempt_ts"]) - int(time.time()) <= 245, str(int(row["next_attempt_ts"]) - int(time.time())))
        svc._circuits.clear()
        svc._buckets.clear()
        _rewind(svc, exhaust_id)
        svc.process_pending()
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (exhaust_id,))[0]
        check("超过最大重试次数后 failed", row["status"] == "failed", str(row))
        check("耗尽失败 last_error 提示重试次数", "重试 5 次" in row["last_error"], row["last_error"])
        check("耗尽前共 6 次请求", server.requests_count == 6, str(server.requests_count))
        svc._circuits.clear()
        svc._buckets.clear()

        # 网络错误归为可重试
        server.reset()
        bad = svc.create_channel(
            {"type": "webhook", "name": "不可达", "config": {}, "secret": "http://127.0.0.1:1/hook"}
        )
        before = len(_outbox(svc))
        svc.on_event(
            {
                "type": "run.finished",
                "severity": "info",
                "dedupe_key": "net-err-1",
                "ts": int(time.time()),
                "data": {"run_id": "r1", "platforms_total": 1, "platforms_ok": 1},
            }
        )
        pending = [
            item
            for item in _outbox(svc, "pending")
            if item["dedupe_key"] == "net-err-1"
        ]
        check("网络错误场景产生 pending 通知", len(pending) == 1 and len(_outbox(svc)) == before + 1)
        oid = pending[0]["outbox_id"]
        # 该事件同时绑定 2 个通道（webhook 成功 + 不可达）：任一成功即 sent
        svc._circuits.clear()
        svc._buckets.clear()
        svc.process_pending()
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (oid,))[0]
        check("多通道部分可达时标记 sent", row["status"] == "sent", str(row))
        check("不可达通道失败记录在日志", any(item["channel_id"] == bad["channel_id"] for item in svc.list_logs(20)))
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# 7) 通道熔断 + 令牌桶
# ---------------------------------------------------------------------------
def test_circuit_and_rate(root: str, server: FakeNotifyServer) -> None:
    section("7) 通道熔断 30min / 令牌桶 10 条/分")
    svc = make_service(root, "circuit", quiet_off=True)
    try:
        channel = add_webhook_channel(svc, server)
        channel_id = channel["channel_id"]
        server.reset()
        server.set_mode("fail_500")
        svc.on_event(make_event("run.finished", dedupe="circuit-1"))
        oid = _outbox(svc, "pending")[0]["outbox_id"]
        for _ in range(5):
            _rewind(svc, oid)
            svc.process_pending()
        health = svc.channel_health(channel_id)
        check("连续 5 次失败后通道熔断", health["circuit_open"] is True, str(health))
        check("熔断计数>=5", health["consecutive_fail"] >= 5, str(health))
        check("熔断窗口约 30min", 1700 <= int(health["circuit_open_until"] or 0) - int(time.time()) <= 1810, str(health))
        requests_before = server.requests_count
        _rewind(svc, oid)
        svc.process_pending()
        check("熔断期间跳过发送（无新请求）", server.requests_count == requests_before, str(server.requests_count))
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (oid,))[0]
        check("熔断期间通知仍保留在 outbox（pending）", row["status"] == "pending", str(row["status"]))
        check("熔断期间不消耗重试次数", int(row["attempts"]) == 5, str(row["attempts"]))
        # 模拟 30min 后：熔断关闭，恢复发送
        svc._circuits[channel_id]["open_until"] = time.time() - 1
        server.set_mode("ok")
        _rewind(svc, oid)
        svc.process_pending()
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (oid,))[0]
        check("熔断结束后恢复发送成功", row["status"] == "sent", str(row["status"]))
        check("成功后熔断计数清零", svc.channel_health(channel_id)["consecutive_fail"] == 0)

        # 令牌桶
        svc._buckets.clear()
        acquired = [svc._acquire_rate(channel_id) for _ in range(10)]
        check("令牌桶前 10 条放行", all(acquired), str(acquired))
        check("第 11 条被限流", svc._acquire_rate(channel_id) is False)
        check("_rate_available 反映限流状态", svc._rate_available(channel_id) is False)
        server.reset()
        server.set_mode("ok")
        svc.on_event(make_event("run.finished", dedupe="rate-1"))
        oid = _outbox(svc, "pending")[0]["outbox_id"]
        svc.process_pending()
        check("限流期间不发请求", server.requests_count == 0, str(server.requests_count))
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (oid,))[0]
        check("限流后通知重新排队（不消耗 attempts）", row["status"] == "pending" and int(row["attempts"] or 0) == 0, str(row))
        check("限流后 next_attempt 约 2s 后", 0 < int(row["next_attempt_ts"] or 0) - int(time.time()) <= 3, str(row["next_attempt_ts"]))
        # 清空桶 → 立即恢复
        svc._buckets[channel_id].clear()
        _rewind(svc, oid)
        svc.process_pending()
        row = svc.state.query("SELECT * FROM notify_outbox WHERE outbox_id = ?", (oid,))[0]
        check("令牌桶恢复后发送成功", row["status"] == "sent", str(row["status"]))
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# 8) worker 线程 / 生命周期
# ---------------------------------------------------------------------------
def test_worker_lifecycle(root: str, server: FakeNotifyServer) -> None:
    section("8) worker 线程 / start-shutdown 生命周期")
    svc = make_service(root, "worker", worker=True, quiet_off=True)
    try:
        channel = add_webhook_channel(svc, server)
        server.reset()
        server.set_mode("ok")
        svc.on_event(make_event("run.finished", dedupe="worker-1"))
        delivered = wait_for(lambda: server.requests_count >= 1, timeout=5.0)
        check("worker 线程自动发送事件", delivered, str(server.requests_count))
        sent = wait_for(lambda: len(_outbox(svc, "sent")) == 1, timeout=5.0)
        check("worker 发送后 outbox=sent", sent, str(_outbox(svc, "sent")))
        check("worker 记录 notify_log", len(svc.list_logs(5)) >= 1)
        worker = svc._worker
        check("worker 是 daemon 线程", worker is not None and worker.daemon, str(type(worker)))
        svc.shutdown()
        check("shutdown 后 worker 停止", worker is not None and not worker.is_alive())
        svc.shutdown()
        check("重复 shutdown 不抛异常", True)
        before = len(_outbox(svc))
        svc.on_event(make_event("run.finished", dedupe="after-shutdown-1"))
        check("停服后事件被忽略", len(_outbox(svc)) == before)
        _ = channel
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# 9) 测试发送 API + SMTP
# ---------------------------------------------------------------------------
def test_test_send_api(root: str, server: FakeNotifyServer) -> None:
    section("9) 测试发送 API（同步 / 不抛 500）")
    smtp = FakeSMTPRecorder().install()
    svc = make_service(root, "testsend", quiet_off=True)
    client = make_api(svc)
    try:
        channel = add_webhook_channel(svc, server)
        server.reset()
        server.set_mode("ok")
        resp = client.post(f"/api/notify/channels/{channel['channel_id']}/test")
        body = _json(resp)
        check("单通道测试发送 HTTP 200", resp.status_code == 200, resp.text)
        check("单通道测试发送 ok=true", body.get("ok") is True, str(body))
        check("单通道测试发送返回 latency_ms", isinstance(body.get("latency_ms"), int))
        check("单通道测试发送返回 http_status", body.get("http_status") == 200, str(body.get("http_status")))
        check("测试发送实际到达 fake 服务端", server.requests_count == 1)
        check("测试发送标题含品牌", "舆见 OpinionLens" in str((server.last_request() or {}).get("json", {}).get("title")), str((server.last_request() or {}).get("json")))

        server.set_mode("fail_500")
        resp = client.post(f"/api/notify/channels/{channel['channel_id']}/test")
        body = _json(resp)
        check("测试发送失败仍返回 HTTP 200（不抛 500）", resp.status_code == 200, resp.text)
        check("测试发送失败 ok=false", body.get("ok") is False, str(body))
        check("测试发送失败带 http_status=500", body.get("http_status") == 500, str(body))
        check("测试发送失败带 error", bool(body.get("error")), str(body))

        # 全部通道测试：一个成功一个网络失败
        bad = svc.create_channel(
            {"type": "webhook", "name": "不可达", "config": {}, "secret": "http://127.0.0.1:1/hook"}
        )
        server.set_mode("ok")
        body = _json(client.post("/api/notify/test"))
        check("全部通道测试返回 results", len(body.get("results") or []) == 2, str(body))
        check("全部通道测试部分失败 ok=false", body.get("ok") is False, str(body))
        check("全部通道测试 sent 计数正确", body.get("sent") == 1, str(body.get("sent")))
        check("全部通道测试顶层含 latency_ms/http_status 字段", "latency_ms" in body and "http_status" in body)
        client.delete(f"/api/notify/channels/{bad['channel_id']}")

        # 没有启用通道
        for item in svc.list_channels():
            client.delete(f"/api/notify/channels/{item['channel_id']}")
        body = _json(client.post("/api/notify/test"))
        check("没有启用通道时 ok=false 且带中文提示", body.get("ok") is False and "通道" in str(body.get("error")), str(body))

        # 密钥无法解密 → ok=false，不 500
        broken = svc.create_channel(
            {"type": "wecom_bot", "name": "坏密钥", "config": {}, "secret": "abcdef123456"}
        )
        svc.state.execute(
            "UPDATE notify_channels SET secret_enc = ? WHERE channel_id = ?",
            ("dpapi:not-a-valid-blob", broken["channel_id"]),
        )
        resp = client.post(f"/api/notify/channels/{broken['channel_id']}/test")
        body = _json(resp)
        check("密钥无法解密时测试发送 200 + ok=false", resp.status_code == 200 and body.get("ok") is False, resp.text)
        check("密钥无法解密错误信息可读", "密钥" in str(body.get("error")), str(body.get("error")))
        check("密钥无法解密测试失败不产生明文", "not-a-valid-blob" not in json.dumps(body, ensure_ascii=False))

        # ---- SMTP：真实 EmailMessage 构造 + 假 smtplib ----
        smtp.reset()
        resp = client.post(
            "/api/notify/channels",
            json={
                "type": "smtp",
                "name": "邮件通知",
                "config": {
                    "host": "smtp.example.com",
                    "port": 465,
                    "security": "ssl",
                    "username": "bot@example.com",
                    "from": "bot@example.com",
                    "to": ["ops@example.com", "boss@example.com"],
                },
                "secret": "authcode1234",
            },
        )
        smtp_channel = _json(resp).get("channel") or {}
        check("创建 SMTP 通道成功", bool(smtp_channel.get("channel_id")), resp.text)
        check("SMTP 响应不含明文授权码", "authcode1234" not in json.dumps(_json(resp), ensure_ascii=False))
        check("SMTP 响应 config 含 from/to", "from" in (smtp_channel.get("config") or {}) and "to" in (smtp_channel.get("config") or {}))
        resp = client.post(f"/api/notify/channels/{smtp_channel['channel_id']}/test")
        body = _json(resp)
        check("SMTP 测试发送 ok=true", body.get("ok") is True, str(body))
        check("SMTP 假客户端收到 1 封邮件", smtp.sent_count == 1, str(smtp.sent_count))
        message = smtp.last_message()
        subject = str(make_header(decode_header(str(message["Subject"])))) if message is not None else ""
        content = message.get_content() if message is not None else ""
        check("SMTP 主题 UTF-8 含中文品牌", "舆见 OpinionLens" in subject, subject)
        check("SMTP 正文含测试文案", "通知测试" in content or "测试消息" in content, content[:200])
        check("SMTP 收件人正确", message is not None and "ops@example.com" in str(message["To"]), str(message["To"] if message is not None else ""))
        check("SMTP 使用授权码登录", smtp.logins and smtp.logins[-1] == ("bot@example.com", "authcode1234"), str(smtp.logins[-1:]))
        check("SMTP_SSL 走 ssl 连接", smtp.instances and smtp.instances[-1].port == 465)

        smtp.reset()
        smtp.mode = "auth_fail"
        body = _json(client.post(f"/api/notify/channels/{smtp_channel['channel_id']}/test"))
        check("SMTP 认证失败 ok=false", body.get("ok") is False, str(body))
        check("SMTP 认证失败标记 permanent", body.get("permanent") is True, str(body))
        check("SMTP 认证失败错误可读", "认证" in str(body.get("error")), str(body.get("error")))

        smtp.reset()
        smtp.mode = "connect_error"
        body = _json(client.post(f"/api/notify/channels/{smtp_channel['channel_id']}/test"))
        check("SMTP 连接失败 ok=false", body.get("ok") is False, str(body))
        check("SMTP 连接失败归为可重试", body.get("permanent") is False, str(body))
        check("SMTP 连接失败错误信息不含明文密码", "authcode1234" not in str(body.get("error")), str(body.get("error")))
    finally:
        smtp.uninstall()
        _finish(svc)


# ---------------------------------------------------------------------------
# 10) outbox / logs / settings / retry API
# ---------------------------------------------------------------------------
def test_outbox_logs_settings_api(root: str, server: FakeNotifyServer) -> None:
    section("10) outbox / logs / settings / retry API")
    svc = make_service(root, "admin_api", quiet_off=True)
    client = make_api(svc)
    try:
        channel = add_webhook_channel(svc, server)
        server.reset()
        server.set_mode("ok")

        # settings
        resp = client.put(
            "/api/notify/settings",
            json={"enabled": True, "privacy_mode": "standard", "quiet_hours": [], "muted_until": 0},
        )
        body = _json(resp)
        check("PUT settings 返回完整配置", body.get("brand") == "舆见 OpinionLens" and "channels" in body, resp.text)
        check("PUT settings privacy_mode 生效", body.get("privacy_mode") == "standard", str(body.get("privacy_mode")))
        check("PUT settings quiet_hours 生效", body.get("quiet_hours") == [], str(body.get("quiet_hours")))
        resp = client.put("/api/notify/settings", json={"privacy_mode": "invalid_mode"})
        check("非法 privacy_mode 返回 422", resp.status_code == 422, resp.text)
        check("非法 privacy_mode 错误码正确", (_json(resp).get("error") or {}).get("code") == "NOTIFY_SETTINGS_INVALID")
        resp = client.put("/api/notify/settings", json={"quiet_hours": [{"start": "25:00", "end": "08:00"}]})
        check("非法静默时段返回 422", resp.status_code == 422, resp.text)
        client.put("/api/notify/settings", json={"privacy_mode": "minimal"})

        # rules 关闭再打开
        resp = client.put("/api/notify/rules", json={"rules": [{"event_type": "run.finished", "enabled": False}]})
        check("PUT rules 返回 rules", isinstance(_json(resp).get("rules"), list))
        resp = client.put("/api/notify/rules", json={"rules": [{"event_type": "no.such", "enabled": True}]})
        check("未知 event_type 返回 422", resp.status_code == 422, resp.text)
        # 恢复 run.finished 订阅；另关闭一个本段未用到的事件用于校验 config 反映开关
        client.put("/api/notify/rules", json={"rules": [{"event_type": "run.finished", "enabled": True}]})
        client.put("/api/notify/rules", json={"rules": [{"event_type": "run.failed", "enabled": False}]})

        # outbox：先产生一条 pending、一条 failed
        svc.on_event(make_event("run.finished", dedupe="admin-1"))
        resp = client.get("/api/notify/outbox", params={"status": "pending", "limit": 5})
        body = _json(resp)
        check("GET outbox 返回 items/outbox 双键", isinstance(body.get("items"), list) and body.get("outbox") == body.get("items"))
        check("GET outbox count 正确", body.get("count") == 1, str(body.get("count")))
        item = (body.get("items") or [])[0]
        check(
            "outbox 单条字段完整",
            {"outbox_id", "event_type", "severity", "title", "status", "attempts", "last_error", "created_at", "sent_at"} <= set(item),
            str(sorted(item.keys())),
        )
        resp = client.get("/api/notify/outbox", params={"status": "bogus"})
        check("非法 outbox status 返回 422", resp.status_code == 422, resp.text)
        resp = client.post("/api/notify/outbox/ob_missing/retry")
        check("重试不存在的 outbox 返回 404", resp.status_code == 404, resp.text)
        resp = client.post(f"/api/notify/outbox/{item['outbox_id']}/retry")
        check("重试 pending 通知返回 409", resp.status_code == 409, resp.text)

        # 正常发送 → logs
        svc.process_pending()
        resp = client.get("/api/notify/logs", params={"limit": 10})
        body = _json(resp)
        check("GET logs 返回 items/logs 双键", isinstance(body.get("items"), list) and body.get("logs") == body.get("items"))
        log_item = (body.get("items") or [])[0] if body.get("items") else {}
        check("log 记录含通道名称", log_item.get("channel_name") == "测试 Webhook", str(log_item.get("channel_name")))
        check("log 记录 ok=true", log_item.get("ok") is True)
        check("log 不含明文 webhook URL", server.base_url not in json.dumps(body, ensure_ascii=False))

        # 永久失败 → 手动重试成功
        server.set_mode("fail_401")
        svc.on_event(make_event("run.finished", dedupe="admin-fail-1"))
        svc.process_pending()
        failed = _outbox(svc, "failed")
        check("产生 1 条 failed 通知", len(failed) == 1, str(len(failed)))
        resp = client.post(f"/api/notify/outbox/{failed[0]['outbox_id']}/retry")
        check("手动重试失败通知成功", _json(resp).get("ok") is True, resp.text)
        check("手动重试后状态 pending", _json(resp).get("status") == "pending")
        server.set_mode("ok")
        svc.process_pending()
        check("手动重试后最终发送成功", len(_outbox(svc, "sent")) == 2, str(len(_outbox(svc, "sent"))))

        # config worker 统计
        config = _json(client.get("/api/notify/config"))
        worker = config.get("worker") or {}
        check("config worker 统计 sent/pending", "sent" in worker and "pending" in worker, str(worker))
        check("config 已发送时间戳有值", worker.get("last_sent_at") is not None, str(worker))
        check("config rules 反映订阅开关", any(not item.get("enabled") for item in (config.get("rules") or [])))
        _ = channel
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# 11) 多通道发送语义
# ---------------------------------------------------------------------------
def test_multi_channel(root: str, server: FakeNotifyServer) -> None:
    section("11) 多通道发送语义 / 删除通道兜底")
    svc = make_service(root, "multi", quiet_off=True)
    client = make_api(svc)
    try:
        first = add_webhook_channel(svc, server, name="通道A", path="/a")
        second = add_webhook_channel(svc, server, name="通道B", path="/b")
        server.reset()
        server.set_mode("ok")
        svc.on_event(make_event("run.finished", dedupe="multi-1"))
        svc.process_pending()
        check("多通道全部发送成功", server.requests_count == 2, str(server.requests_count))
        check("多通道发送后 outbox=sent", len(_outbox(svc, "sent")) == 1)
        check("多通道发送写 2 条 notify_log", len([x for x in svc.list_logs(10) if x["outbox_id"] == _outbox(svc, "sent")[0]["outbox_id"]]) == 2)

        # 入队后禁用其中一个通道
        server.reset()
        svc.on_event(make_event("run.finished", dedupe="multi-2"))
        client.patch(f"/api/notify/channels/{second['channel_id']}", json={"enabled": False})
        svc.process_pending()
        check("禁用通道后只向启用通道发送", server.requests_count == 1, str(server.requests_count))
        row = [item for item in _outbox(svc, "sent") if item["dedupe_key"] == "multi-2"][0]
        oid = row["outbox_id"]
        logs = [item for item in svc.list_logs(20) if item["outbox_id"] == oid]
        check("禁用通道写入失败日志", len(logs) == 2 and any(not item["ok"] for item in logs), str(len(logs)))
        check("禁用通道不阻塞发送成功", row["status"] == "sent")

        # 入队后删除全部通道 → 队列标记 failed
        svc.on_event(make_event("run.finished", dedupe="multi-3"))
        client.delete(f"/api/notify/channels/{first['channel_id']}")
        client.delete(f"/api/notify/channels/{second['channel_id']}")
        svc.process_pending()
        failed = [item for item in _outbox(svc, "failed") if item["dedupe_key"] == "multi-3"]
        check("通道全部删除后通知标记 failed", len(failed) == 1, str(len(failed)))
        check("通道删除失败原因可读", "通道" in failed[0]["last_error"], failed[0]["last_error"])
    finally:
        _finish(svc)


# ---------------------------------------------------------------------------
# run / main
# ---------------------------------------------------------------------------
def run(report: Optional[Any] = None) -> Tuple[int, int]:
    """执行全部离线测试，返回 ``(passed, failed)``。"""
    global _failures, _passes
    _passes = 0
    _failures = 0
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 - 非标准流时忽略
                pass
    print("=" * 80)
    print("舆见 OpinionLens · 通知后端离线自测（不联网 / 不发真邮件）")
    print("=" * 80)

    root = tempfile.mkdtemp(prefix="notify_test_")
    server = FakeNotifyServer().start()
    saved_env = {key: os.environ.get(key) for key in (TEST_ENV, TEST_HTTP_BASE_ENV)}
    os.environ[TEST_ENV] = "1"
    os.environ[TEST_HTTP_BASE_ENV] = server.base_url
    print(f"临时目录：{root}")
    print(f"fake 通知服务端：{server.base_url}")
    sections = (
        test_validation,
        test_channels_api,
        test_rules_and_matching,
        test_quiet_hours,
        test_render_privacy,
        test_send_and_retry,
        test_circuit_and_rate,
        test_worker_lifecycle,
        test_test_send_api,
        test_outbox_logs_settings_api,
        test_multi_channel,
    )
    try:
        for function in sections:
            try:
                function(root, server)
            except Exception as exc:  # noqa: BLE001 - 单段异常不阻断其它测试
                traceback.print_exc()
                check(f"{function.__name__} 执行未抛异常", False, repr(exc))
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 80)
    if _failures:
        print(f"测试失败：{_failures} 项（通过 {_passes} 项）")
    else:
        print(f"全部通过：{_passes} 项断言")
    print("=" * 80)
    if callable(report):
        try:
            report({"passed": _passes, "failed": _failures})
        except Exception:  # noqa: BLE001
            pass
    return _passes, _failures


def main() -> int:
    _passed, failed = run()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

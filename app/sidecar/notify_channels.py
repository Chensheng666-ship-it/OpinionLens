# -*- coding: utf-8 -*-
"""通知通道适配器（舆见 OpinionLens）。

支持类型（契约 §3）：
* ``smtp``       邮件（smtplib：465 SSL / 587 STARTTLS / 明文 25），UTF-8 主题，超时 10s；
* ``wecom_bot``  企业微信群机器人 Webhook（markdown 消息）；
* ``serverchan`` Server酱（SendKey）；
* ``bark``       Bark（iOS，支持自建 host）；
* ``webhook``    通用 Webhook（POST JSON，URL 本身作为密钥）。

设计约束（硬性）：
1. **只用标准库**：``urllib.request`` / ``smtplib`` / ``email.message.EmailMessage``，不引第三方依赖；
2. **出站白名单**：生产环境默认仅 ``https`` + 内置 host 白名单（``qyapi.weixin.qq.com`` /
   ``sctapi.ftqq.com`` / ``api.day.app`` / 自建 bark host / 通用 webhook 仅 https）；
   测试模式（环境变量 ``NOTIFY_TEST_MODE=1``）放开 http 与任意 host，供离线 fake server 使用；
   测试模式下可用 ``NOTIFY_TEST_HTTP_BASE=http://127.0.0.1:<port>`` 覆盖内置 host；
3. **密钥不出本模块**：secret 只作为内存参数传入；日志/错误/审计里出现的 URL 一律脱敏
   （只保留 scheme + host + 路径模板），异常文本会去掉 secret 串与本地路径；
4. 发送失败不抛异常：统一返回 :class:`SendResult`，由 ``notify_service`` 决定重试/熔断。

通道密钥约定（``secret``）：
* ``smtp``：SMTP 授权码/密码（有 username 时必填）；
* ``wecom_bot``：群机器人 Webhook 里的 ``key``（也允许直接粘贴完整 Webhook URL，自动提取）；
* ``serverchan``：``SCT`` 开头的 SendKey（也允许粘贴完整 URL）；
* ``bark``：Device Key（也允许粘贴完整 URL）；
* ``webhook``：完整 URL（URL 即密钥，因此按 secret 存储/脱敏）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any, Dict, List, Optional, Tuple

from typing_utils import as_dict

log = logging.getLogger("sidecar.notify.channels")

BRAND = "舆见 OpinionLens"
TEST_ENV = "NOTIFY_TEST_MODE"
TEST_HTTP_BASE_ENV = "NOTIFY_TEST_HTTP_BASE"
DEFAULT_TIMEOUT = 10.0
MAX_ERROR_LEN = 300
USER_AGENT = "OpinionLens-Notifier/1.0"

# 生产环境内置通道 host 白名单（契约 §3 / 参考 04 §4.6）
ALLOWED_HOSTS: Dict[str, Tuple[str, ...]] = {
    "wecom_bot": ("qyapi.weixin.qq.com",),
    "serverchan": ("sctapi.ftqq.com",),
    "bark": ("api.day.app",),  # 自建 bark host 由 config["host"] 指定（仍要求 https）
    # webhook 不限制 host，但生产只允许 https
}

# 通道类型注册表（API/设置页渲染用；不含任何密钥字段）
CHANNEL_TYPES: Dict[str, Dict[str, Any]] = {
    "smtp": {
        "label": "邮件 SMTP",
        "secret_label": "SMTP 授权码 / 密码",
        "secret_required": False,  # 有 username 时才必填
        "fields": ["host", "port", "security", "username", "from", "to"],
        "hint": "465 端口用 SSL、587 端口用 STARTTLS；QQ/163 等邮箱需使用授权码。",
    },
    "wecom_bot": {
        "label": "企业微信机器人",
        "secret_label": "Webhook Key",
        "secret_required": True,
        "fields": [],
        "hint": "企业微信群机器人 Webhook 中 key 参数；也可直接粘贴完整 Webhook URL。",
    },
    "serverchan": {
        "label": "Server酱",
        "secret_label": "SendKey",
        "secret_required": True,
        "fields": [],
        "hint": "Server酱³ 的 SendKey（SCT 开头），推送到微信。",
    },
    "bark": {
        "label": "Bark（iOS）",
        "secret_label": "Device Key",
        "secret_required": True,
        "fields": ["server_url", "group", "level", "sound"],
        "hint": "Bark App 中的 Device Key；可填自建服务器地址（如 https://bark.example.com）。",
    },
    "webhook": {
        "label": "通用 Webhook",
        "secret_label": "Webhook URL（含令牌，按密钥保存）",
        "secret_required": True,
        "fields": [],
        "hint": "仅 https；推送 POST JSON：{title, body, source, ts}。",
    },
}

BARK_LEVELS = ("active", "timeSensitive", "critical", "passive")
SMTP_SECURITIES = ("ssl", "starttls", "none")

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-.]{0,251}[A-Za-z0-9])?$")
_WECOM_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{8,128}$")
_SCT_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{6,128}$")
_BARK_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{4,128}$")
_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:\\[^\s，,；;、）)]*"),  # Windows 绝对路径
    re.compile(r"/(?:home|Users|private|var|tmp|opt)/[^\s，,；;、）)]*"),  # POSIX 路径
)
_SECRET_QUERY_RE = re.compile(
    r"(?i)(key|token|access_token|sendkey|send_key|device_key|secret|password|passwd|pwd|auth)=([^&\s'\"#]+)"
)


class ChannelConfigError(ValueError):
    """通道配置不合法（URL 不在白名单、必填字段缺失等）。"""


@dataclass
class SendResult:
    """一次发送的结果；``permanent=True`` 表示不可重试（4xx/认证失败/配置错误）。"""

    ok: bool
    permanent: bool = False
    http_status: Optional[int] = None
    latency_ms: int = 0
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "permanent": bool(self.permanent),
            "http_status": self.http_status,
            "latency_ms": int(self.latency_ms or 0),
            "error": self.error or "",
        }


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "y")


def test_mode(explicit: Optional[bool] = None) -> bool:
    """是否测试模式：显式参数优先，否则读 ``NOTIFY_TEST_MODE``。"""
    if explicit is not None:
        return bool(explicit)
    return _truthy(os.environ.get(TEST_ENV))


def test_http_base() -> str:
    """测试模式下覆盖内置通道 host 的本地 fake server 基址。"""
    base = str(os.environ.get(TEST_HTTP_BASE_ENV) or "").strip()
    return base.rstrip("/") if base else "http://127.0.0.1:9"


def normalize_secret(channel_type: str, secret: Any) -> str:
    """落库前规范化密钥：内置通道允许用户粘贴完整 URL，只保存真正的 token。

    * ``wecom_bot`` / ``serverchan`` / ``bark``：从完整 URL 提取 key / sendkey / device key；
    * ``webhook``：URL 本身就是密钥，原样保存；
    * ``smtp``：授权码/密码原样保存。
    """
    ctype = str(channel_type or "").strip().lower()
    if ctype in ("wecom_bot", "serverchan", "bark"):
        return _extract_secret(secret, ctype)
    return str(secret or "")


def channel_type_catalog() -> List[Dict[str, Any]]:
    """给设置页的通道类型清单（无密钥）。"""
    items = []
    for ctype, meta in CHANNEL_TYPES.items():
        items.append(
            {
                "type": ctype,
                "label": meta["label"],
                "secret_label": meta["secret_label"],
                "secret_required": bool(meta["secret_required"]),
                "fields": list(meta["fields"]),
                "hint": meta["hint"],
            }
        )
    return items


def strip_paths(text: Any) -> str:
    """去掉文本里的本地绝对路径（通知外发绝不带本地路径）。"""
    result = str(text or "")
    for pattern in _PATH_PATTERNS:
        result = pattern.sub("<路径>", result)
    return result


def sanitize_text(text: Any, secrets: Any = (), max_len: int = MAX_ERROR_LEN) -> str:
    """日志/错误脱敏：隐藏 secret、URL 路径与 query、本地路径，并截断。

    用于 ``notify_log.error`` / ``outbox.last_error`` / 日志输出，保证：
    * 不出现明文密钥（配置里的 secret 与常见 ``key=...`` 形式）；
    * URL 只保留 ``scheme://host/***``（不带 query、不带路径里的 key）；
    * 不出现本地绝对路径。
    """
    result = str(text or "")
    if isinstance(secrets, str):
        secrets = (secrets,)
    for item in secrets or ():
        secret = str(item or "")
        if secret and secret in result:
            result = result.replace(secret, "***")
    result = _SECRET_QUERY_RE.sub(lambda m: f"{m.group(1)}=***", result)

    def _mask_url(match: "re.Match[str]") -> str:
        try:
            parsed = urllib.parse.urlsplit(match.group(0))
            if parsed.hostname:
                return f"{parsed.scheme}://{parsed.hostname}/***"
        except ValueError:
            pass
        return "***"

    result = re.sub(r"https?://[^\s'\"<>]+", _mask_url, result)
    result = strip_paths(result)
    result = re.sub(r"\s+", " ", result).strip()
    return result[: max(16, int(max_len or MAX_ERROR_LEN))]


def _parse_port(value: Any, default: int) -> int:
    if value in (None, ""):
        return int(default)
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ChannelConfigError(f"端口必须是整数：{value!r}") from exc
    if not 1 <= port <= 65535:
        raise ChannelConfigError(f"端口超出范围（1-65535）：{port}")
    return port


def _validate_email(value: Any, field_label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ChannelConfigError(f"{field_label}不能为空")
    if text.count("@") != 1 or " " in text:
        raise ChannelConfigError(f"{field_label}格式不正确：{text[:80]}")
    local, _, domain = text.partition("@")
    if not local or not domain or "." not in domain:
        raise ChannelConfigError(f"{field_label}格式不正确：{text[:80]}")
    return text


def _normalize_to_addrs(raw: Any) -> List[str]:
    items: List[str] = []
    if isinstance(raw, (list, tuple)):
        candidates = [str(item) for item in raw]
    else:
        candidates = re.split(r"[,;，；\s]+", str(raw or ""))
    for candidate in candidates:
        text = candidate.strip()
        if not text:
            continue
        items.append(_validate_email(text, "收件人地址"))
    if not items:
        raise ChannelConfigError("收件人地址（to）不能为空")
    return items


def _str_value(source: Dict[str, Any], *keys: str) -> str:
    """按别名顺序取第一个非空字符串（兼容前端短名/旧字段名）。"""
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _bark_host(config: Dict[str, Any]) -> str:
    """Bark 自建服务器 host：兼容 ``server_url``（完整 URL）与 ``host`` 两种写法。"""
    server_url = str(config.get("server_url") or "").strip()
    if server_url:
        text = server_url if "://" in server_url else f"https://{server_url}"
        try:
            parsed = urllib.parse.urlsplit(text)
        except ValueError:
            return ""
        return str(parsed.hostname or "").strip()
    return str(config.get("host") or "").strip()


def _validate_host(host: Any, field_label: str = "host") -> str:
    text = str(host or "").strip()
    if not text:
        raise ChannelConfigError(f"{field_label} 不能为空")
    if "://" in text or "/" in text:
        raise ChannelConfigError(f"{field_label} 只填主机名（不要带 http:// 或路径）：{text[:80]}")
    if not _HOST_RE.match(text):
        raise ChannelConfigError(f"{field_label} 格式不正确：{text[:80]}")
    return text


def _validate_http_url(url: str, tm: bool, allow_http_hosts: Tuple[str, ...] = ()) -> "urllib.parse.SplitResult":
    """校验出站 URL：生产只允许 https；测试模式允许 http/任意 host。"""
    text = str(url or "").strip()
    if not text:
        raise ChannelConfigError("Webhook URL 不能为空")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError as exc:
        raise ChannelConfigError("Webhook URL 格式不正确") from exc
    if parsed.scheme not in ("http", "https"):
        raise ChannelConfigError("Webhook URL 必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise ChannelConfigError("Webhook URL 缺少主机名")
    if parsed.username or parsed.password:
        raise ChannelConfigError("Webhook URL 不允许内嵌用户名/密码（请用独立的密钥字段）")
    if not tm:
        if parsed.scheme != "https":
            raise ChannelConfigError("生产环境仅允许 https 出站地址（测试模式可放开 http）")
        if allow_http_hosts and parsed.hostname not in allow_http_hosts:
            raise ChannelConfigError(f"出站 host 不在白名单：{parsed.hostname}")
    return parsed


def _extract_secret(secret: Any, channel_type: str) -> str:
    """从用户输入中提取真正的密钥（兼容直接粘贴完整 URL）。"""
    raw = str(secret or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        if channel_type == "wecom_bot" and raw.lower().startswith("key="):
            raw = raw.split("=", 1)[1].strip()
        if channel_type == "serverchan" and raw.lower().endswith(".send"):
            raw = raw[: -len(".send")]
        return raw
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return raw
    if channel_type == "wecom_bot":
        values = urllib.parse.parse_qs(parsed.query).get("key") or []
        return str(values[0]).strip() if values else ""
    if channel_type == "serverchan":
        segment = parsed.path.rsplit("/", 1)[-1]
        if segment.lower().endswith(".send"):
            segment = segment[: -len(".send")]
        return segment.strip()
    if channel_type == "bark":
        segments = [item for item in parsed.path.split("/") if item]
        return segments[0] if segments else ""
    return raw


# ---------------------------------------------------------------------------
# 配置校验（POST/PATCH 通道时调用）
# ---------------------------------------------------------------------------
def validate_config(
    channel_type: str,
    config: Optional[Dict[str, Any]] = None,
    secret: Any = "",
    test_mode_: Optional[bool] = None,
    require_secret: bool = True,
) -> Dict[str, Any]:
    """校验并规范化通道配置。

    :param channel_type: 5 类之一
    :param config: 非敏感配置（host/port/from_addr/to_addrs/group/level 等）
    :param secret: 明文密钥；``require_secret=False`` 且为空时表示“沿用旧密钥，不校验”
    :return: 规范化后的 config dict
    :raises ChannelConfigError: 配置不合法（API 层转 422 ``NOTIFY_CHANNEL_INVALID``）
    """
    ctype = str(channel_type or "").strip().lower()
    if ctype not in CHANNEL_TYPES:
        raise ChannelConfigError(
            "不支持的通道类型：%s（可选：%s）" % (ctype or "(空)", "、".join(CHANNEL_TYPES))
        )
    source = dict(as_dict(config or {}))
    raw_secret = str(secret) if secret is not None else ""
    tm = test_mode(test_mode_)

    # ---------------- SMTP ----------------
    if ctype == "smtp":
        host = _validate_host(source.get("host"), "SMTP 服务器 host")
        security = str(source.get("security") or "ssl").strip().lower()
        if security not in SMTP_SECURITIES:
            raise ChannelConfigError(
                "SMTP 加密方式只支持 ssl / starttls / none：%s" % security
            )
        default_port = {"ssl": 465, "starttls": 587, "none": 25}[security]
        port = _parse_port(source.get("port"), default_port)
        username = _str_value(source, "username", "user")
        from_addr = _validate_email(
            _str_value(source, "from", "from_addr", "from_email", "sender"), "发件人地址（from）"
        )
        to_addrs = _normalize_to_addrs(source.get("to", source.get("to_addrs")))
        if username:
            # 主流服务商（163/QQ/Gmail/企业邮）都要求「登录名 = 完整邮箱」且「发件人 = 登录名」；
            # 非 ASCII 用户名还会让 smtplib 在 AUTH 编码时抛 UnicodeEncodeError（显示为 ascii codec 错误）。
            # 在保存阶段就给出明确提示，避免到服务器才报 535 / 553。
            try:
                username.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ChannelConfigError(
                    f"SMTP 用户名只能是英文邮箱地址（如 {from_addr}），不能包含中文：{username[:80]}"
                ) from exc
            if "@" not in username:
                raise ChannelConfigError(
                    f"SMTP 用户名应填写完整邮箱地址（通常与发件人相同：{from_addr}），当前为：{username[:80]}"
                )
            if username.lower() != from_addr.lower():
                raise ChannelConfigError(
                    f"发件人（{from_addr}）必须与 SMTP 用户名（{username[:80]}）一致，"
                    "否则 163/QQ 等服务商会拒绝投递（553 Mail from must equal authorized user）"
                )
        elif raw_secret:
            # 填了授权码但用户名留空：按发件人地址登录（与服务商要求一致）
            username = from_addr
        if require_secret and username and not raw_secret:
            raise ChannelConfigError("SMTP 已填写用户名，必须同时填写授权码/密码")
        timeout = source.get("timeout") or DEFAULT_TIMEOUT
        try:
            timeout = max(3.0, min(30.0, float(timeout)))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        return {
            "host": host,
            "port": port,
            "security": security,
            "username": username,
            "from": from_addr,
            "to": to_addrs,
            "timeout": timeout,
        }

    # ---------------- 企业微信机器人 ----------------
    if ctype == "wecom_bot":
        key = _extract_secret(raw_secret, ctype)
        if not key:
            if require_secret:
                raise ChannelConfigError("企业微信机器人 Key 不能为空（Webhook URL 中的 key 参数）")
        elif not _WECOM_KEY_RE.match(key):
            raise ChannelConfigError("企业微信机器人 Key 格式不正确（应为字母/数字/中划线）")
        return {}

    # ---------------- Server酱 ----------------
    if ctype == "serverchan":
        key = _extract_secret(raw_secret, ctype)
        if not key:
            if require_secret:
                raise ChannelConfigError("Server酱 SendKey 不能为空")
        elif not _SCT_KEY_RE.match(key):
            raise ChannelConfigError("Server酱 SendKey 格式不正确（不应包含空格或斜杠）")
        return {}

    # ---------------- Bark ----------------
    if ctype == "bark":
        key = _extract_secret(raw_secret, ctype)
        if not key:
            if require_secret:
                raise ChannelConfigError("Bark Device Key 不能为空")
        elif not _BARK_KEY_RE.match(key):
            raise ChannelConfigError("Bark Device Key 格式不正确（不应包含空格或斜杠）")
        server_url = str(source.get("server_url") or "").strip()
        if server_url:
            text = server_url if "://" in server_url else f"https://{server_url}"
            parsed = _validate_http_url(text, tm)
            server_url = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
            host = str(parsed.hostname or "")
        elif source.get("host"):
            host = _validate_host(source.get("host"), "Bark 服务器 host")
            server_url = "" if host == "api.day.app" else f"https://{host}"
        else:
            host = "api.day.app"
            server_url = ""
        group = str(source.get("group") or BRAND).strip() or BRAND
        if len(group) > 64:
            group = group[:64]
        level = str(source.get("level") or "active").strip()
        if level not in BARK_LEVELS:
            raise ChannelConfigError("Bark level 只支持 %s" % " / ".join(BARK_LEVELS))
        sound = str(source.get("sound") or "").strip()[:64]
        normalized = {"server_url": server_url, "host": host, "group": group, "level": level}
        if sound:
            normalized["sound"] = sound
        return normalized

    # ---------------- 通用 Webhook ----------------
    if ctype == "webhook":
        url = raw_secret.strip()
        if not url:
            if require_secret:
                raise ChannelConfigError("通用 Webhook URL 不能为空（URL 可作为密钥保存）")
            return {}
        _validate_http_url(url, tm)
        return {}

    raise ChannelConfigError("不支持的通道类型：%s" % ctype)  # pragma: no cover


def resolve_endpoint(
    channel_type: str,
    config: Optional[Dict[str, Any]] = None,
    secret: Any = "",
    title: str = "",
    body: str = "",
    test_mode_: Optional[bool] = None,
) -> str:
    """构造并校验最终出站 URL（内置通道 host 由白名单固定；测试模式走 fake base）。"""
    ctype = str(channel_type or "").strip().lower()
    source = dict(as_dict(config or {}))
    tm = test_mode(test_mode_)
    key = _extract_secret(secret, ctype)
    if ctype == "wecom_bot":
        base = test_http_base() if tm else "https://qyapi.weixin.qq.com"
        return f"{base}/cgi-bin/webhook/send?key={urllib.parse.quote(key, safe='')}"
    if ctype == "serverchan":
        base = test_http_base() if tm else "https://sctapi.ftqq.com"
        return f"{base}/{urllib.parse.quote(key, safe='')}.send"
    if ctype == "bark":
        if tm:
            base = test_http_base()
        else:
            host = _bark_host(source) or "api.day.app"
            _validate_host(host, "Bark 服务器 host")
            base = f"https://{host}"
        query = urllib.parse.urlencode(
            {
                "group": str(source.get("group") or BRAND),
                "level": str(source.get("level") or "active"),
                "sound": str(source.get("sound") or ""),
            }
        )
        return (
            f"{base}/{urllib.parse.quote(key, safe='')}/"
            f"{urllib.parse.quote(str(title or ''), safe='')}/"
            f"{urllib.parse.quote(str(body or ''), safe='')}?{query}"
        )
    if ctype == "webhook":
        url = str(secret or "").strip()
        _validate_http_url(url, tm)
        return url
    if ctype == "smtp":
        raise ChannelConfigError("SMTP 通道不使用 URL 出站")
    raise ChannelConfigError("不支持的通道类型：%s" % ctype)


def safe_endpoint(
    channel_type: str,
    config: Optional[Dict[str, Any]] = None,
    secret: Any = "",
    test_mode_: Optional[bool] = None,
) -> str:
    """日志用端点描述：只保留 scheme + host + 路径模板（不含 query/密钥）。"""
    ctype = str(channel_type or "").strip().lower()
    source = dict(as_dict(config or {}))
    tm = test_mode(test_mode_)
    try:
        if ctype == "smtp":
            host = str(source.get("host") or "");
            port = source.get("port") or "";
            security = source.get("security") or "ssl"
            return f"smtp://{host}:{port}（{security}）"
        if ctype == "wecom_bot":
            base = test_http_base() if tm else "https://qyapi.weixin.qq.com"
            return base + "/cgi-bin/webhook/send"
        if ctype == "serverchan":
            base = test_http_base() if tm else "https://sctapi.ftqq.com"
            return base + "/***.send"
        if ctype == "bark":
            if tm:
                return test_http_base() + "/***"
            host = _bark_host(source) or "api.day.app"
            return f"https://{host}/***"
        if ctype == "webhook":
            parsed = urllib.parse.urlsplit(str(secret or ""))
            if parsed.hostname:
                return f"{parsed.scheme}://{parsed.hostname}/***"
            return "webhook://***"
    except Exception:  # noqa: BLE001 - 脱敏失败只影响日志可读性
        pass
    return str(ctype or "unknown")


# ---------------------------------------------------------------------------
# HTTP 底层
# ---------------------------------------------------------------------------
def _read_body(raw: bytes, limit: int = 4096) -> str:
    try:
        return raw[:limit].decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _parse_json(raw: bytes) -> Any:
    text = _read_body(raw)
    if not text:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _request(
    url: str,
    data: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    method: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Tuple[int, bytes]:
    final_headers = {"User-Agent": USER_AGENT}
    final_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=final_headers, method=method)
    with urllib.request.urlopen(request, timeout=float(timeout)) as response:  # noqa: S310 - 已过白名单校验
        return int(getattr(response, "status", 200) or 200), response.read(8192)


def _post_json(
    url: str, payload: Dict[str, Any], timeout: float = DEFAULT_TIMEOUT
) -> Tuple[int, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    status, raw = _request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        timeout=timeout,
    )
    return status, _parse_json(raw)


def _get_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> Tuple[int, Any]:
    status, raw = _request(url, method="GET", timeout=timeout)
    return status, _parse_json(raw)


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------
def _smtp_message(
    config: Dict[str, Any], title: str, body: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = str(title or f"{BRAND} 通知")
    message["From"] = formataddr((BRAND, str(config.get("from") or "")))
    message["To"] = ", ".join(str(item) for item in (config.get("to") or []))
    text = f"{title}\n\n{body}\n\n—— 本邮件由「{BRAND}」自动发送。"
    message.set_content(text, charset="utf-8")
    return message


def _send_smtp(
    config: Dict[str, Any], secret: str, title: str, body: str, timeout: float
) -> SendResult:
    normalized = validate_config("smtp", config, secret, test_mode_=True, require_secret=False)
    host = str(normalized["host"])
    port = int(normalized["port"])
    security = str(normalized["security"])
    username = str(normalized.get("username") or "")
    if not username and secret:
        username = str(normalized.get("from") or "")
    message = _smtp_message(normalized, title, body)
    client: Any = None
    try:
        if security == "ssl":
            client = smtplib.SMTP_SSL(
                host, port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            if security == "starttls":
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
        if username:
            client.login(username, str(secret or ""))
        client.send_message(message)
        return SendResult(ok=True)
    finally:
        if client is not None:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - quit 失败不影响发送结果
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# 各通道发送实现
# ---------------------------------------------------------------------------
def _send_wecom_bot(
    config: Dict[str, Any], secret: str, title: str, body: str, timeout: float, tm: bool
) -> SendResult:
    url = resolve_endpoint("wecom_bot", config, secret, test_mode_=tm)
    payload = {
        "msgtype": "markdown",
        "markdown": {"content": f"### {title}\n\n{body}"},
    }
    status, doc = _post_json(url, payload, timeout=timeout)
    if isinstance(doc, dict):
        errcode = doc.get("errcode")
        if errcode not in (0, "0", None):
            errmsg = sanitize_text(doc.get("errmsg") or "", secrets=(secret,), max_len=120)
            return SendResult(
                ok=False,
                permanent=True,
                http_status=status,
                error=f"企业微信返回 errcode={errcode}：{errmsg or '未知错误'}",
            )
    return SendResult(ok=True, http_status=status)


def _send_serverchan(
    config: Dict[str, Any], secret: str, title: str, body: str, timeout: float, tm: bool
) -> SendResult:
    url = resolve_endpoint("serverchan", config, secret, test_mode_=tm)
    payload = {"title": str(title or BRAND), "desp": str(body or "")}
    status, doc = _post_json(url, payload, timeout=timeout)
    if isinstance(doc, dict):
        code = doc.get("code", doc.get("errno"))
        if code not in (0, "0", None):
            message = sanitize_text(
                doc.get("message") or doc.get("errmsg") or "", secrets=(secret,), max_len=120
            )
            return SendResult(
                ok=False,
                permanent=True,
                http_status=status,
                error=f"Server酱返回 code={code}：{message or '未知错误'}",
            )
    return SendResult(ok=True, http_status=status)


def _send_bark(
    config: Dict[str, Any], secret: str, title: str, body: str, timeout: float, tm: bool
) -> SendResult:
    url = resolve_endpoint("bark", config, secret, title=title, body=body, test_mode_=tm)
    status, doc = _get_json(url, timeout=timeout)
    if isinstance(doc, dict):
        code = doc.get("code")
        if code is not None:
            try:
                code_int = int(code)
            except (TypeError, ValueError):
                code_int = 0
            if code_int != 200:
                message = sanitize_text(
                    doc.get("message") or "", secrets=(secret,), max_len=120
                )
                return SendResult(
                    ok=False,
                    permanent=code_int < 500,
                    http_status=status,
                    error=f"Bark 返回 code={code_int}：{message or '未知错误'}",
                )
    return SendResult(ok=True, http_status=status)


def _send_webhook(
    config: Dict[str, Any], secret: str, title: str, body: str, timeout: float, tm: bool
) -> SendResult:
    url = resolve_endpoint("webhook", config, secret, test_mode_=tm)
    payload = {
        "source": BRAND,
        "title": str(title or ""),
        "body": str(body or ""),
        "ts": int(time.time()),
    }
    status, _doc = _post_json(url, payload, timeout=timeout)
    return SendResult(ok=True, http_status=status)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def send(
    channel: Dict[str, Any],
    title: str,
    body: str,
    timeout: Optional[float] = None,
    test_mode_: Optional[bool] = None,
) -> SendResult:
    """发送一条通知，永不抛异常。

    :param channel: ``{"type", "config": dict, "secret": 明文}``（secret 只在内存中）
    :return: :class:`SendResult`
    """
    started = time.perf_counter()
    spec = as_dict(channel)
    ctype = str(spec.get("type") or "").strip().lower()
    config = as_dict(spec.get("config"))
    secret = str(spec.get("secret") or "")
    tm = test_mode(test_mode_)
    timeout_value = DEFAULT_TIMEOUT
    try:
        timeout_value = float(timeout if timeout is not None else config.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout_value = DEFAULT_TIMEOUT
    try:
        if ctype == "smtp":
            result = _send_smtp(config, secret, title, body, timeout_value)
        elif ctype == "wecom_bot":
            result = _send_wecom_bot(config, secret, title, body, timeout_value, tm)
        elif ctype == "serverchan":
            result = _send_serverchan(config, secret, title, body, timeout_value, tm)
        elif ctype == "bark":
            result = _send_bark(config, secret, title, body, timeout_value, tm)
        elif ctype == "webhook":
            result = _send_webhook(config, secret, title, body, timeout_value, tm)
        elif not ctype:
            result = SendResult(ok=False, permanent=True, error="通知通道缺少 type 字段")
        else:
            result = SendResult(ok=False, permanent=True, error=f"不支持的通道类型：{ctype}")
    except ChannelConfigError as exc:
        result = SendResult(
            ok=False, permanent=True, error=sanitize_text(str(exc), secrets=(secret,))
        )
    except urllib.error.HTTPError as exc:
        status = int(getattr(exc, "code", 0) or 0)
        try:
            snippet = _read_body(exc.read(200) or b"")
        except Exception:  # noqa: BLE001
            snippet = ""
        permanent = 400 <= status < 500 and status not in (408, 425, 429)
        result = SendResult(
            ok=False,
            permanent=permanent,
            http_status=status,
            error=sanitize_text(f"HTTP {status} {snippet}".strip(), secrets=(secret,)),
        )
    except urllib.error.URLError as exc:
        result = SendResult(
            ok=False,
            permanent=False,
            error=sanitize_text(f"网络错误：{getattr(exc, 'reason', exc)}", secrets=(secret,)),
        )
    except (socket.timeout, TimeoutError):
        result = SendResult(ok=False, permanent=False, error="请求超时（10 秒）")
    except smtplib.SMTPAuthenticationError as exc:
        result = SendResult(
            ok=False,
            permanent=True,
            error=sanitize_text(f"SMTP 认证失败：{exc.smtp_code} {exc.smtp_error!r}", secrets=(secret,)),
        )
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPNotSupportedError) as exc:
        result = SendResult(
            ok=False, permanent=True, error=sanitize_text(f"SMTP 拒绝投递：{exc}", secrets=(secret,))
        )
    except smtplib.SMTPException as exc:
        result = SendResult(
            ok=False, permanent=False, error=sanitize_text(f"SMTP 发送失败：{exc}", secrets=(secret,))
        )
    except OSError as exc:
        result = SendResult(
            ok=False, permanent=False, error=sanitize_text(f"连接失败：{exc}", secrets=(secret,))
        )
    except Exception as exc:  # noqa: BLE001 - 发送路径绝不抛出
        log.warning(
            "通知发送异常：channel=%s type=%s error=%s",
            str(spec.get("channel_id") or ""),
            ctype or "unknown",
            sanitize_text(str(exc), secrets=(secret,)),
        )
        result = SendResult(
            ok=False, permanent=False, error=sanitize_text(f"发送异常：{exc}", secrets=(secret,))
        )
    result.latency_ms = int(round((time.perf_counter() - started) * 1000))
    if not result.ok:
        log.info(
            "通知发送失败：channel=%s type=%s endpoint=%s status=%s permanent=%s error=%s",
            str(spec.get("channel_id") or ""),
            ctype or "unknown",
            safe_endpoint(ctype, config, secret, test_mode_=tm),
            result.http_status,
            result.permanent,
            result.error,
        )
    return result


__all__ = [
    "ALLOWED_HOSTS",
    "BRAND",
    "CHANNEL_TYPES",
    "ChannelConfigError",
    "DEFAULT_TIMEOUT",
    "MAX_ERROR_LEN",
    "SendResult",
    "TEST_ENV",
    "TEST_HTTP_BASE_ENV",
    "channel_type_catalog",
    "normalize_secret",
    "resolve_endpoint",
    "safe_endpoint",
    "sanitize_text",
    "send",
    "strip_paths",
    "test_http_base",
    "test_mode",
    "validate_config",
]

# -*- coding: utf-8 -*-
"""模型 API 客户端（开源版舆见 OpinionLens）。

用户可在「设置 → 模型 API」配置自己的模型服务，报告中心直连该服务生成叙述：

* ``openai_compatible``：OpenAI / DeepSeek / Ollama / vLLM / 各类中转站（``/chat/completions``）；
* ``deepseek``：OpenAI 协议，默认 ``https://api.deepseek.com`` + ``deepseek-chat``；
* ``anthropic``：``/v1/messages`` + ``x-api-key`` + ``anthropic-version: 2023-06-01``。

安全约定：
* API Key 只经 :mod:`secret_store` 加密后落库（Windows DPAPI），列表/详情只回脱敏值；
* base_url 里的用户名/密码与查询串同样拆出加密保存（``endpoint_secret_enc``），
  ``base_url`` 列只存 ``scheme://host[:port]/path``，调用模型时才拼回；
* 日志只记录 provider_id / kind / model / host / 状态码 / 耗时，**不记录密钥与请求体**；
* 未配置 provider 时报告中心自动降级为本地模板（degraded），不阻塞采集与导出。

两个对外类：
* :class:`LLMService` —— provider CRUD、测试连接、统一 ``chat()``；
* :class:`LocalLLMClient` —— 报告中心使用的 duck-typing 客户端（替代原内置 HTTP 客户端）：
  ``configured`` / ``cfg.masked_base_url`` / ``test_connection`` / ``analyze_sentiment`` /
  ``generate_narrative`` / ``close``；``generate_narrative`` 只产出 narrative JSON，
  HTML 由 :mod:`local_report` 用 ``report_pipeline`` 本地渲染。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import SplitResult, parse_qsl, quote, unquote, urlsplit

from secret_store import SecretError, mask_secret, protect, unprotect
from typing_utils import as_dict, as_list

log = logging.getLogger("sidecar.llm")

try:  # httpx 在 MediaCrawler venv 中已安装；缺失时给出可读错误
    import httpx
except Exception:  # pragma: no cover - 正常环境必有 httpx
    httpx = None


class _HttpxUrlLogFilter(logging.Filter):
    """httpx 自带的 ``HTTP Request: POST <url>`` 日志去掉查询串（拼回的地址秘密在那里）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if httpx is not None and isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                f"{mask_base_url(str(arg))}{arg.path}" if isinstance(arg, httpx.URL) else arg
                for arg in record.args
            )
        return True


if httpx is not None:
    logging.getLogger("httpx").addFilter(_HttpxUrlLogFilter())

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
PROVIDER_KINDS = ("openai_compatible", "deepseek", "anthropic")
KIND_LABELS = {
    "openai_compatible": "OpenAI 兼容",
    "deepseek": "DeepSeek",
    "anthropic": "Anthropic",
}
DEFAULT_BASE_URLS = {"deepseek": "https://api.deepseek.com"}
DEFAULT_MODELS = {"deepseek": "deepseek-chat"}
ANTHROPIC_VERSION = "2023-06-01"

SCHEMA_VERSION = "public-sentiment-data@v1"
NARRATIVE_SCHEMA_VERSION = "1.0"

DEFAULT_TIMEOUT = 120.0
CONNECT_TIMEOUT = 10.0
TEST_TIMEOUT = 20.0
SENTIMENT_TIMEOUT = 90.0
SENTIMENT_BATCH_SIZE = 50
SENTIMENT_TEXT_LIMIT = 500
NARRATIVE_MAX_TOKENS = 8000
NARRATIVE_TEMPERATURE = 0.4

# 错误码（report_manager / API 均按 code 分支，文案面向普通用户）
CODE_NOT_CONFIGURED = "LLM_NOT_CONFIGURED"
CODE_NOT_FOUND = "LLM_PROVIDER_NOT_FOUND"
CODE_INVALID_CONFIG = "LLM_INVALID_CONFIG"
CODE_TIMEOUT = "LLM_TIMEOUT"
CODE_NETWORK = "LLM_NETWORK"
CODE_HTTP_4XX = "LLM_HTTP_4XX"
CODE_HTTP_5XX = "LLM_HTTP_5XX"
CODE_PARSE = "LLM_PARSE"
CODE_BAD_RESPONSE = "LLM_BAD_RESPONSE"
CODE_BAD_NARRATIVE = "LLM_BAD_NARRATIVE"
CODE_UNSUPPORTED = "LLM_UNSUPPORTED"

_LOCAL_AI_GENERATED_BY = "local-llm"

#: 情感标签归一化（模型可能返回中文 / 大小写变体）
_SENTIMENT_ALIASES = {
    "positive": "positive",
    "pos": "positive",
    "正面": "positive",
    "积极": "positive",
    "neutral": "neutral",
    "neu": "neutral",
    "中性": "neutral",
    "negative": "negative",
    "neg": "negative",
    "负面": "negative",
    "消极": "negative",
}


# ---------------------------------------------------------------------------
# 异常 / 工具
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """模型调用失败（配置错误 / 网络 / 超时 / HTTP 错误 / 响应解析失败）。"""

    def __init__(
        self,
        message: str,
        code: str = "LLM_ERROR",
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = str(message)
        self.code = str(code)
        self.status_code = status_code
        self.retryable = bool(retryable)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "status_code": self.status_code,
            "retryable": self.retryable,
        }


def _host_port(parsed: SplitResult) -> str:
    """``host[:port]``（不含 userinfo）；端口非法时抛 ``ValueError``。"""
    host = str(parsed.hostname or "")
    if not host:
        return ""
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    return f"{host}:{port}" if port is not None else host


def mask_base_url(base_url: str) -> str:
    """脱敏展示：只保留 scheme://host[:port]，不含用户名/密码、路径与查询参数（可能含令牌）。"""
    text = str(base_url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
        host = _host_port(parsed)
    except ValueError:
        return ""
    if not parsed.scheme or not host:
        return ""
    return f"{parsed.scheme}://{host}"


def normalize_base_url(base_url: Any) -> str:
    """去首尾空白与末尾斜杠（不补 /v1，由具体协议端点拼接）。"""
    return str(base_url or "").strip().rstrip("/")


def _ensure_http_url(base_url: Any) -> str:
    value = normalize_base_url(base_url)
    if not value:
        return ""
    lowered = value.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        shown = mask_base_url(value)
        raise LLMError(
            "base_url 必须以 http:// 或 https:// 开头" + (f"：{shown}" if shown else ""),
            code=CODE_INVALID_CONFIG,
            status_code=400,
        )
    return value


#: 地址里需要与 API Key 同等保护的部分（userinfo 与整段 query 都可能是令牌）
_ENDPOINT_SECRET_KEYS = ("username", "password", "query")


def split_endpoint(base_url: Any) -> Tuple[str, Dict[str, str]]:
    """拆分接口地址 → (可落库/展示的 ``scheme://host[:port]/path``, 需加密保存的部分)。

    用户名、密码（已百分号解码）与整段查询串都视为秘密，只经 :func:`protect` 落库；
    片段（``#...``）不会发给服务端，直接丢弃。
    """
    value = _ensure_http_url(base_url)
    if not value:
        return "", {}
    try:
        parsed = urlsplit(value)
        host = _host_port(parsed)
    except ValueError as exc:
        raise LLMError(
            "base_url 格式不正确（主机名或端口无效）",
            code=CODE_INVALID_CONFIG,
            status_code=400,
        ) from exc
    if not host:
        raise LLMError(
            "base_url 缺少主机名（例如 https://api.openai.com）",
            code=CODE_INVALID_CONFIG,
            status_code=400,
        )
    public = f"{parsed.scheme}://{host}{parsed.path}".rstrip("/")
    parts: Dict[str, str] = {}
    if parsed.username:
        parts["username"] = unquote(parsed.username)
    if parsed.password:
        parts["password"] = unquote(parsed.password)
    if parsed.query:
        parts["query"] = parsed.query
    return public, parts


def _protect_endpoint_secret(public_base: str, parts: Dict[str, str]) -> str:
    """加密保存地址秘密，并绑定拆分出它的公开地址（换 host/路径后不再随请求发出）。"""
    clean = {key: str(parts[key]) for key in _ENDPOINT_SECRET_KEYS if parts.get(key)}
    if not clean:
        return ""
    return protect(json.dumps({"base": public_base, **clean}, ensure_ascii=False))


def _unprotect_endpoint_secret(secret_enc: Any) -> Dict[str, str]:
    if not secret_enc:
        return {}
    try:
        data = json.loads(unprotect(str(secret_enc)))
    except (SecretError, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: str(value) for key, value in data.items() if isinstance(value, str)}


def _endpoint_parts_for(public_base: str, stored: Dict[str, str]) -> Dict[str, str]:
    if not stored or stored.get("base") != public_base:
        return {}
    return {key: stored[key] for key in _ENDPOINT_SECRET_KEYS if stored.get(key)}


def _secret_values(parts: Dict[str, str], *extra: Any) -> List[str]:
    """需要从错误文本里抹掉的值（含百分号编码形式与查询参数逐项的值）。"""
    values: List[str] = []
    candidates: List[str] = [str(parts.get(key) or "") for key in _ENDPOINT_SECRET_KEYS]
    query = str(parts.get("query") or "")
    if query:
        for item in query.split("&"):
            candidates.append(item.partition("=")[2])
        candidates.extend(value for _name, value in parse_qsl(query, keep_blank_values=True))
    candidates.extend(str(item or "") for item in extra)
    for candidate in candidates:
        for form in (candidate, quote(candidate, safe="")):
            if form and form not in values:
                values.append(form)
    values.sort(key=len, reverse=True)
    return values


def _redact(text: Any, secrets: Sequence[str]) -> str:
    result = str(text or "")
    for secret in secrets:
        if secret and secret in result:
            result = result.replace(secret, "***")
    return result


def openai_chat_url(base_url: str) -> str:
    """OpenAI 兼容端点归一化：用户可填 ``https://api.deepseek.com``、``.../v1``、
    ``.../v1/chat/completions`` 或带自定义路径的中转站地址。"""
    base = normalize_base_url(base_url)
    lowered = base.lower()
    if lowered.endswith("/chat/completions"):
        return base
    if lowered.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def anthropic_chat_url(base_url: str) -> str:
    """Anthropic 端点归一化：``https://api.anthropic.com`` → ``/v1/messages``。"""
    base = normalize_base_url(base_url)
    lowered = base.lower()
    if lowered.endswith("/v1/messages"):
        return base
    if lowered.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def _now_ts() -> int:
    return int(time.time())


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any, limit: int = 0) -> str:
    text = str(value if value is not None else "").strip()
    return text[:limit] if limit and len(text) > limit else text


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off", ""):
        return False
    return default


def _loads(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


# ---------------------------------------------------------------------------
# JSON 提取（容忍 Markdown 围栏 / 前后解释文字）
# ---------------------------------------------------------------------------
def _balanced_slice(text: str, start: int) -> Optional[str]:
    """从 ``text[start]``（{ 或 [）开始找配平的 JSON 片段，正确跳过字符串内部括号。"""
    if start < 0 or start >= len(text) or text[start] not in "[{":
        return None
    pairs = {"{": "}", "[": "]"}
    stack: List[str] = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            stack.append(pairs[char])
        elif char in "}]":
            if not stack or stack[-1] != char:
                return None
            stack.pop()
            if not stack:
                return text[start : index + 1]
    return None


def extract_json_value(text: Any) -> Any:
    """从模型回复中提取第一个合法 JSON 值；失败抛 :class:`LLMError`。"""
    if not isinstance(text, str):
        raise LLMError("模型返回内容不是文本", code=CODE_PARSE)
    raw = text.strip().lstrip("\ufeff")
    if not raw:
        raise LLMError("模型返回内容为空", code=CODE_PARSE)
    candidates: List[str] = []
    for match in re.finditer(r"```(?:json|JSON)?\s*(.*?)```", raw, re.S):
        snippet = match.group(1).strip()
        if snippet:
            candidates.append(snippet)
    candidates.append(raw)
    for candidate in candidates:
        for index, char in enumerate(candidate):
            if char not in "[{":
                continue
            segment = _balanced_slice(candidate, index)
            if not segment:
                continue
            try:
                return json.loads(segment)
            except (TypeError, ValueError):
                continue
    raise LLMError("模型返回内容中未找到可解析的 JSON", code=CODE_PARSE)


def _dict_merge(base: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并（extra 优先，空值不覆盖 base）——用于修订时的 partial narrative。"""
    result: Dict[str, Any] = dict(base)
    for key, value in (extra or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _dict_merge(result[key], value)
        elif value not in (None, "", [], {}):
            result[key] = value
        elif key not in result:
            result[key] = value
    return result


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
NARRATIVE_SYSTEM_PROMPT = """你是「舆见 OpinionLens」舆情监测周报的叙述层引擎（narrative@v1）。

【唯一任务】
根据用户消息中的 JSON（report_meta / counts / stats / samples，修订时另有 previous_narrative 与 instruction），
写出报告各章节的中文叙述，输出**一个** JSON 对象。所有统计数字由客户端代码计算，你只负责文字表达。

【输出硬约束（违反即视为失败）】
1. 只输出一个 JSON 对象本身：第一个字符必须是 {，最后一个字符必须是 }。
   禁止 Markdown 围栏（```json / ```）、禁止任何解释、前言、后记或思考过程。
2. 结构必须为：
{
  "schema_version": "1.0",
  "overview": {"headline": "一句话标题", "one_line_summary": "总览段落", "scope_note": "数据口径说明"},
  "trend": {"insight": "声量趋势解读"},
  "sentiment": {"insight": "情感分布解读"},
  "platform": {"insight": "平台声量解读"},
  "negative": {"intro": "负面事件表前说明", "note": "状态说明", "measures": [{"event_id": "NEG-01", "measure": "处理建议", "status": "与 stats 中 handling_status 一致"}]},
  "positive": {"intro": "正面事件表前说明", "follow_ups": [{"event_id": "POS-01", "follow_up": "跟进建议", "worth_amplifying": true}]},
  "suggestions": ["建议 1", "建议 2", "建议 3"],
  "suggestions_note": "建议说明",
  "risk": {"level": "低", "assessment": "风险评估"},
  "disclaimer": "说明数字由程序统计、模型只负责文字"
}
3. 所有数字必须能在 stats 中找到同值字段；禁止编造 stats 中不存在的事件、链接、互动量、处理进度、环比数据。
4. negative.measures 与 stats.negative_events 按 event_id 一一对齐（顺序一致，不增不减）；status 只能引用事件中的
   handling_status；measure 只能写「建议」，不得虚构「已处理 / 已删除 / 已发声明」等事实。
5. positive.follow_ups 与 stats.positive_events 按 event_id 一一对齐；没有事件时给空数组。
6. 热词、平台、情感判断只能来自 stats；suggestions 给 3-8 条可执行建议，中文书面语、专业克制、信息密度高。
7. risk.level 必须等于 stats.risk.level 原值（stats 缺失时用「低」）；不要自行升级/降级风险。
8. 修订模式（消息中含 previous_narrative 与 instruction）：以 previous_narrative 为唯一基准，
   只修改 instruction 要求的局部文字；结构、事件 id、数字一律保持不变，输出仍是完整 JSON 对象。
9. JSON 字符串内部避免裸换行；不要输出注释、尾逗号；使用 UTF-8 中文。"""

SENTIMENT_SYSTEM_PROMPT = """你是舆情情感分析器。对用户提供的每条文本判断情感倾向（positive / neutral / negative）。
只输出一个 JSON 对象（不要 Markdown 围栏、不要解释）：
{"items":[{"id":"原样返回输入 id","sentiment":"positive|neutral|negative","confidence":0到1之间的小数,"reason":"不超过40字的中文理由"}]}
必须覆盖全部输入 id；无法判断时 sentiment 用 neutral；不要编造输入之外的 id。"""


def _narrative_user_payload(
    package: Dict[str, Any],
    instruction: str = "",
    previous_narrative: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    report = as_dict(package.get("report"))
    payload: Dict[str, Any] = {
        "report_meta": {
            "brand": report.get("brand", ""),
            "title": report.get("title", ""),
            "window": report.get("window"),
            "keywords": list(report.get("keywords") or []),
            "platforms": list(report.get("platforms") or []),
            "schema_version": package.get("schema_version", SCHEMA_VERSION),
        },
        "counts": package.get("counts") or {},
        "stats": package.get("stats") or {},
        "samples": package.get("samples") or {},
    }
    if instruction:
        payload["instruction"] = str(instruction)[:4000]
    if isinstance(previous_narrative, dict) and previous_narrative:
        payload["previous_narrative"] = previous_narrative
    return payload


# 必填核心字段（缺任意一项 → 视为模型输出不合格，由报告中心降级本地模板）
_NARRATIVE_REQUIRED_PATHS: Tuple[Tuple[str, ...], ...] = (
    ("overview", "one_line_summary"),
    ("trend", "insight"),
    ("sentiment", "insight"),
    ("platform", "insight"),
    ("negative", "intro"),
    ("negative", "note"),
    ("positive", "intro"),
)


def _get_path(document: Dict[str, Any], path: Tuple[str, ...]) -> Any:
    node: Any = document
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _normalize_narrative(
    value: Any,
    provider: Dict[str, Any],
    previous: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """校验模型输出的 narrative（宽松子集），补齐非核心字段并写入生成来源。"""
    if not isinstance(value, dict) or not value:
        raise LLMError("模型返回的 narrative 不是 JSON 对象", code=CODE_BAD_NARRATIVE)
    doc = _dict_merge(previous, value) if isinstance(previous, dict) and previous else dict(value)
    for key in ("overview", "trend", "sentiment", "platform", "negative", "positive", "risk"):
        if not isinstance(doc.get(key), dict):
            doc[key] = {}
    missing: List[str] = []
    for path in _NARRATIVE_REQUIRED_PATHS:
        node = _get_path(doc, path)
        if not isinstance(node, str) or not node.strip():
            missing.append("/".join(path))
    suggestions = doc.get("suggestions")
    if not isinstance(suggestions, list) or not [
        item for item in suggestions if isinstance(item, str) and item.strip()
    ]:
        missing.append("suggestions")
    if missing:
        raise LLMError(
            f"模型返回的 narrative 缺少必填内容：{'、'.join(missing)}",
            code=CODE_BAD_NARRATIVE,
        )
    overview = doc["overview"]
    negative = doc["negative"]
    positive = doc["positive"]
    risk = doc["risk"]
    stats = {}
    if isinstance(value.get("_stats"), dict):  # pragma: no cover - 保留扩展位
        stats = value["_stats"]
    if not isinstance(negative.get("measures"), list):
        negative["measures"] = []
    if not isinstance(positive.get("follow_ups"), list):
        positive["follow_ups"] = []
    if not _text(overview.get("headline")):
        overview["headline"] = "本期舆情总体平稳，风险可控"
    if not isinstance(overview.get("scope_note"), str):
        overview["scope_note"] = ""
    if not _text(risk.get("level")):
        risk["level"] = "低"
    if risk.get("level") not in ("低", "中", "高"):
        risk["level"] = "低"
    if not isinstance(risk.get("assessment"), str):
        risk["assessment"] = ""
    doc["suggestions"] = [
        item.strip() for item in as_list(suggestions) if isinstance(item, str) and item.strip()
    ][:8]
    if not isinstance(doc.get("suggestions_note"), str):
        doc["suggestions_note"] = "以上建议由模型基于统计结果生成，请结合业务实际调整执行优先级。"
    if not isinstance(doc.get("disclaimer"), str) or not doc["disclaimer"].strip():
        doc["disclaimer"] = "本报告统计数字由程序计算，模型仅负责文字叙述。"
    doc["schema_version"] = NARRATIVE_SCHEMA_VERSION
    doc["generated_by"] = _LOCAL_AI_GENERATED_BY
    doc["model"] = str(provider.get("model") or "")
    doc["generated_at"] = _now_text()
    doc.pop("_stats", None)
    return doc


# ---------------------------------------------------------------------------
# LLMService：provider CRUD + 统一 chat
# ---------------------------------------------------------------------------
_ROW_COLUMNS = (
    "provider_id, name, kind, base_url, model, secret_enc, endpoint_secret_enc, extra_json, "
    "enabled, is_default, last_test_ok, last_test_at, last_test_msg, created_at, updated_at"
)


def _row_to_provider(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "provider_id": str(row.get("provider_id") or ""),
        "name": str(row.get("name") or ""),
        "kind": str(row.get("kind") or ""),
        "base_url": str(row.get("base_url") or ""),
        "model": str(row.get("model") or ""),
        "secret_enc": str(row.get("secret_enc") or ""),
        "endpoint_secret_enc": str(row.get("endpoint_secret_enc") or ""),
        "extra": _loads(row.get("extra_json"), {}),
        "enabled": bool(row.get("enabled")),
        "is_default": bool(row.get("is_default")),
        "last_test_ok": None if row.get("last_test_ok") is None else bool(row.get("last_test_ok")),
        "last_test_at": row.get("last_test_at"),
        "last_test_msg": str(row.get("last_test_msg") or ""),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _decrypt_secret(secret_enc: str) -> str:
    if not secret_enc:
        return ""
    try:
        return unprotect(secret_enc)
    except SecretError:
        return ""


def _public_base_url(base_url: Any) -> str:
    try:
        return split_endpoint(base_url)[0]
    except LLMError:
        return mask_base_url(str(base_url or ""))


def _with_secrets(provider: Dict[str, Any]) -> Dict[str, Any]:
    """内部视图：附上解密后的 ``api_key`` 与 ``endpoint_secret``（只用于发请求）。"""
    provider = dict(provider)
    provider["api_key"] = _decrypt_secret(provider.get("secret_enc") or "")
    provider["endpoint_secret"] = _unprotect_endpoint_secret(
        provider.get("endpoint_secret_enc") or ""
    )
    return provider


def _request_endpoint(provider: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """发请求用的 (公开地址, 地址秘密)：加密保存的秘密只配给拆出它的那个地址。"""
    public, inline = split_endpoint(provider.get("base_url"))
    stored = provider.get("endpoint_secret")
    if not isinstance(stored, dict):
        stored = _unprotect_endpoint_secret(provider.get("endpoint_secret_enc") or "")
    parts = _endpoint_parts_for(public, stored)
    parts.update(inline)
    return public, parts


def _provider_public(provider: Dict[str, Any]) -> Dict[str, Any]:
    """渲染进程可见的 provider（绝不包含明文密钥，地址只含 scheme/host/port/path）。"""
    secret = _decrypt_secret(str(provider.get("secret_enc") or ""))
    public = {
        "provider_id": provider["provider_id"],
        "name": provider["name"],
        "kind": provider["kind"],
        "base_url": _public_base_url(provider["base_url"]),
        "model": provider["model"],
        "extra": dict(provider.get("extra") or {}),
        "enabled": bool(provider.get("enabled")),
        "is_default": bool(provider.get("is_default")),
        "api_key_masked": mask_secret(secret) if secret else "",
        "secret_configured": bool(provider.get("secret_enc")),
        "endpoint_secret_configured": bool(provider.get("endpoint_secret_enc")),
        "last_test_ok": provider.get("last_test_ok"),
        "last_test_at": provider.get("last_test_at"),
        "last_test_msg": provider.get("last_test_msg") or "",
        "created_at": provider.get("created_at"),
        "updated_at": provider.get("updated_at"),
    }
    return public


def _default_name(kind: str, model: str) -> str:
    label = KIND_LABELS.get(kind, kind or "模型")
    return f"{label} · {model}" if model else label


class LLMService:
    """模型 API 配置服务（provider CRUD / 测试 / 统一对话调用）。"""

    def __init__(self, cfg: Any, state: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        if state is not None:
            try:
                self._migrate_endpoint_secrets()
            except Exception:  # noqa: BLE001 - 迁移失败不阻塞启动
                log.warning("迁移模型 API 地址中的密钥失败（下次启动重试）")

    def _migrate_endpoint_secrets(self) -> None:
        """旧版本把完整 base_url（含 userinfo/查询串）明文落库：拆出秘密改为加密保存。"""
        rows = self._query(
            "SELECT provider_id, base_url, endpoint_secret_enc, last_test_msg FROM llm_providers"
        )
        migrated = 0
        for row in rows:
            raw = str(row.get("base_url") or "")
            try:
                public, inline = split_endpoint(raw)
            except LLMError:
                public, inline = mask_base_url(raw), {}
            if public == raw and not inline:
                continue
            if migrated == 0:
                self._execute("PRAGMA secure_delete = ON")
            parts = _endpoint_parts_for(
                public, _unprotect_endpoint_secret(row.get("endpoint_secret_enc"))
            )
            parts.update(inline)
            self._execute(
                "UPDATE llm_providers SET base_url = ?, endpoint_secret_enc = ?, "
                "last_test_msg = ? WHERE provider_id = ?",
                (
                    public,
                    _protect_endpoint_secret(public, parts),
                    _redact(row.get("last_test_msg"), _secret_values(inline)),
                    str(row.get("provider_id") or ""),
                ),
            )
            migrated += 1
        if migrated:
            self._execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log.info("已将 %s 个模型 API 地址中的密钥改为加密保存", migrated)

    # ---- 内部：SQL -----------------------------------------------------
    def _state(self) -> Any:
        if self.state is None:
            raise LLMError(
                "模型 API 存储未就绪（sidecar state 缺失）",
                code=CODE_INVALID_CONFIG,
                status_code=500,
            )
        return self.state

    def _query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        try:
            return list(self._state().query(sql, params))
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001 - 存储异常统一转 LLMError
            raise LLMError(
                f"模型 API 配置读取失败：{type(exc).__name__}: {exc}",
                code=CODE_INVALID_CONFIG,
                status_code=500,
            ) from exc

    def _execute(self, sql: str, params: tuple = ()) -> int:
        try:
            return int(self._state().execute(sql, params))
        except LLMError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMError(
                f"模型 API 配置写入失败：{type(exc).__name__}: {exc}",
                code=CODE_INVALID_CONFIG,
                status_code=500,
            ) from exc

    def _fetch(self, provider_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query(
            f"SELECT {_ROW_COLUMNS} FROM llm_providers WHERE provider_id = ?",
            (str(provider_id),),
        )
        return _row_to_provider(rows[0]) if rows else None

    def _require_provider(self, provider_id: str) -> Dict[str, Any]:
        provider = self._fetch(provider_id)
        if provider is None:
            raise LLMError(
                f"模型 API 配置不存在：{provider_id}",
                code=CODE_NOT_FOUND,
                status_code=404,
            )
        return provider

    # ---- provider 校验 -------------------------------------------------
    @staticmethod
    def _normalize_kind(kind: Any) -> str:
        value = str(kind or "").strip().lower()
        if value not in PROVIDER_KINDS:
            raise LLMError(
                f"不支持的模型服务类型：{value or '(空)'}；可选 "
                + " / ".join(PROVIDER_KINDS),
                code=CODE_INVALID_CONFIG,
                status_code=400,
            )
        return value

    @staticmethod
    def _resolve_endpoint(
        kind: str, base_url: Any, model: Any
    ) -> Tuple[str, Dict[str, str], str]:
        """返回 (公开 base_url, 需加密保存的地址秘密, model)。"""
        resolved_base, endpoint_parts = split_endpoint(base_url)
        if kind == "deepseek":
            resolved_base = resolved_base or DEFAULT_BASE_URLS["deepseek"]
        if not resolved_base:
            raise LLMError(
                "base_url 不能为空（例如 https://api.openai.com 或 http://127.0.0.1:11434/v1）",
                code=CODE_INVALID_CONFIG,
                status_code=400,
            )
        resolved_model = str(model or "").strip() or DEFAULT_MODELS.get(kind, "")
        if not resolved_model:
            raise LLMError(
                "model 不能为空（例如 gpt-4o-mini / deepseek-chat / claude-3-5-sonnet-latest）",
                code=CODE_INVALID_CONFIG,
                status_code=400,
            )
        return resolved_base, endpoint_parts, resolved_model

    # ---- provider CRUD -------------------------------------------------
    def list_providers(self, masked: bool = True) -> List[Dict[str, Any]]:
        rows = self._query(
            f"SELECT {_ROW_COLUMNS} FROM llm_providers ORDER BY is_default DESC, created_at ASC"
        )
        providers = [_row_to_provider(row) for row in rows]
        if not masked:
            return providers
        return [_provider_public(provider) for provider in providers]

    def get_provider(self, provider_id: str, masked: bool = True) -> Dict[str, Any]:
        """按 id 读取 provider；masked=False 时含解密后的 ``api_key`` / ``endpoint_secret``（内部使用）。"""
        provider = self._require_provider(provider_id)
        if masked:
            return _provider_public(provider)
        return _with_secrets(provider)

    def create_provider(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise LLMError("请求体必须是 JSON 对象", code=CODE_INVALID_CONFIG, status_code=400)
        kind = self._normalize_kind(data.get("kind"))
        base_url, endpoint_parts, model = self._resolve_endpoint(
            kind, data.get("base_url"), data.get("model")
        )
        name = _text(data.get("name"), 80) or _default_name(kind, model)
        api_key = _text(data.get("api_key"), 4000)
        secret_enc = protect(api_key) if api_key else ""
        endpoint_secret_enc = _protect_endpoint_secret(base_url, endpoint_parts)
        extra = data.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        now = _now_ts()
        provider_id = "prov_" + uuid.uuid4().hex[:12]
        has_default = bool(
            self._query("SELECT provider_id FROM llm_providers WHERE is_default = 1 LIMIT 1")
        )
        is_default = _as_bool(data.get("is_default"), False) or not has_default
        enabled = _as_bool(data.get("enabled"), True)
        try:
            self._execute(
                "INSERT INTO llm_providers(provider_id, name, kind, base_url, model, secret_enc, "
                "endpoint_secret_enc, extra_json, enabled, is_default, last_test_ok, "
                "last_test_at, last_test_msg, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, '', ?, ?)",
                (
                    provider_id,
                    name,
                    kind,
                    base_url,
                    model,
                    secret_enc,
                    endpoint_secret_enc,
                    json.dumps(extra, ensure_ascii=False),
                    1 if enabled else 0,
                    1 if is_default else 0,
                    now,
                    now,
                ),
            )
        except LLMError:
            raise
        if is_default:
            self._execute(
                "UPDATE llm_providers SET is_default = 0, updated_at = ? "
                "WHERE is_default = 1 AND provider_id <> ?",
                (now, provider_id),
            )
        log.info(
            "已新增模型 API 配置：provider_id=%s kind=%s model=%s base_url=%s",
            provider_id,
            kind,
            model,
            mask_base_url(base_url),
        )
        return _provider_public(self._require_provider(provider_id))

    def update_provider(self, provider_id: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise LLMError("请求体必须是 JSON 对象", code=CODE_INVALID_CONFIG, status_code=400)
        provider = self._require_provider(provider_id)
        sets: List[str] = []
        params: List[Any] = []
        kind = provider["kind"]
        if "kind" in data:
            kind = self._normalize_kind(data.get("kind"))
            sets.append("kind = ?")
            params.append(kind)
        base_url = data.get("base_url") if "base_url" in data else provider["base_url"]
        model = (
            str(data.get("model") or "").strip() if "model" in data else provider["model"]
        )
        resolved_base, endpoint_parts, resolved_model = self._resolve_endpoint(
            kind, base_url, model
        )
        endpoint_secret_enc: Optional[str] = None  # None = 保留原值
        if "base_url" in data or kind != provider["kind"]:
            sets.append("base_url = ?")
            params.append(resolved_base)
            if endpoint_parts:
                endpoint_secret_enc = _protect_endpoint_secret(resolved_base, endpoint_parts)
            elif not _endpoint_parts_for(
                resolved_base, _unprotect_endpoint_secret(provider.get("endpoint_secret_enc"))
            ):
                # 回填同一公开地址（设置页编辑时）保留原秘密；换了地址则不再带旧秘密
                endpoint_secret_enc = ""
        if _as_bool(data.get("clear_endpoint_secret"), False):
            endpoint_secret_enc = ""
        if endpoint_secret_enc is not None and (
            endpoint_secret_enc or provider.get("endpoint_secret_enc")
        ):
            sets.append("endpoint_secret_enc = ?")
            params.append(endpoint_secret_enc)
        if "model" in data or kind != provider["kind"]:
            sets.append("model = ?")
            params.append(resolved_model)
        if "name" in data:
            sets.append("name = ?")
            params.append(_text(data.get("name"), 80) or _default_name(kind, resolved_model))
        if "enabled" in data:
            sets.append("enabled = ?")
            params.append(1 if _as_bool(data.get("enabled"), True) else 0)
        if "extra" in data and isinstance(data.get("extra"), dict):
            sets.append("extra_json = ?")
            params.append(json.dumps(data["extra"], ensure_ascii=False))
        if "api_key" in data:
            api_key = _text(data.get("api_key"), 4000)
            if api_key:  # 空值 = 保留原密钥（显式清空用 clear_secret）
                sets.append("secret_enc = ?")
                params.append(protect(api_key))
        if _as_bool(data.get("clear_secret"), False):
            sets.append("secret_enc = ?")
            params.append("")
        is_default = data.get("is_default")
        now = _now_ts()
        if is_default is not None:
            if _as_bool(is_default, False):
                self._execute(
                    "UPDATE llm_providers SET is_default = 0, updated_at = ? "
                    "WHERE is_default = 1 AND provider_id <> ?",
                    (now, provider_id),
                )
                sets.append("is_default = ?")
                params.append(1)
            else:
                sets.append("is_default = ?")
                params.append(0)
        sets.append("updated_at = ?")
        params.append(now)
        params.append(provider_id)
        self._execute(
            f"UPDATE llm_providers SET {', '.join(sets)} WHERE provider_id = ?",
            tuple(params),
        )
        log.info("已更新模型 API 配置：provider_id=%s", provider_id)
        return _provider_public(self._require_provider(provider_id))

    def delete_provider(self, provider_id: str) -> Dict[str, Any]:
        provider = self._require_provider(provider_id)
        self._execute("DELETE FROM llm_providers WHERE provider_id = ?", (provider_id,))
        new_default = ""
        if provider["is_default"]:
            fallback = self._query(
                "SELECT provider_id FROM llm_providers WHERE enabled = 1 "
                "ORDER BY created_at ASC LIMIT 1"
            )
            if fallback:
                new_default = str(fallback[0]["provider_id"])
                self._execute(
                    "UPDATE llm_providers SET is_default = 1, updated_at = ? WHERE provider_id = ?",
                    (_now_ts(), new_default),
                )
        log.info("已删除模型 API 配置：provider_id=%s", provider_id)
        return {
            "ok": True,
            "deleted": provider_id,
            "default_provider_id": new_default,
        }

    def set_default(self, provider_id: str) -> Dict[str, Any]:
        provider = self._require_provider(provider_id)
        now = _now_ts()
        self._execute("UPDATE llm_providers SET is_default = 0 WHERE is_default = 1")
        self._execute(
            "UPDATE llm_providers SET is_default = 1, updated_at = ? WHERE provider_id = ?",
            (now, provider_id),
        )
        log.info("已设置默认模型 API：provider_id=%s", provider["provider_id"])
        return _provider_public(self._require_provider(provider_id))

    def get_default(self) -> Optional[Dict[str, Any]]:
        """默认 provider（含解密 ``api_key`` / ``endpoint_secret``）；无默认时自愈取最早启用的一个。"""
        rows = self._query(
            f"SELECT {_ROW_COLUMNS} FROM llm_providers WHERE is_default = 1 AND enabled = 1 "
            "ORDER BY updated_at DESC LIMIT 1"
        )
        if not rows:
            rows = self._query(
                f"SELECT {_ROW_COLUMNS} FROM llm_providers WHERE enabled = 1 "
                "ORDER BY created_at ASC LIMIT 1"
            )
        if not rows:
            return None
        provider = _row_to_provider(rows[0])
        if not provider["is_default"]:
            try:
                self._execute(
                    "UPDATE llm_providers SET is_default = 1, updated_at = ? WHERE provider_id = ?",
                    (_now_ts(), provider["provider_id"]),
                )
                provider["is_default"] = True
            except LLMError:
                pass
        return _with_secrets(provider)

    def config_summary(self) -> Dict[str, Any]:
        provider = self.get_default()
        if not provider:
            return {
                "configured": False,
                "provider_id": "",
                "provider_name": "",
                "kind": "",
                "model": "",
                "base_url": "",
                "degraded_fallback": True,
            }
        return {
            "configured": True,
            "provider_id": provider["provider_id"],
            "provider_name": provider["name"],
            "kind": provider["kind"],
            "model": provider["model"],
            "base_url": mask_base_url(provider["base_url"]),
            "degraded_fallback": True,
        }

    # ---- 测试连接 ------------------------------------------------------
    def test_provider(self, provider_id: str) -> Dict[str, Any]:
        """发一条 ≤16 token 的 ping；不抛异常（除 provider 不存在），返回结构化结果。"""
        provider = self.get_provider(provider_id, masked=False)
        try:
            secrets = _secret_values(_request_endpoint(provider)[1], provider.get("api_key"))
        except LLMError:
            secrets = _secret_values({}, provider.get("api_key"))
        started = time.monotonic()
        try:
            reply = self.chat(
                provider,
                [
                    {"role": "system", "content": "你是连接测试助手。用户说 ping 时只回复 pong。"},
                    {"role": "user", "content": "ping"},
                ],
                max_tokens=16,
                temperature=0.0,
                timeout=TEST_TIMEOUT,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            result = {
                "ok": True,
                "provider_id": provider["provider_id"],
                "provider_name": provider["name"],
                "kind": provider["kind"],
                "model": provider["model"],
                "latency_ms": latency_ms,
                "reply": _text(reply, 200),
                "error": None,
            }
            self._record_test_result(provider["provider_id"], True, "")
        except LLMError as exc:
            error = exc.to_dict()
            error["message"] = _redact(exc.message, secrets)
            result = {
                "ok": False,
                "provider_id": provider["provider_id"],
                "provider_name": provider["name"],
                "kind": provider["kind"],
                "model": provider["model"],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "reply": "",
                "error": error,
            }
            self._record_test_result(provider["provider_id"], False, error["message"][:500])
        except Exception as exc:  # noqa: BLE001 - 测试接口永不 500
            message = _redact(f"{type(exc).__name__}: {exc}", secrets)
            result = {
                "ok": False,
                "provider_id": provider["provider_id"],
                "provider_name": provider["name"],
                "kind": provider["kind"],
                "model": provider["model"],
                "latency_ms": int((time.monotonic() - started) * 1000),
                "reply": "",
                "error": {"code": CODE_NETWORK, "message": message},
            }
            self._record_test_result(provider["provider_id"], False, message[:500])
        return result

    def _record_test_result(self, provider_id: str, ok: bool, message: str) -> None:
        try:
            self._execute(
                "UPDATE llm_providers SET last_test_ok = ?, last_test_at = ?, "
                "last_test_msg = ?, updated_at = ? WHERE provider_id = ?",
                (1 if ok else 0, _now_ts(), str(message or "")[:500], _now_ts(), provider_id),
            )
        except Exception:  # noqa: BLE001 - 记录测试结果失败不影响测试本身
            log.warning("记录模型 API 测试结果失败：provider_id=%s", provider_id)

    # ---- 统一对话 ------------------------------------------------------
    def chat(
        self,
        provider: Dict[str, Any],
        messages: List[Dict[str, str]],
        max_tokens: int = 8000,
        temperature: float = 0.4,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        """调用模型返回纯文本；失败抛 :class:`LLMError`。"""
        if isinstance(provider, str):
            provider = self.get_provider(provider, masked=False)
        if not isinstance(provider, dict):
            raise LLMError("provider 必须是 dict 或 provider_id", code=CODE_INVALID_CONFIG)
        if not isinstance(messages, list) or not messages:
            raise LLMError("messages 不能为空", code=CODE_INVALID_CONFIG)
        kind = str(provider.get("kind") or "").strip().lower() or "openai_compatible"
        base_url = str(provider.get("base_url") or "").strip()
        model = str(provider.get("model") or "").strip()
        if not base_url:
            raise LLMError(
                f"模型 API 未配置 base_url（provider={provider.get('provider_id') or ''}）",
                code=CODE_NOT_CONFIGURED,
            )
        if not model:
            raise LLMError(
                f"模型 API 未配置 model（provider={provider.get('provider_id') or ''}）",
                code=CODE_NOT_CONFIGURED,
            )
        try:
            timeout_value = max(1.0, float(timeout or DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout_value = DEFAULT_TIMEOUT
        if kind == "anthropic":
            return self._chat_anthropic(
                provider, messages, model, max_tokens, temperature, timeout_value
            )
        return self._chat_openai(
            provider, messages, model, max_tokens, temperature, timeout_value
        )

    # -- HTTP ------------------------------------------------------------
    @staticmethod
    def _request_headers(provider: Dict[str, Any], kind: str) -> Dict[str, str]:
        api_key = str(provider.get("api_key") or "")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if kind == "anthropic":
            if api_key:
                headers["x-api-key"] = api_key
            headers["anthropic-version"] = ANTHROPIC_VERSION
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _post_json(
        self,
        url: str,
        headers: Dict[str, str],
        payload: Dict[str, Any],
        timeout: float,
        *,
        provider: Dict[str, Any],
        kind: str,
        endpoint_parts: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """``url`` 为不含秘密的公开地址；``endpoint_parts`` 只在这里拼回实际请求。"""
        if httpx is None:  # pragma: no cover - venv 一定有
            raise LLMError("httpx 未安装，无法调用模型 API", code=CODE_NETWORK)
        parts = dict(endpoint_parts or {})
        secrets = _secret_values(parts, provider.get("api_key"))
        provider_id = str(provider.get("provider_id") or "")
        host = mask_base_url(url)
        request_url = url + (f"?{parts['query']}" if parts.get("query") else "")
        request_kwargs: Dict[str, Any] = {"headers": headers, "json": payload}
        if parts.get("username") or parts.get("password"):
            request_kwargs["auth"] = httpx.BasicAuth(
                parts.get("username", ""), parts.get("password", "")
            )
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout))
            ) as client:
                response = client.post(request_url, **request_kwargs)
        except httpx.TimeoutException as exc:
            log.warning(
                "模型 API 超时：provider_id=%s kind=%s host=%s timeout=%ss",
                provider_id,
                kind,
                host,
                timeout,
            )
            raise LLMError(
                f"模型 API 请求超时（>{timeout:.0f}s），请检查网络或调大超时",
                code=CODE_TIMEOUT,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            log.warning(
                "模型 API 网络错误：provider_id=%s kind=%s host=%s error=%s",
                provider_id,
                kind,
                host,
                type(exc).__name__,
            )
            raise LLMError(
                f"模型 API 不可达（{host}）：{type(exc).__name__}",
                code=CODE_NETWORK,
                retryable=True,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 统一转 LLMError
            raise LLMError(
                f"模型 API 调用异常：{type(exc).__name__}: {_redact(exc, secrets)[:200]}",
                code=CODE_NETWORK,
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        status = int(getattr(response, "status_code", 0) or 0)
        body: Any = None
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应（网关错误页等）
            body = None
        if 200 <= status < 300:
            if not isinstance(body, dict):
                log.warning(
                    "模型 API 响应解析失败：provider_id=%s status=%s latency_ms=%s",
                    provider_id,
                    status,
                    latency_ms,
                )
                raise LLMError(
                    f"模型 API 响应不是合法 JSON（HTTP {status}），请检查 base_url 是否正确",
                    code=CODE_PARSE,
                    status_code=status,
                )
            log.info(
                "模型 API 调用完成：provider_id=%s kind=%s model=%s status=%s latency_ms=%s",
                provider_id,
                kind,
                str(payload.get("model") or ""),
                status,
                latency_ms,
            )
            return body
        message = ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("type") or "")
            elif isinstance(error, str):
                message = error
            if not message:
                message = str(body.get("message") or body.get("detail") or "")
        if not message:
            message = str(getattr(response, "text", "") or "")[:300]
        if 400 <= status < 500:
            code = CODE_HTTP_4XX
            retryable = status == 429
        else:
            code = CODE_HTTP_5XX
            retryable = True
        log.warning(
            "模型 API HTTP 错误：provider_id=%s kind=%s status=%s latency_ms=%s",
            provider_id,
            kind,
            status,
            latency_ms,
        )
        raise LLMError(
            f"模型 API 请求失败（HTTP {status}）：{_redact(message, secrets)[:300]}",
            code=code,
            status_code=status,
            retryable=retryable,
        )

    def _chat_openai(
        self,
        provider: Dict[str, Any],
        messages: List[Dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> str:
        public_base, endpoint_parts = _request_endpoint(provider)
        url = openai_chat_url(public_base)
        payload = {
            "model": model,
            "messages": [
                {
                    "role": str(item.get("role") or "user"),
                    "content": str(item.get("content") or ""),
                }
                for item in messages
                if isinstance(item, dict)
            ],
            "max_tokens": max(1, int(max_tokens)),
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "stream": False,
        }
        body = self._post_json(
            url,
            self._request_headers(provider, "openai_compatible"),
            payload,
            timeout,
            provider=provider,
            kind="openai_compatible",
            endpoint_parts=endpoint_parts,
        )
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LLMError(
                "模型响应缺少 choices（请确认 base_url 指向 OpenAI 兼容 /chat/completions 接口）",
                code=CODE_BAD_RESPONSE,
            )
        first = as_dict(choices[0] if choices else None)
        message = as_dict(first.get("message"))
        content = message.get("content")
        if isinstance(content, list):  # 部分网关返回 content parts
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            )
        text = str(content or "").strip()
        if not text:
            raise LLMError("模型返回内容为空", code=CODE_BAD_RESPONSE)
        return text

    def _chat_anthropic(
        self,
        provider: Dict[str, Any],
        messages: List[Dict[str, str]],
        model: str,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> str:
        system_parts: List[str] = []
        chat_messages: List[Dict[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "user").lower()
            content = str(item.get("content") or "")
            if role == "system":
                system_parts.append(content)
            elif role == "assistant":
                chat_messages.append({"role": "assistant", "content": content})
            else:
                chat_messages.append({"role": "user", "content": content})
        if not chat_messages:
            chat_messages = [{"role": "user", "content": "ping"}]
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": max(1, int(max_tokens)),
            "temperature": max(0.0, min(float(temperature), 1.0)),
            "messages": chat_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        public_base, endpoint_parts = _request_endpoint(provider)
        body = self._post_json(
            anthropic_chat_url(public_base),
            self._request_headers(provider, "anthropic"),
            payload,
            timeout,
            provider=provider,
            kind="anthropic",
            endpoint_parts=endpoint_parts,
        )
        blocks = body.get("content")
        if not isinstance(blocks, list):
            raise LLMError(
                "模型响应缺少 content（请确认 base_url 指向 Anthropic /v1/messages 接口）",
                code=CODE_BAD_RESPONSE,
            )
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") in (None, "text")
        ).strip()
        if not text:
            raise LLMError("模型返回内容为空", code=CODE_BAD_RESPONSE)
        return text

    def close(self) -> None:
        """兼容旧客户端生命周期（本实现按请求创建短连接，无长连接可关）。"""
        return None


# ---------------------------------------------------------------------------
# LocalLLMClient：报告中心使用的 duck-typing 客户端
# ---------------------------------------------------------------------------
class _ClientConfigView:
    """兼容旧远端报告客户端 ``cfg`` 的只读视图（configured / masked_base_url）。"""

    def __init__(self, service: LLMService) -> None:
        self._service = service

    def _summary(self) -> Dict[str, Any]:
        try:
            return self._service.config_summary()
        except Exception:  # noqa: BLE001 - 配置读取失败按未配置处理
            return {"configured": False, "base_url": "", "model": "", "provider_name": ""}

    @property
    def configured(self) -> bool:
        return bool(self._summary().get("configured"))

    @property
    def masked_base_url(self) -> str:
        return str(self._summary().get("base_url") or "")

    @property
    def model(self) -> str:
        return str(self._summary().get("model") or "")

    @property
    def provider_name(self) -> str:
        return str(self._summary().get("provider_name") or "")


class LocalLLMClient:
    """报告中心的模型客户端：narrative JSON 生成 + 情感批量打标。

    与旧远端报告客户端保持相同 duck 接口（``configured`` / ``cfg.masked_base_url`` /
    ``test_connection`` / ``analyze_sentiment`` / ``generate_narrative`` / ``close`` /
    ``SCHEMA_VERSION``），但不再请求任何固定 8000 服务：
    ``chat()`` 直连用户在设置页配置的 provider，narrative 由 :mod:`local_report` 本地渲染 HTML。
    """

    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, cfg: Any = None, llm: Optional[LLMService] = None, state: Any = None) -> None:
        self.app_cfg = cfg
        if llm is None:
            llm = LLMService(cfg, state)
        self.service = llm
        self.cfg = _ClientConfigView(llm)

    # -- 生命周期 ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.cfg.configured)

    @property
    def narrative_available(self) -> bool:
        return True

    def close(self) -> None:
        try:
            self.service.close()
        except Exception:  # noqa: BLE001
            pass

    # -- 连接检测 ---------------------------------------------------------
    def test_connection(self, timeout: Optional[float] = None) -> Dict[str, Any]:
        """设置页/连接探测：不抛异常，返回 ``{ok, configured, elapsed_ms, capabilities, ...}``。"""
        summary = self.cfg._summary()
        if not summary.get("configured"):
            return {
                "ok": False,
                "configured": False,
                "elapsed_ms": 0,
                "capabilities": {"narrative": {"endpoint": "local-llm"}},
                "narrative_available": True,
                "model": None,
                "provider_name": "",
                "error": {
                    "code": CODE_NOT_CONFIGURED,
                    "message": "未配置模型 API（设置 → 模型 API），报告将使用本地模板生成",
                },
            }
        provider = self.service.get_default() or {}
        started = time.monotonic()
        result = self.service.test_provider(str(provider.get("provider_id") or ""))
        elapsed_ms = int(result.get("latency_ms") or (time.monotonic() - started) * 1000)
        if result.get("ok"):
            return {
                "ok": True,
                "configured": True,
                "elapsed_ms": elapsed_ms,
                "capabilities": {"narrative": {"endpoint": "local-llm"}},
                "narrative_available": True,
                "provider_id": provider.get("provider_id"),
                "provider_name": provider.get("name") or "",
                "model": provider.get("model") or "",
                "reply": result.get("reply") or "",
                "error": None,
            }
        return {
            "ok": False,
            "configured": True,
            "elapsed_ms": elapsed_ms,
            "capabilities": {"narrative": {"endpoint": "local-llm"}},
            "narrative_available": True,
            "provider_id": provider.get("provider_id"),
            "provider_name": provider.get("name") or "",
            "model": provider.get("model") or "",
            "reply": "",
            "error": result.get("error") or {"code": CODE_NETWORK, "message": "模型 API 测试失败"},
        }

    # -- 情感分析（≤50 条/批；失败返回 [] 交给 report_data 中性兜底） -------
    def analyze_sentiment(self, items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, str]] = []
        seen: set = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            normalized.append(
                {"id": item_id, "text": str(item.get("text") or "")[:SENTIMENT_TEXT_LIMIT]}
            )
        if not normalized:
            return []
        try:
            provider = self.service.get_default()
        except Exception:  # noqa: BLE001 - 配置异常按未配置处理
            provider = None
        if not provider:
            return []
        results: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(normalized), SENTIMENT_BATCH_SIZE):
            batch = normalized[start : start + SENTIMENT_BATCH_SIZE]
            try:
                reply = self.service.chat(
                    provider,
                    [
                        {"role": "system", "content": SENTIMENT_SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps({"items": batch}, ensure_ascii=False),
                        },
                    ],
                    max_tokens=4000,
                    temperature=0.0,
                    timeout=SENTIMENT_TIMEOUT,
                )
                data = extract_json_value(reply)
                rows = data.get("items") if isinstance(data, dict) else data
                if not isinstance(rows, list):
                    raise LLMError("情感分析响应缺少 items 数组", code=CODE_BAD_RESPONSE)
            except Exception as exc:  # noqa: BLE001 - 情感打标失败绝不阻断报告
                log.warning(
                    "情感分析失败，按中性兜底（批 %s 条）：%s: %s",
                    len(batch),
                    type(exc).__name__,
                    str(exc)[:200],
                )
                return []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                item_id = str(row.get("id") or "").strip()
                if not item_id or item_id not in {item["id"] for item in batch}:
                    continue
                results[item_id] = _normalize_sentiment_row(item_id, row)
        merged: List[Dict[str, Any]] = []
        for item in normalized:
            merged.append(
                results.get(
                    item["id"],
                    {
                        "id": item["id"],
                        "sentiment": "neutral",
                        "confidence": 0.0,
                        "reason": "模型未返回该条结果，按中性兜底",
                    },
                )
            )
        return merged

    # -- narrative（报告中心的唯一 AI 入口） ------------------------------
    def generate_narrative(
        self,
        package: Dict[str, Any],
        instruction: str = "",
        previous_narrative: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """数据包 → narrative JSON（本地渲染 HTML）；失败抛 :class:`LLMError` 由报告中心降级。"""
        if not isinstance(package, dict) or not package:
            raise LLMError("报告数据包必须是 JSON 对象", code=CODE_INVALID_CONFIG)
        provider = self.service.get_default()
        if not provider:
            raise LLMError(
                "未配置模型 API（设置 → 模型 API），报告将使用本地模板生成",
                code=CODE_NOT_CONFIGURED,
            )
        payload = _narrative_user_payload(package, instruction, previous_narrative)
        reply = self.service.chat(
            provider,
            [
                {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            max_tokens=NARRATIVE_MAX_TOKENS,
            temperature=NARRATIVE_TEMPERATURE,
            timeout=DEFAULT_TIMEOUT,
        )
        value = extract_json_value(reply)
        narrative = _normalize_narrative(
            value,
            provider,
            previous=previous_narrative if isinstance(previous_narrative, dict) else None,
        )
        return {
            "ok": True,
            "model": str(provider.get("model") or ""),
            "narrative": narrative,
            "meta": {
                "generated_by": _LOCAL_AI_GENERATED_BY,
                "provider_id": provider.get("provider_id") or "",
                "provider_name": provider.get("name") or "",
                "fallback_reason": "",
            },
        }

    # -- 旧 /reports 五方法：报告已改为本地直连，保留空实现（不参与任何路径） --
    def create_report(self, package: Dict[str, Any], *, instruction: str = "") -> Dict[str, Any]:
        raise LLMError(
            "当前版本已取消远端报告任务接口（报告由模型直连 + 本地渲染）",
            code=CODE_UNSUPPORTED,
        )

    def get_report(self, report_id: str, version: Optional[int] = None) -> Dict[str, Any]:
        raise LLMError("当前版本已取消远端报告任务接口", code=CODE_UNSUPPORTED)

    def cancel_report(self, report_id: str) -> Dict[str, Any]:
        return {"ok": True}

    def get_html(self, report_id: str, version: Optional[int] = None) -> Dict[str, Any]:
        raise LLMError("当前版本已取消远端报告任务接口", code=CODE_UNSUPPORTED)

    def revise(self, report_id: str, instruction: str, base_version: int) -> Dict[str, Any]:
        raise LLMError("当前版本已取消远端报告任务接口", code=CODE_UNSUPPORTED)

    def list_reports(self) -> Dict[str, Any]:
        return {"reports": []}


def _normalize_sentiment_row(item_id: str, row: Dict[str, Any]) -> Dict[str, Any]:
    raw_label = str(row.get("sentiment") or row.get("label") or "neutral").strip().lower()
    sentiment = _SENTIMENT_ALIASES.get(raw_label, "neutral")
    try:
        confidence = float(row.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence != confidence:  # NaN
        confidence = 0.0
    return {
        "id": item_id,
        "sentiment": sentiment,
        "confidence": max(0.0, min(confidence, 1.0)),
        "reason": _text(row.get("reason") or row.get("note") or "", 120),
    }


__all__ = [
    "LLMError",
    "LLMService",
    "LocalLLMClient",
    "PROVIDER_KINDS",
    "KIND_LABELS",
    "SCHEMA_VERSION",
    "NARRATIVE_SYSTEM_PROMPT",
    "extract_json_value",
    "mask_base_url",
    "split_endpoint",
    "normalize_base_url",
    "openai_chat_url",
    "anthropic_chat_url",
]

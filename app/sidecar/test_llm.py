# -*- coding: utf-8 -*-
"""模型 API 配置 + 报告直连离线自测（不联网、不启动浏览器、不需要真实 API Key）。

运行方式::

    MediaCrawler\\.venv\\Scripts\\python.exe app/sidecar/test_llm.py

覆盖：

1. provider CRUD / 脱敏 / 密钥加密 roundtrip / 校验错误；
2. OpenAI 兼容 / DeepSeek / Anthropic 三种协议的 chat 解析与 URL 归一化；
3. 错误分类（网络 / 超时 / HTTP 4xx / HTTP 5xx / 响应解析失败）；
4. narrative JSON 提取（Markdown 围栏 / 前后文字 / 修订合并）与脏输出降级；
5. 情感批量打标（≤50 条/批、id 对齐、失败返回 [] 中性兜底）；
6. 报告中心集成：AI 成功 completed（local-ai）、调用失败/未配置 → degraded
   （local-template）、修订 v2、事件 publish（finished/degraded/failed/cancelled）。

所有模型请求都指向进程内 ``_fake_llm_server``（127.0.0.1 随机端口），不访问外网。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SIDECAR_DIR = Path(__file__).resolve().parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))

PROJECT_ROOT = SIDECAR_DIR.parent.parent

from _fake_llm_server import FAKE_API_KEY, FAKE_MODEL, FakeLLMServer  # noqa: E402
from engine_runner import SidecarConfig  # noqa: E402
from events import bus  # noqa: E402
import state as state_mod  # noqa: E402
from llm_client import (  # noqa: E402
    CODE_BAD_NARRATIVE,
    CODE_HTTP_4XX,
    CODE_HTTP_5XX,
    CODE_INVALID_CONFIG,
    CODE_NETWORK,
    CODE_NOT_CONFIGURED,
    CODE_NOT_FOUND,
    CODE_PARSE,
    CODE_TIMEOUT,
    LLMError,
    LLMService,
    LocalLLMClient,
    anthropic_chat_url,
    extract_json_value,
    openai_chat_url,
)
from report_manager import (  # noqa: E402
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_DEGRADED,
    STATUS_FAILED,
    ReportManager,
)


# ---------------------------------------------------------------------------
# 断言器（可直接接入 selftest.Report）
# ---------------------------------------------------------------------------
class Checker:
    def __init__(self, report: Any = None) -> None:
        self.report = report
        self.passed = 0
        self.failed: List[str] = []

    def check(self, name: str, condition: Any, detail: str = "") -> None:
        if self.report is not None:
            self.report.check(name, condition, detail)  # selftest.Report 失败时抛异常
            self.passed += 1
            return
        if condition:
            self.passed += 1
            print(f"  [PASS] {name}")
            return
        text = f"{name} {detail}".strip()
        self.failed.append(text)
        print(f"  [FAIL] {text}")

    def section(self, title: str) -> None:
        if self.report is None:
            print(f"\n[{title}]")


def _expect_llm_error(
    func: Callable[[], Any], code: Optional[str] = None
) -> Optional[LLMError]:
    try:
        func()
    except LLMError as exc:
        if code is not None and exc.code != code:
            print(f"  [FAIL] 期望错误码 {code}，实际 {exc.code}（{exc.message}）")
            return None
        return exc
    except Exception as exc:  # noqa: BLE001
        print(f"  [FAIL] 期望 LLMError，实际 {type(exc).__name__}: {exc}")
        return None
    print("  [FAIL] 期望抛出 LLMError，但调用成功")
    return None


# ---------------------------------------------------------------------------
# 测试数据：完整 stats 数据包（本地 POC 模板可渲染成功）
# ---------------------------------------------------------------------------
def _sentiment_block(positive: int, neutral: int, negative: int) -> Dict[str, Any]:
    total = max(1, positive + neutral + negative)
    return {
        "positive": {"count": positive, "ratio": round(positive / total, 4)},
        "neutral": {"count": neutral, "ratio": round(neutral / total, 4)},
        "negative": {"count": negative, "ratio": round(negative / total, 4)},
        "total": total,
    }


def build_fake_stats() -> Dict[str, Any]:
    overall = _sentiment_block(2, 6, 2)
    content = _sentiment_block(1, 2, 1)
    comments = _sentiment_block(1, 4, 1)
    platform_rows = [
        {
            "rank": 1,
            "platform": "小红书",
            "mentions": 6,
            "share": 0.6,
            "content": 3,
            "comments": 3,
            "negative_ratio": 0.1667,
            "content_dominant_sentiment": "中性为主",
            "overall": _sentiment_block(1, 4, 1),
        },
        {
            "rank": 2,
            "platform": "知乎",
            "mentions": 4,
            "share": 0.4,
            "content": 1,
            "comments": 3,
            "negative_ratio": 0.25,
            "content_dominant_sentiment": "中性为主",
            "overall": _sentiment_block(1, 2, 1),
        },
    ]
    trend = {
        "window": {"days": 7, "start": "2026-11-03", "end": "2026-11-09"},
        "platforms": ["小红书", "知乎"],
        "days": [
            {
                "date": f"2026-11-0{index + 3}",
                "label": f"11-0{index + 3}",
                "total": 1 if index < 3 else 2,
                "by_platform": {
                    "小红书": 1 if index % 2 == 0 else 0,
                    "知乎": 0 if index % 2 == 0 else 1,
                },
            }
            for index in range(7)
        ],
        "window_totals": {
            "items": 10,
            "content": 4,
            "comments": 6,
            "daily_average": 1.4,
            "by_platform": {"小红书": 6, "知乎": 4},
        },
        "peak": {"label": "11-09", "total": 2, "by_platform": {"小红书": 1, "知乎": 1}},
    }
    negative_events = [
        {
            "event_id": "NEG-01",
            "rank": 1,
            "platform": "知乎",
            "title": "示例科技项目交付进度遭质疑",
            "summary": "有用户反馈项目交付节奏偏慢，希望官方说明排期。",
            "url": "https://example.com/zhihu/1",
            "published_date": "2026-10-20",
            "interactions": {"likes": 12, "comments": 5, "shares": 2},
            "engagement": 19,
            "heat_score": 32.5,
            "heat_rank": 3,
            "handling_code": "watch",
            "handling_status": "持续观察",
            "handling_basis": "近 14 天仍有负面评论互动",
            "entity_risk": "",
            "days_old": 20,
        },
        {
            "event_id": "NEG-02",
            "rank": 2,
            "platform": "小红书",
            "title": "历史笔记提及产品体验一般",
            "summary": "旧笔记反馈产品体验一般，当前无新增互动。",
            "url": "",
            "published_date": "2025-08-01",
            "interactions": {"likes": 3},
            "engagement": 3,
            "heat_score": 12.0,
            "heat_rank": 8,
            "handling_code": "archived",
            "handling_status": "历史存量·已沉淀",
            "handling_basis": "发布时间超过 180 天",
            "entity_risk": "",
            "days_old": 280,
        },
    ]
    positive_events = [
        {
            "event_id": "POS-01",
            "rank": 1,
            "platform": "小红书",
            "title": "示例科技获行业媒体报道",
            "summary": "媒体报道示例科技在数据安全方向的实践，整体正面。",
            "url": "https://example.com/xhs/1",
            "published_date": "2026-11-05",
            "interactions": {"likes": 80, "comments": 12, "shares": 6},
            "engagement": 98,
            "heat_score": 41.0,
            "heat_rank": 1,
        }
    ]
    return {
        "stats_fingerprint": "sha256:test-fingerprint",
        "report": {
            "brand": "示例科技",
            "title": "示例科技舆情监测周报",
            "report_date": "2026-11-09",
            "version": 1,
            "scope_mode": "period",
            "period": {"start": "2026-11-03", "end": "2026-11-09"},
            "collected_at": "2026-11-09 18:00:00",
        },
        "totals": {"items": 10, "content": 4, "comments": 6, "platforms": 2},
        "sentiment": {
            "overall": overall,
            "content": content,
            "comments": comments,
            "by_platform": platform_rows,
        },
        "risk": {
            "level": "低",
            "level_code": "low",
            "negative_events": 2,
            "negative_ratio": 0.2,
            "content_negative_ratio": 0.25,
            "thresholds": {"low": 0.1, "high": 0.3},
            "basis": "负面占比 20.0%，低于低风险阈值 10.0% 以上",
        },
        "platform_ranking": platform_rows,
        "trend": trend,
        "negative_events": negative_events,
        "positive_events": positive_events,
        "keywords": {
            "top": [
                {
                    "keyword": "数据安全",
                    "doc_count": 5,
                    "frequency": 8,
                    "platforms": ["小红书", "知乎"],
                },
                {
                    "keyword": "交付进度",
                    "doc_count": 3,
                    "frequency": 4,
                    "platforms": ["知乎"],
                },
            ],
            "related": [{"keyword": "数据安全", "doc_count": 5, "platforms": ["小红书"]}],
        },
        "comments": {"total": 6, "comment_to_content_ratio": "1.5"},
        "data_quality": {
            "content_rows": 4,
            "comment_rows": 6,
            "content_id_precision_fixed": 0,
            "comment_ids_numeric_risk": 0,
        },
        "warnings": [],
    }


def build_fake_package(
    *,
    window_start: str = "2026-11-01",
    window_end: str = "2026-11-09",
    keywords: Optional[List[str]] = None,
    platforms: Optional[List[str]] = None,
    sentiment_analyzer: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
    total_override: Optional[int] = None,
    **_: Any,
) -> Dict[str, Any]:
    """selftest 用完整数据包（结构合法、可被本地 POC 模板渲染）。"""
    keywords = list(keywords or ["示例科技"])
    platforms = list(platforms or ["xhs", "dy"])
    stats = build_fake_stats()
    if sentiment_analyzer is not None:
        sample_items = [
            {"id": "c1", "text": "示例科技表现积极，好评较多"},
            {"id": "c2", "text": "项目投诉交付慢，体验一般"},
            {"id": "c3", "text": "行业关注度上升，媒体报道"},
        ]
        try:
            rows = sentiment_analyzer(sample_items) or []
        except Exception:  # noqa: BLE001 - 与真实 report_data 一致：情感失败不阻断
            rows = []
        stats["data_quality"]["sentiment"] = {
            "analyzed": len(rows),
            "positive": sum(1 for row in rows if row.get("sentiment") == "positive"),
            "negative": sum(1 for row in rows if row.get("sentiment") == "negative"),
            "neutral": sum(
                1 for row in rows if row.get("sentiment") not in ("positive", "negative")
            ),
            "fallback": not bool(rows),
        }
    counts = {
        "content": 4,
        "comments": 6,
        "total": 10,
        "by_platform": [
            {"platform": code, "content": 2, "comments": 3} for code in platforms
        ],
    }
    if total_override is not None:
        counts["total"] = int(total_override)
    return {
        "schema_version": "public-sentiment-data@v1",
        "package_id": "pkg_test_llm",
        "generated_at": "2026-11-09T10:00:00+00:00",
        "source": {"app": "opinionlens", "app_version": "test"},
        "report": {
            "brand": "示例科技",
            "title": "示例科技舆情监测周报",
            "window": {
                "mode": "publish_time",
                "start": str(window_start),
                "end": str(window_end),
                "timezone": "Asia/Shanghai",
                "label": f"{window_start}~{window_end}",
            },
            "keywords": keywords,
            "platforms": platforms,
        },
        "counts": counts,
        "stats": stats,
        "samples": {
            "negative": [],
            "positive": [],
            "top_comments": [],
            "hot_keywords": keywords[:5],
        },
        "contents": [],
        "meta": {
            "stats_fingerprint": "sha256:test-fingerprint",
            "filtering": {
                "window_start": str(window_start),
                "window_end": str(window_end),
                "platforms": platforms,
            },
            "data_freshness": "test",
        },
    }


def build_fake_package_func() -> Callable[..., Dict[str, Any]]:
    return build_fake_package


# ---------------------------------------------------------------------------
# 测试工具
# ---------------------------------------------------------------------------
def _make_llm(tmp_dir: Path) -> tuple:
    store = state_mod.StateStore(tmp_dir / "app.db")
    return store, LLMService(None, store)


def _create_provider(
    llm: LLMService,
    server: FakeLLMServer,
    *,
    kind: str = "openai_compatible",
    name: str = "测试模型",
    model: str = FAKE_MODEL,
    api_key: str = FAKE_API_KEY,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    return llm.create_provider(
        {
            "name": name,
            "kind": kind,
            "base_url": base_url if base_url is not None else server.base_url,
            "model": model,
            "api_key": api_key,
        }
    )


def _wait_report(manager: ReportManager, report_id: str, timeout: float = 30.0) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = manager.get_report(report_id)
        if last.get("status") in (STATUS_COMPLETED, STATUS_DEGRADED, STATUS_FAILED, STATUS_CANCELLED):
            return last
        time.sleep(0.02)
    return last


def _capture_events() -> tuple:
    events: List[Dict[str, Any]] = []

    def handler(event: Dict[str, Any]) -> None:
        events.append(event)

    bus.subscribe(handler)
    return events, handler


def _find_event(events: List[Dict[str, Any]], event_type: str, dedupe_key: str) -> Optional[Dict[str, Any]]:
    for event in events:
        if event.get("type") == event_type and str(event.get("dedupe_key")) == dedupe_key:
            return event
    return None


# ---------------------------------------------------------------------------
# 用例组
# ---------------------------------------------------------------------------
def test_json_extraction(check: Checker) -> None:
    check.section("JSON 提取")
    value = extract_json_value('前言\n```json\n{"a": 1, "b": {"c": "}"}}\n```\n后记')
    check.check("围栏 + 前后文字可提取 JSON", value == {"a": 1, "b": {"c": "}"}}, repr(value))
    value = extract_json_value('说明：{"items": [{"id": "x", "text": "包含 } 的括号"}]} 结束')
    check.check("非围栏 JSON 可提取（字符串内括号不干扰）", value == {"items": [{"id": "x", "text": "包含 } 的括号"}]}, repr(value))
    value = extract_json_value('[{"id": "a"}]')
    check.check("根为数组的 JSON 可提取", value == [{"id": "a"}], repr(value))
    error = _expect_llm_error(lambda: extract_json_value("模型拒绝回答"), CODE_PARSE)
    check.check("无 JSON 时抛 LLMError(LLM_PARSE)", error is not None)
    error = _expect_llm_error(lambda: extract_json_value(""), CODE_PARSE)
    check.check("空回复抛 LLMError(LLM_PARSE)", error is not None)


def test_url_normalization(check: Checker, server: FakeLLMServer) -> None:
    check.section("端点归一化")
    host = server.base_url
    cases = [
        (host, host + "/v1/chat/completions"),
        (host + "/", host + "/v1/chat/completions"),
        (host + "/v1", host + "/v1/chat/completions"),
        (host + "/v1/", host + "/v1/chat/completions"),
        (host + "/v1/chat/completions", host + "/v1/chat/completions"),
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/chat/completions"),
        ("http://127.0.0.1:11434/v1", "http://127.0.0.1:11434/v1/chat/completions"),
        ("https://relay.example.com/api", "https://relay.example.com/api/v1/chat/completions"),
    ]
    for source, expected in cases:
        check.check(
            f"openai_chat_url({source})",
            openai_chat_url(source) == expected,
            f"实际 {openai_chat_url(source)}",
        )
    anthropic_cases = [
        ("https://api.anthropic.com", "https://api.anthropic.com/v1/messages"),
        ("https://api.anthropic.com/v1", "https://api.anthropic.com/v1/messages"),
        ("https://proxy.example.com/v1/messages", "https://proxy.example.com/v1/messages"),
    ]
    for source, expected in anthropic_cases:
        check.check(
            f"anthropic_chat_url({source})",
            anthropic_chat_url(source) == expected,
            f"实际 {anthropic_chat_url(source)}",
        )


def test_provider_crud(check: Checker, server: FakeLLMServer, tmp_dir: Path) -> None:
    check.section("provider CRUD / 脱敏 / 密钥 roundtrip")
    store, llm = _make_llm(tmp_dir)
    try:
        provider = _create_provider(llm, server, api_key="sk-secret-abcd")
        check.check(
            "创建 provider 返回脱敏字段（api_key_masked=****abcd / secret_configured）",
            provider.get("api_key_masked") == "****abcd"
            and provider.get("secret_configured") is True
            and provider.get("is_default") is True
            and provider.get("enabled") is True
            and provider.get("kind") == "openai_compatible"
            and provider.get("model") == FAKE_MODEL,
            json.dumps(provider, ensure_ascii=False)[:500],
        )
        providers = llm.list_providers()
        payload_text = json.dumps(providers, ensure_ascii=False)
        check.check(
            "列表不返回明文密钥（只回 api_key_masked）",
            "sk-secret-abcd" not in payload_text
            and providers[0].get("api_key_masked") == "****abcd"
            and "secret_enc" not in providers[0],
            payload_text[:400],
        )
        rows = store.query("SELECT secret_enc FROM llm_providers")
        stored = str((rows[0].get("secret_enc") if rows else "") or "")
        check.check(
            "库里存的是加密密文（dpapi:/b64: 前缀，非明文）",
            bool(stored)
            and stored != "sk-secret-abcd"
            and (stored.startswith("dpapi:") or stored.startswith("b64:")),
            stored[:40],
        )
        internal = llm.get_provider(provider["provider_id"], masked=False)
        check.check(
            "密钥加密后 can roundtrip 解密（DPAPI/b64 roundtrip）",
            internal.get("api_key") == "sk-secret-abcd",
            repr(internal.get("api_key"))[:40],
        )
        second = llm.create_provider(
            {
                "name": "DeepSeek 默认",
                "kind": "deepseek",
                "api_key": "sk-ds-0000",
            }
        )
        check.check(
            "deepseek 可省略 base_url/model（默认 https://api.deepseek.com + deepseek-chat）",
            second.get("base_url") == "https://api.deepseek.com"
            and second.get("model") == "deepseek-chat"
            and second.get("is_default") is False,
            json.dumps(second, ensure_ascii=False)[:400],
        )
        llm.set_default(second["provider_id"])
        first_after = llm.get_provider(provider["provider_id"])
        default_after = llm.get_default() or {}
        check.check(
            "set_default 生效且默认唯一",
            first_after.get("is_default") is False
            and default_after.get("provider_id") == second["provider_id"],
            json.dumps(default_after, ensure_ascii=False)[:300],
        )
        updated = llm.update_provider(provider["provider_id"], {"name": "改个名字"})
        internal_after = llm.get_provider(provider["provider_id"], masked=False)
        check.check(
            "PATCH 不传 api_key = 保留原密钥",
            updated.get("name") == "改个名字"
            and internal_after.get("api_key") == "sk-secret-abcd",
            json.dumps(updated, ensure_ascii=False)[:300],
        )
        updated = llm.update_provider(provider["provider_id"], {"api_key": "sk-new-9999"})
        check.check(
            "PATCH 传新 api_key 覆盖（脱敏 ****9999）",
            updated.get("api_key_masked") == "****9999"
            and llm.get_provider(provider["provider_id"], masked=False).get("api_key")
            == "sk-new-9999",
            json.dumps(updated, ensure_ascii=False)[:300],
        )
        cleared = llm.update_provider(provider["provider_id"], {"clear_secret": True})
        check.check(
            "clear_secret=true 清空密钥",
            cleared.get("secret_configured") is False
            and cleared.get("api_key_masked") == "",
            json.dumps(cleared, ensure_ascii=False)[:300],
        )
        disabled = llm.update_provider(provider["provider_id"], {"enabled": False})
        check.check("PATCH enabled=false", disabled.get("enabled") is False)
        llm.update_provider(provider["provider_id"], {"enabled": True})
        for name, payload, code in (
            ("kind 非法", {"kind": "foo", "base_url": "http://x", "model": "m"}, CODE_INVALID_CONFIG),
            ("openai 缺 base_url", {"kind": "openai_compatible", "model": "m"}, CODE_INVALID_CONFIG),
            ("缺 model", {"kind": "openai_compatible", "base_url": "http://x"}, CODE_INVALID_CONFIG),
        ):
            error = _expect_llm_error(lambda p=payload: llm.create_provider(p), code)
            check.check(f"创建校验：{name} → LLM_INVALID_CONFIG", error is not None)
        error = _expect_llm_error(
            lambda: llm.update_provider("prov_not_exists", {"name": "x"}), CODE_NOT_FOUND
        )
        check.check("PATCH 不存在的 provider → LLM_PROVIDER_NOT_FOUND", error is not None)
        deleted = llm.delete_provider(second["provider_id"])
        check.check(
            "DELETE 默认 provider 后自动把默认让给下一个启用项",
            deleted.get("ok") is True
            and deleted.get("default_provider_id") == provider["provider_id"],
            json.dumps(deleted, ensure_ascii=False)[:300],
        )
        test_result = llm.test_provider(provider["provider_id"])
        check.check(
            "test_provider 发送 ping 并记录 last_test_ok",
            test_result.get("ok") is True
            and "pong" in str(test_result.get("reply") or "").lower()
            and isinstance(test_result.get("latency_ms"), int)
            and llm.get_provider(provider["provider_id"]).get("last_test_ok") is True,
            json.dumps(test_result, ensure_ascii=False)[:400],
        )
        error = _expect_llm_error(lambda: llm.test_provider("prov_not_exists"), CODE_NOT_FOUND)
        check.check("test_provider 不存在的 id → LLM_PROVIDER_NOT_FOUND", error is not None)
        llm.delete_provider(provider["provider_id"])
        check.check(
            "删除全部 provider 后 get_default()=None / config_summary configured=false",
            llm.get_default() is None
            and llm.config_summary().get("configured") is False,
            repr(llm.config_summary()),
        )
    finally:
        store.close()


def test_chat_openai(check: Checker, server: FakeLLMServer, tmp_dir: Path) -> None:
    check.section("OpenAI 兼容 / DeepSeek 协议")
    store, llm = _make_llm(tmp_dir)
    try:
        provider = _create_provider(llm, server, api_key="sk-openai-abcd")
        internal = llm.get_provider(provider["provider_id"], masked=False)
        server.reset_records()
        reply = llm.chat(internal, [{"role": "user", "content": "ping"}], max_tokens=16, timeout=5)
        record = server.records()[-1] if server.records() else {}
        headers = record.get("headers") or {}
        check.check(
            "openai_compatible chat 解析 choices[0].message.content",
            "pong" in reply.lower(),
            repr(reply)[:200],
        )
        check.check(
            "base_url 无 /v1 时自动补 /v1/chat/completions",
            str(record.get("path") or "").endswith("/v1/chat/completions"),
            str(record.get("path")),
        )
        check.check(
            "带 Authorization: Bearer <key> 且不泄露到日志",
            str(headers.get("authorization") or "") == "Bearer sk-openai-abcd",
            str(headers.get("authorization")),
        )
        for suffix in ("/v1", "/v1/", "/v1/chat/completions"):
            variant = provider["provider_id"] + suffix.replace("/", "_")
            llm.update_provider(provider["provider_id"], {"base_url": server.base_url + suffix})
            server.reset_records()
            reply = llm.chat(
                llm.get_provider(provider["provider_id"], masked=False),
                [{"role": "user", "content": "ping"}],
                max_tokens=16,
                timeout=5,
            )
            check.check(
                f"base_url 填 {server.base_url}{suffix} 也能正确请求",
                "pong" in reply.lower()
                and str((server.records()[-1] or {}).get("path") or "").endswith(
                    "/chat/completions"
                ),
                str((server.records()[-1] or {}).get("path")),
            )
        llm.update_provider(provider["provider_id"], {"base_url": server.base_url})
        deepseek = llm.create_provider(
            {
                "name": "deepseek-test",
                "kind": "deepseek",
                "base_url": server.base_url,
                "model": "deepseek-chat",
                "api_key": "sk-deep-abcd",
            }
        )
        server.reset_records()
        reply = llm.chat(
            llm.get_provider(deepseek["provider_id"], masked=False),
            [{"role": "user", "content": "ping"}],
            max_tokens=16,
            timeout=5,
        )
        body = (server.records()[-1] or {}).get("body") or {}
        check.check(
            "deepseek 走 OpenAI 协议（model/base_url 透传）",
            "pong" in reply.lower() and body.get("model") == "deepseek-chat",
            json.dumps(body, ensure_ascii=False)[:300],
        )
        internal = llm.get_provider(provider["provider_id"], masked=False)
        server.mode = "fail"
        error = _expect_llm_error(lambda: llm.chat(internal, [{"role": "user", "content": "ping"}], timeout=5), CODE_HTTP_5XX)
        check.check("HTTP 500 → LLMError(LLM_HTTP_5XX, retryable)", error is not None and error.retryable)
        server.mode = "auth_fail"
        error = _expect_llm_error(lambda: llm.chat(internal, [{"role": "user", "content": "ping"}], timeout=5), CODE_HTTP_4XX)
        check.check("HTTP 401 → LLMError(LLM_HTTP_4XX)", error is not None and error.status_code == 401)
        server.mode = "http_garbage"
        error = _expect_llm_error(lambda: llm.chat(internal, [{"role": "user", "content": "ping"}], timeout=5), CODE_PARSE)
        check.check("HTTP 200 非 JSON → LLMError(LLM_PARSE)", error is not None)
        server.mode = "timeout"
        error = _expect_llm_error(
            lambda: llm.chat(internal, [{"role": "user", "content": "ping"}], timeout=0.3),
            CODE_TIMEOUT,
        )
        check.check("请求超时 → LLMError(LLM_TIMEOUT, retryable)", error is not None and error.retryable)
        server.mode = "ok"
        unreachable = dict(internal, base_url="http://127.0.0.1:1")
        try:
            llm.chat(unreachable, [{"role": "user", "content": "ping"}], timeout=1)
            network_error: Optional[LLMError] = None
        except LLMError as exc:
            network_error = exc
        check.check(
            "端口不可达 → LLMError(LLM_NETWORK 或连接超时 LLM_TIMEOUT)",
            network_error is not None
            and network_error.code in (CODE_NETWORK, CODE_TIMEOUT),
            f"code={getattr(network_error, 'code', None)} msg={str(network_error)[:120]}",
        )
    finally:
        store.close()


def test_chat_anthropic(check: Checker, server: FakeLLMServer, tmp_dir: Path) -> None:
    check.section("Anthropic 协议")
    store, llm = _make_llm(tmp_dir)
    try:
        provider = _create_provider(llm, server, kind="anthropic", api_key="sk-ant-abcd")
        internal = llm.get_provider(provider["provider_id"], masked=False)
        server.reset_records()
        reply = llm.chat(
            internal,
            [
                {"role": "system", "content": "你是测试助手"},
                {"role": "user", "content": "ping"},
            ],
            max_tokens=16,
            timeout=5,
        )
        record = server.records()[-1] if server.records() else {}
        body = record.get("body") or {}
        headers = record.get("headers") or {}
        check.check(
            "anthropic 请求 /v1/messages 且解析 content[0].text",
            str(record.get("path") or "").endswith("/v1/messages")
            and "pong" in reply.lower(),
            f"path={record.get('path')} reply={reply!r}",
        )
        check.check(
            "anthropic headers：x-api-key + anthropic-version: 2023-06-01",
            str(headers.get("x-api-key") or "") == "sk-ant-abcd"
            and str(headers.get("anthropic-version") or "") == "2023-06-01",
            json.dumps(headers, ensure_ascii=False)[:300],
        )
        check.check(
            "anthropic body：system 独立字段、messages 不含 system",
            body.get("system") == "你是测试助手"
            and [item.get("role") for item in body.get("messages") or []] == ["user"],
            json.dumps(body, ensure_ascii=False)[:400],
        )
        server.mode = "fail"
        error = _expect_llm_error(lambda: llm.chat(internal, [{"role": "user", "content": "ping"}], timeout=5), CODE_HTTP_5XX)
        check.check("anthropic HTTP 500 → LLM_HTTP_5XX", error is not None)
    finally:
        server.mode = "ok"
        store.close()


def test_sentiment(check: Checker, server: FakeLLMServer, tmp_dir: Path) -> None:
    check.section("情感批量打标")
    store, llm = _make_llm(tmp_dir)
    try:
        _create_provider(llm, server)
        client = LocalLLMClient(None, llm=llm)
        items = [
            {
                "id": f"item_{index}",
                "text": "示例科技好评积极" if index % 2 == 0 else "项目投诉交付慢，负面",
            }
            for index in range(120)
        ]
        server.reset_records()
        rows = client.analyze_sentiment(items)
        sizes = server.sentiment_batch_sizes()
        indexed = {str(row.get("id")): row for row in rows}
        check.check(
            "analyze_sentiment 120 条自动 ≤50 分批（50/50/20）",
            len(rows) == 120 and sizes == [50, 50, 20],
            f"rows={len(rows)} sizes={sizes}",
        )
        check.check(
            "情感结果与输入 id 对齐且标签合法",
            indexed.get("item_0", {}).get("sentiment") == "positive"
            and indexed.get("item_1", {}).get("sentiment") == "negative"
            and all(
                row.get("sentiment") in ("positive", "neutral", "negative") for row in rows
            ),
            repr(indexed.get("item_0")),
        )
        server.mode = "fail"
        rows = client.analyze_sentiment([{"id": "x", "text": "测试"}])
        check.check("情感调用失败返回 []（不抛异常，中性兜底）", rows == [], repr(rows))
        server.mode = "ok"
        empty_client = LocalLLMClient(
            None, LLMService(None, state_mod.StateStore(tmp_dir / "empty" / "app.db"))
        )
        try:
            rows = empty_client.analyze_sentiment([{"id": "x", "text": "测试"}])
        finally:
            empty_client.close()
        check.check("未配置模型 API 时 analyze_sentiment 返回 []", rows == [], repr(rows))
    finally:
        store.close()


def test_narrative(check: Checker, server: FakeLLMServer, tmp_dir: Path) -> None:
    check.section("narrative 生成 / 提取 / 降级")
    store, llm = _make_llm(tmp_dir)
    try:
        _create_provider(llm, server)
        client = LocalLLMClient(None, llm=llm)
        package = build_fake_package()
        server.reset_records()
        response = client.generate_narrative(package)
        narrative = response.get("narrative") or {}
        sent = server.narrative_requests[-1] if server.narrative_requests else {}
        check.check(
            "generate_narrative 返回 ok/model/narrative/meta.generated_by=local-llm",
            response.get("ok") is True
            and response.get("model") == FAKE_MODEL
            and response.get("meta", {}).get("generated_by") == "local-llm"
            and isinstance(narrative, dict),
            json.dumps(response, ensure_ascii=False)[:500],
        )
        check.check(
            "narrative 必填字段完整（overview/trend/sentiment/platform/negative/positive/suggestions/risk）",
            bool(narrative.get("overview", {}).get("one_line_summary"))
            and bool(narrative.get("trend", {}).get("insight"))
            and bool(narrative.get("sentiment", {}).get("insight"))
            and bool(narrative.get("platform", {}).get("insight"))
            and isinstance(narrative.get("negative", {}).get("measures"), list)
            and isinstance(narrative.get("positive", {}).get("follow_ups"), list)
            and len(narrative.get("suggestions") or []) >= 1
            and narrative.get("risk", {}).get("level") in ("低", "中", "高"),
            json.dumps(narrative, ensure_ascii=False)[:600],
        )
        check.check(
            "user payload 传 stats / report_meta（不传 contents，控制 token）",
            isinstance(sent.get("stats"), dict)
            and isinstance(sent.get("report_meta"), dict)
            and "contents" not in sent
            and (sent.get("report_meta") or {}).get("title") == "示例科技舆情监测周报",
            json.dumps(sent, ensure_ascii=False)[:400],
        )
        check.check(
            "修订时 previous_narrative / instruction 传给模型",
            bool(sent.get("previous_narrative")) is False
            and client.generate_narrative(
                package, instruction="把建议写得更具体", previous_narrative=narrative
            ).get("ok")
            is True
            and "previous_narrative" in (server.narrative_requests[-1] or {})
            and (server.narrative_requests[-1] or {}).get("instruction") == "把建议写得更具体",
            detail=json.dumps(
                server.narrative_requests[-1] if server.narrative_requests else {},
                ensure_ascii=False,
            )[:500],
        )
        server.mode = "dirty"
        response = client.generate_narrative(package)
        check.check(
            "脏输出（Markdown 围栏 + 前后文字）仍能提取 narrative",
            response.get("ok") is True
            and bool((response.get("narrative") or {}).get("overview", {}).get("one_line_summary")),
            json.dumps(response, ensure_ascii=False)[:400],
        )
        server.mode = "invalid_json"
        error = _expect_llm_error(lambda: client.generate_narrative(package), CODE_PARSE)
        check.check("模型输出无 JSON → LLMError(LLM_PARSE)，由报告中心降级", error is not None)
        server.mode = "missing_fields"
        error = _expect_llm_error(lambda: client.generate_narrative(package), CODE_BAD_NARRATIVE)
        check.check("模型输出缺必填字段 → LLMError(LLM_BAD_NARRATIVE)", error is not None)
        server.mode = "fail"
        error = _expect_llm_error(lambda: client.generate_narrative(package), CODE_HTTP_5XX)
        check.check("模型 HTTP 500 → LLMError(LLM_HTTP_5XX)", error is not None)
        server.mode = "ok"
        empty_client = LocalLLMClient(
            None, LLMService(None, state_mod.StateStore(tmp_dir / "empty" / "app.db"))
        )
        try:
            error = _expect_llm_error(
                lambda: empty_client.generate_narrative(package), CODE_NOT_CONFIGURED
            )
        finally:
            empty_client.close()
        check.check("未配置模型 API → LLMError(LLM_NOT_CONFIGURED)", error is not None)
    finally:
        store.close()


def _report_cfg(data_dir: Path) -> SidecarConfig:
    return SidecarConfig(
        data_dir=data_dir,
        mc_root=PROJECT_ROOT / "MediaCrawler",
        python=Path(sys.executable),
        engine_entry=PROJECT_ROOT / "app" / "engine" / "entry.py",
    )


def test_report_integration(check: Checker, server: FakeLLMServer, tmp_dir: Path) -> None:
    check.section("报告中心集成（AI 成功 / 降级 / 修订 / 事件）")
    events: List[Dict[str, Any]] = []
    handler: Optional[Callable[[Dict[str, Any]], None]] = None
    manager: Optional[ReportManager] = None
    try:
        events, handler = _capture_events()
        store, llm = _make_llm(tmp_dir / "report_ai")
        provider = _create_provider(llm, server, name="集成测试模型")
        data_dir = tmp_dir / "report_ai" / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        manager = ReportManager(
            _report_cfg(data_dir),
            llm=llm,
            report_data_loader=lambda: (build_fake_package, ""),
        )
        config = manager.config_info()
        check.check(
            "GET /api/reports/config：configured + provider_name/model/base_url 脱敏 + 旧字段",
            config.get("configured") is True
            and config.get("provider_name") == "集成测试模型"
            and config.get("model") == FAKE_MODEL
            and str(config.get("base_url") or "").startswith("http://127.0.0.1")
            and config.get("degraded_fallback") is True
            and config.get("report_data_ready") is True
            and config.get("schema_version") == "public-sentiment-data@v1"
            and config.get("max_versions") == 10,
            json.dumps(config, ensure_ascii=False)[:600],
        )
        connection = manager.connection_info()
        check.check(
            "connection_info：configured/connected/narrative_endpoint=local-llm/provider_name",
            connection.get("configured") is True
            and connection.get("connected") is True
            and connection.get("narrative_available") is True
            and connection.get("narrative_endpoint") == "local-llm"
            and connection.get("provider_name") == "集成测试模型"
            and connection.get("local_fallback") is True,
            json.dumps(connection, ensure_ascii=False)[:500],
        )
        tested = manager.test_connection()
        check.check(
            "test_connection：ping 默认 provider 成功（ok/model）",
            tested.get("ok") is True and tested.get("model") == FAKE_MODEL,
            json.dumps(tested, ensure_ascii=False)[:500],
        )
        server.reset_records()
        created = manager.create_report(
            {
                "window_start": "2026-11-01",
                "window_end": "2026-11-09",
                "keywords": ["示例科技"],
                "platforms": ["xhs", "dy"],
            }
        )
        report_id = str(created.get("report_id") or "")
        final = _wait_report(manager, report_id)
        check.check(
            "报告生成完成：completed / progress=100 / v1 html_available",
            final.get("status") == STATUS_COMPLETED
            and int(final.get("progress") or 0) == 100
            and int(final.get("current_version") or 0) == 1
            and final.get("html_available") is True
            and len(final.get("versions") or []) == 1,
            json.dumps(final, ensure_ascii=False)[:900],
        )
        check.check(
            "AI 成功路径：source=local-ai / narrative_generated_by=local-llm",
            (final.get("meta") or {}).get("source") == "local-ai"
            and final.get("narrative_generated_by") == "local-llm"
            and (final.get("meta") or {}).get("ai_fallback_reason") == "",
            json.dumps(final.get("meta"), ensure_ascii=False)[:600],
        )
        html_path = data_dir / "reports" / report_id / "v1.html"
        html = html_path.read_text(encoding="utf-8") if html_path.is_file() else ""
        check.check(
            "HTML 落盘且为本地 POC 模板渲染（7 章节 + 标题/示例科技）",
            html_path.is_file()
            and "section-overview" in html
            and "section-suggestions" in html
            and "示例科技舆情监测周报" in html
            and len(html) > 50_000,
            f"bytes={len(html)}",
        )
        check.check(
            "narrative_v1.json 落盘（调试/修订基准）",
            (data_dir / "reports" / report_id / "narrative_v1.json").is_file(),
        )
        check.check(
            "生成调用了情感打标（fake /sentiment 每批 ≤50 条）",
            len(server.sentiment_batches) >= 1
            and all(len(batch) <= 50 for batch in server.sentiment_batches),
            f"batches={server.sentiment_batch_sizes()}",
        )
        check.check(
            "模型收到数据包（report_meta/stats，含关键词与窗口）",
            bool(server.narrative_requests)
            and (server.narrative_requests[-1].get("report_meta") or {}).get(
                "keywords"
            )
            == ["示例科技"]
            and (((server.narrative_requests[-1].get("report_meta") or {}).get("window")) or {}).get(
                "start"
            )
            == "2026-11-01",
            json.dumps(server.narrative_requests[-1] if server.narrative_requests else {}, ensure_ascii=False)[:500],
        )
        finished_event = _find_event(events, "report.finished", f"report:{report_id}:v1:finished")
        check.check(
            "发布 report.finished 事件（dedupe_key=report:<id>:v1:finished）",
            finished_event is not None
            and finished_event.get("severity") == "info"
            and (finished_event.get("data") or {}).get("report_id") == report_id
            and int((finished_event.get("data") or {}).get("version") or 0) == 1,
            json.dumps(finished_event or {}, ensure_ascii=False)[:500],
        )
        revised = manager.revise_report(
            report_id, "把下周建议写得更具体一些", base_version=1
        )
        revised_final = _wait_report(manager, report_id)
        v2_html_path = data_dir / "reports" / report_id / "v2.html"
        v2_html = v2_html_path.read_text(encoding="utf-8") if v2_html_path.is_file() else ""
        last_payload = server.narrative_requests[-1] if server.narrative_requests else {}
        check.check(
            "修订生成 v2：completed / current_version=2 / versions 含 v1/v2",
            revised.get("target_version") == 2
            and revised_final.get("status") == STATUS_COMPLETED
            and int(revised_final.get("current_version") or 0) == 2
            and len(revised_final.get("versions") or []) >= 2
            and v2_html_path.is_file(),
            json.dumps(revised_final, ensure_ascii=False)[:800],
        )
        check.check(
            "修订请求带 instruction + previous_narrative，且 v2 HTML 含修订说明",
            last_payload.get("instruction") == "把下周建议写得更具体一些"
            and isinstance(last_payload.get("previous_narrative"), dict)
            and "把下周建议写得更具体一些" in v2_html,
            json.dumps(last_payload, ensure_ascii=False)[:400],
        )
        finished_v2 = _find_event(
            events, "report.finished", f"report:{report_id}:v2:finished"
        )
        check.check("修订完成发布 report.finished(v2)", finished_v2 is not None)

        # ---- 模型调用失败 → 本地模板 degraded（不标 failed） ----
        server.mode = "fail"
        failed_created = manager.create_report(
            {
                "window_start": "2026-11-01",
                "window_end": "2026-11-09",
                "keywords": ["示例科技"],
            }
        )
        failed_id = str(failed_created.get("report_id") or "")
        failed_final = _wait_report(manager, failed_id)
        fallback_reason = str(failed_final.get("ai_fallback_reason") or "")
        degraded_event = _find_event(
            events, "report.degraded", f"report:{failed_id}:v1:degraded"
        )
        check.check(
            "模型 HTTP 500 → degraded / source=local-template / 原因含模型 API / HTML 可用",
            failed_final.get("status") == STATUS_DEGRADED
            and (failed_final.get("meta") or {}).get("source") == "local-template"
            and failed_final.get("narrative_generated_by") == "local-template"
            and "模型 API" in fallback_reason
            and failed_final.get("html_available") is True
            and isinstance(failed_final.get("file_path"), str),
            json.dumps(failed_final, ensure_ascii=False)[:900],
        )
        check.check(
            "发布 report.degraded 事件（dedupe_key=report:<id>:v1:degraded）",
            degraded_event is not None
            and degraded_event.get("severity") == "warn"
            and bool((degraded_event.get("data") or {}).get("error")),
            json.dumps(degraded_event or {}, ensure_ascii=False)[:500],
        )
        server.mode = "ok"
        # 已被降级的报告再次修订：模型可用时重新生成 AI 版本（v2 completed/local-ai）
        manager.revise_report(failed_id, "本地重渲染补充说明", base_version=1)
        local_revised = _wait_report(manager, failed_id)
        local_v2 = data_dir / "reports" / failed_id / "v2.html"
        check.check(
            "降级报告可修订 v2（模型恢复 → 重新 AI 生成 completed/local-ai，含修订说明）",
            local_revised.get("status") == STATUS_COMPLETED
            and int(local_revised.get("current_version") or 0) == 2
            and (local_revised.get("meta") or {}).get("source") == "local-ai"
            and local_v2.is_file()
            and "本地重渲染补充说明" in local_v2.read_text(encoding="utf-8"),
            json.dumps(local_revised, ensure_ascii=False)[:700],
        )
        # 模型再次失败时修订回退本地模板重渲染（degraded）
        server.mode = "fail"
        manager.revise_report(failed_id, "模型失败时本地重渲染", base_version=2)
        failed_revised = _wait_report(manager, failed_id)
        local_v3 = data_dir / "reports" / failed_id / "v3.html"
        check.check(
            "修订时模型失败 → degraded（本地模板重渲染，v3.html 可用）",
            failed_revised.get("status") == STATUS_DEGRADED
            and int(failed_revised.get("current_version") or 0) == 3
            and (failed_revised.get("meta") or {}).get("source") == "local-template"
            and "模型 API 修订失败" in str(failed_revised.get("ai_fallback_reason") or "")
            and local_v3.is_file(),
            json.dumps(failed_revised, ensure_ascii=False)[:800],
        )
        manager.shutdown()
        manager = None
        store.close()

        # ---- 未配置模型 API → 本地模板 degraded ----
        empty_store = state_mod.StateStore(tmp_dir / "report_unconfigured" / "app.db")
        empty_llm = LLMService(None, empty_store)
        unconfigured_dir = tmp_dir / "report_unconfigured" / "data"
        unconfigured_dir.mkdir(parents=True, exist_ok=True)
        unconfigured = ReportManager(
            _report_cfg(unconfigured_dir),
            llm=empty_llm,
            report_data_loader=lambda: (build_fake_package, ""),
        )
        try:
            ucfg = unconfigured.config_info()
            check.check(
                "未配置：config configured=false / base_url 空 / provider_name 空 / degraded_fallback=true",
                ucfg.get("configured") is False
                and ucfg.get("base_url") == ""
                and ucfg.get("provider_name") == ""
                and ucfg.get("degraded_fallback") is True,
                json.dumps(ucfg, ensure_ascii=False)[:400],
            )
            uconn = unconfigured.connection_info()
            check.check(
                "未配置：connection connected=false + error 提示未配置模型 API + local_fallback",
                uconn.get("connected") is False
                and "未配置模型 API" in str(uconn.get("error") or "")
                and uconn.get("local_fallback") is True,
                json.dumps(uconn, ensure_ascii=False)[:500],
            )
            u_created = unconfigured.create_report({"window_start": "2026-11-01", "window_end": "2026-11-09"})
            u_id = str(u_created.get("report_id") or "")
            u_final = _wait_report(unconfigured, u_id)
            check.check(
                "未配置 → 报告 degraded（local-template，原因含「未配置模型 API」）",
                u_final.get("status") == STATUS_DEGRADED
                and (u_final.get("meta") or {}).get("source") == "local-template"
                and "未配置模型 API" in str(u_final.get("ai_fallback_reason") or "")
                and u_final.get("html_available") is True,
                json.dumps(u_final, ensure_ascii=False)[:800],
            )
        finally:
            unconfigured.shutdown()
            empty_store.close()

        # ---- no-data → failed + report.failed 事件 ----
        no_data_store = state_mod.StateStore(tmp_dir / "report_nodata" / "app.db")
        no_data_llm = LLMService(None, no_data_store)
        _create_provider(no_data_llm, server)
        no_data_dir = tmp_dir / "report_nodata" / "data"
        no_data_dir.mkdir(parents=True, exist_ok=True)

        def _empty_package(**_: Any) -> Dict[str, Any]:
            return build_fake_package(total_override=0)

        no_data_manager = ReportManager(
            _report_cfg(no_data_dir),
            llm=no_data_llm,
            report_data_loader=lambda: (_empty_package, ""),
        )
        try:
            nd_created = no_data_manager.create_report(
                {"window_start": "2026-11-01", "window_end": "2026-11-09"}
            )
            nd_id = str(nd_created.get("report_id") or "")
            nd_final = _wait_report(no_data_manager, nd_id)
            nd_event = _find_event(events, "report.failed", f"report:{nd_id}:failed")
            check.check(
                "无数据 → failed（REPORT_NO_DATA）+ 发布 report.failed 事件",
                nd_final.get("status") == STATUS_FAILED
                and (nd_final.get("error") or {}).get("code") == "REPORT_NO_DATA"
                and nd_event is not None
                and (nd_event.get("data") or {}).get("error") == "REPORT_NO_DATA",
                json.dumps(nd_final, ensure_ascii=False)[:600],
            )
        finally:
            no_data_manager.shutdown()
            no_data_store.close()

        # ---- 取消 → cancelled + report.cancelled 事件 ----
        cancel_store = state_mod.StateStore(tmp_dir / "report_cancel" / "app.db")
        cancel_llm = LLMService(None, cancel_store)
        _create_provider(cancel_llm, server)
        cancel_dir = tmp_dir / "report_cancel" / "data"
        cancel_dir.mkdir(parents=True, exist_ok=True)

        def _slow_package(**kwargs: Any) -> Dict[str, Any]:
            time.sleep(0.4)
            return build_fake_package(**kwargs)

        cancel_manager = ReportManager(
            _report_cfg(cancel_dir),
            llm=cancel_llm,
            report_data_loader=lambda: (_slow_package, ""),
        )
        try:
            cancel_created = cancel_manager.create_report(
                {"window_start": "2026-11-01", "window_end": "2026-11-09"}
            )
            cancel_id = str(cancel_created.get("report_id") or "")
            cancel_manager.cancel_report(cancel_id)
            cancel_final = _wait_report(cancel_manager, cancel_id)
            cancel_event = _find_event(
                events, "report.cancelled", f"report:{cancel_id}:cancelled"
            )
            check.check(
                "取消报告 → cancelled 且发布 report.cancelled 事件",
                cancel_final.get("status") == STATUS_CANCELLED
                and cancel_event is not None,
                json.dumps(cancel_final, ensure_ascii=False)[:500],
            )
        finally:
            cancel_manager.shutdown()
            cancel_store.close()
    finally:
        if manager is not None:
            try:
                manager.shutdown()
            except Exception:  # noqa: BLE001
                pass
        if handler is not None:
            bus.unsubscribe(handler)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run(report: Any = None) -> tuple:
    """执行全部用例；返回 ``(passed, failed)``。``report`` 可传 selftest.Report 接入总自测。"""
    check = Checker(report)
    tmp_root = Path(tempfile.mkdtemp(prefix="test_llm_")).resolve()
    server: Optional[FakeLLMServer] = None
    try:
        server = FakeLLMServer().start()
        _print = (lambda *a, **k: None) if report is not None else print
        _print("=" * 78)
        _print("模型 API + 报告直连离线自测（fake LLM server，不联网）")
        _print(f"[环境] fake LLM = {server.base_url}（OpenAI + Anthropic）")
        _print(f"[环境] 临时目录 = {tmp_root}")
        _print("=" * 78)
        test_json_extraction(check)
        test_url_normalization(check, server)
        test_provider_crud(check, server, tmp_root / "crud")
        test_chat_openai(check, server, tmp_root / "chat_openai")
        test_chat_anthropic(check, server, tmp_root / "chat_anthropic")
        test_sentiment(check, server, tmp_root / "sentiment")
        test_narrative(check, server, tmp_root / "narrative")
        test_report_integration(check, server, tmp_root / "report")
    except Exception as exc:  # noqa: BLE001 - 兜底避免脚本直接崩
        text = f"未预期异常：{type(exc).__name__}: {exc}"
        check.failed.append(text)
        print(f"\n[FAIL] {text}")
        traceback.print_exc(limit=8)
    finally:
        if server is not None:
            try:
                server.stop()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmp_root, ignore_errors=True)
        if report is None:
            print("\n" + "=" * 78)
            print(f"结果：PASS {check.passed} / FAIL {len(check.failed)}")
            for item in check.failed:
                print(f"  - FAIL: {item}")
            print("=" * 78)
    return check.passed, len(check.failed)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    passed, failed = run()
    if failed == 0:
        print("✅ test_llm.py 全部通过")
        return 0
    print("❌ test_llm.py 存在失败用例")
    return 1


if __name__ == "__main__":
    sys.exit(main())

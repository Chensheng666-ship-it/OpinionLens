# -*- coding: utf-8 -*-
"""快手适配器：搜索首屏网页真实 payload（pcursor=""/webPageArea=""）+ 关键词相关过滤。

上游契约见 ``UPSTREAM_CONTRACT``。补丁属「可选增强」：安装失败降级回 MediaCrawler
原搜索逻辑，但必须发 warning 事件并计数（不再只 print 一句了事）。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

from adapters.base import UpstreamSymbol, emit_patch_warning, register_contract

log = logging.getLogger("engine.adapters.platform_kuaishou")

# --------------------------------------------------------------------------------------
# Patch 4c：快手搜索首屏网页真实 payload
# --------------------------------------------------------------------------------------
# 背景：MediaCrawler KuaishouCrawler.search() 传 pcursor=str(page)（首页 "1"），但快手网页
# 真实搜索首屏请求体是 {"keyword","page":"search","webPageArea":"","pcursor":""}；
# 服务端把无效游标 "1" 当推荐流，返回无人机/打工等无关内容。这里首屏改用网页真实 payload，
# 后续页复用响应里的 pcursor/searchSessionId。
KS_SEARCH_WEB_URI = "/rest/v/search/feed"
KS_MAX_NOTES_PER_PAGE = 20
# 关键词相关过滤：去掉这些通用后缀得到“核心词”（示例科技 -> 示例）
_KS_GENERIC_SUFFIXES = (
    "信息技术",
    "有限公司",
    "公司",
    "集团",
    "股份",
    "科技",
    "智能",
    "网络",
    "电子",
    "实业",
    "发展",
    "有限",
    "传媒",
    "文化",
)


def _ks_web_style_enabled() -> bool:
    raw = str(os.environ.get("ENGINE_KS_SEARCH_WEB_STYLE", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _ks_client_class() -> Any:
    from media_platform.kuaishou.client import KuaiShouClient

    return KuaiShouClient


def _ks_relevance_tokens(keyword: Any) -> List[str]:
    """关键词相关词：keyword 本身 + 去掉通用后缀的核心词 + ENGINE_KS_RELEVANCE_EXTRA 追加。"""
    tokens: List[str] = []
    lowered: set = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.lower() not in lowered:
            lowered.add(text.lower())
            tokens.append(text)

    raw = str(keyword or "").strip()
    add(raw)
    core = raw
    changed = True
    while changed and core:
        changed = False
        for suffix in _KS_GENERIC_SUFFIXES:
            if len(core) > len(suffix) and core.lower().endswith(suffix.lower()):
                core = core[: -len(suffix)].strip()
                changed = True
                break
    add(core)
    extra = os.environ.get("ENGINE_KS_RELEVANCE_EXTRA", "")
    for part in re.split(r"[,，;；\s]+", str(extra)):
        add(part)
    return tokens


def _ks_feed_text(feed: Dict[str, Any]) -> str:
    """提取用于相关判定的文本：caption/title/desc + 作者昵称。"""
    if not isinstance(feed, dict):
        return ""
    photo = feed.get("photo") or {}
    author = feed.get("author") or {}
    parts = [
        photo.get("caption") if isinstance(photo, dict) else "",
        photo.get("desc") if isinstance(photo, dict) else "",
        feed.get("title"),
        feed.get("desc"),
        author.get("name") if isinstance(author, dict) else "",
        feed.get("nickname"),
    ]
    return " ".join(str(part) for part in parts if part).strip()


def _ks_feed_matches(feed: Dict[str, Any], tokens: List[str]) -> bool:
    if not tokens:
        return True
    text = _ks_feed_text(feed).lower()
    return any(token.lower() in text for token in tokens)


def _ks_filter_feeds(response: Any, keyword: Any) -> Any:
    """过滤搜索响应里的推荐补位：只保留命中相关词的 feeds（返回给 core 前过滤）。

    过滤后 core（KuaishouCrawler.search）自然只入库相关条目，评论也只抓这些。
    token 为空时（保守）不过滤。
    """
    if not isinstance(response, dict):
        return response
    feeds = response.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        return response
    tokens = _ks_relevance_tokens(keyword)
    if not tokens:
        return response
    kept = [feed for feed in feeds if _ks_feed_matches(feed, tokens)]
    dropped = len(feeds) - len(kept)
    if dropped:
        print(
            f"[engine] ks 过滤推荐补位：in={len(feeds)} out={len(kept)}"
            f"（keyword={str(keyword)!r} tokens={tokens}）",
            flush=True,
        )
    filtered = dict(response)
    filtered["feeds"] = kept
    return filtered


def patch_kuaishou_search(client_cls: Any = None) -> bool:
    """快手搜索修复（幂等）：首屏 pcursor="" + webPageArea=""，后续页复用服务端游标。

    - 仅平台 ks 时安装；ENGINE_KS_SEARCH_WEB_STYLE=0 时跳过（回退 MediaCrawler 旧行为）。
    - 请求仍走 client.request_rest_v2_signed，保持 __NS_hxfalcon 签名。
    - 返回 True 表示已安装/已安装过；False 表示环境变量关闭或安装失败。
    """
    if not _ks_web_style_enabled():
        print("[engine] ENGINE_KS_SEARCH_WEB_STYLE=0：跳过快手网页真实搜索 payload 补丁（回退旧行为）", flush=True)
        return False
    if client_cls is None:
        try:
            client_cls = _ks_client_class()
        except Exception as exc:  # noqa: BLE001 - 可选增强：降级回 MediaCrawler 原搜索
            emit_patch_warning("ks", "patch_kuaishou_search", f"客户端导入失败，跳过补丁：{type(exc).__name__}: {exc}")
            return False
    if getattr(client_cls, "_engine_ks_search_patched", False):
        return True
    try:
        original_search = client_cls.search_info_by_keyword_v2
    except AttributeError as exc:  # noqa: BLE001 - 上游改名/降级：可选增强跳过，但必须可见
        emit_patch_warning("ks", "patch_kuaishou_search", f"上游缺少 search_info_by_keyword_v2：{exc}")
        return False

    async def _wrapped_search(self, keyword, pcursor="", search_session_id=""):
        state = getattr(self, "_engine_ks_search_state", None)
        if not isinstance(state, dict):
            state = {}
            try:
                setattr(self, "_engine_ks_search_state", state)
            except Exception as exc:  # noqa: BLE001 - 实例不允许塞属性：退回单次状态
                log.debug("ks 搜索状态挂载失败：%s: %s", type(exc).__name__, exc)
        kw = str(keyword)
        pcursor_text = str(pcursor if pcursor is not None else "")
        try:
            page_num = int(pcursor_text.strip()) if pcursor_text.strip().isdigit() else None
        except Exception:
            page_num = None
        entry_state = state.get(kw)
        # 首屏：该 keyword 无状态 / 页码 1 / 旧逻辑首页传入 "1"
        if entry_state is None or page_num == 1 or pcursor_text.strip() == "1":
            payload = {"keyword": keyword, "page": "search", "webPageArea": "", "pcursor": ""}
            response = await self.request_rest_v2_signed(KS_SEARCH_WEB_URI, payload)
            raw_feeds = len((response or {}).get("feeds") or [])
            response = _ks_filter_feeds(response, keyword)
            next_cursor = str((response or {}).get("pcursor") or "").strip()
            session = str((response or {}).get("searchSessionId") or "").strip()
            state[kw] = {"pcursor": next_cursor, "searchSessionId": session}
            feeds = (response or {}).get("feeds") or []
            print(
                f"[engine] ks 搜索首屏使用网页真实 payload（pcursor=\"\", webPageArea=\"\"）"
                f"keyword={kw!r} feeds={raw_feeds}->{len(feeds)} next_pcursor={next_cursor[:24]!r}",
                flush=True,
            )
            return response
        cursor = str((entry_state or {}).get("pcursor") or "").strip()
        session = str((entry_state or {}).get("searchSessionId") or search_session_id or "").strip()
        if cursor:
            payload = {"keyword": keyword, "page": "search", "webPageArea": "", "pcursor": cursor}
            if session:
                payload["searchSessionId"] = session
            response = await self.request_rest_v2_signed(KS_SEARCH_WEB_URI, payload)
            raw_feeds = len((response or {}).get("feeds") or [])
            response = _ks_filter_feeds(response, keyword)
            state[kw] = {
                "pcursor": str((response or {}).get("pcursor") or "").strip(),
                "searchSessionId": str((response or {}).get("searchSessionId") or session).strip(),
            }
            print(
                f"[engine] ks 搜索后续页复用服务端游标（pcursor={cursor[:24]!r}，"
                f"searchSessionId={'有' if session else '无'}）feeds={raw_feeds}->{len((response or {}).get('feeds') or [])}",
                flush=True,
            )
            return response
        print("[engine] ks 后续页缺少服务端游标，回退 MediaCrawler 原搜索逻辑", flush=True)
        fallback_response = await original_search(self, keyword, pcursor, search_session_id)
        return _ks_filter_feeds(fallback_response, keyword)

    client_cls.search_info_by_keyword_v2 = _wrapped_search
    client_cls._engine_ks_search_patched = True
    print("[engine] 已安装快手搜索补丁：首屏 pcursor=\"\"+webPageArea=\"\"（ENGINE_KS_SEARCH_WEB_STYLE=0 可回退）", flush=True)
    return True

# --------------------------------------------------------------------------------------
# 上游契约（快手搜索客户端）
# --------------------------------------------------------------------------------------
UPSTREAM_CONTRACT = [
    UpstreamSymbol("ks", "media_platform/kuaishou/client.py", "KuaiShouClient", platforms=("ks",)),
    UpstreamSymbol(
        "ks",
        "media_platform/kuaishou/client.py",
        "KuaiShouClient.search_info_by_keyword_v2",
        platforms=("ks",),
    ),
    UpstreamSymbol(
        "ks",
        "media_platform/kuaishou/client.py",
        "KuaiShouClient.request_rest_v2_signed",
        platforms=("ks",),
    ),
]

register_contract("ks", "快手：搜索补丁与相关过滤（客户端符号）", UPSTREAM_CONTRACT)

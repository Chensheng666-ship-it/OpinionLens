# -*- coding: utf-8 -*-
"""微博适配器：无评论微博不再被当成 ERROR / 反复重试（客户端 request 包装）。

上游契约见 ``UPSTREAM_CONTRACT``。补丁属「可选增强」：客户端缺失/导入失败时降级，
但必须发 warning 事件并计数。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict

from adapters.base import UpstreamSymbol, emit_patch_warning, register_contract

log = logging.getLogger("engine.adapters.platform_weibo")

_WEIBO_COMMENT_EMPTY_PATCH: Dict[str, Any] = {}
WEIBO_NO_COMMENT_MARKERS = (
    "还没有人评论",
    "还没有评论",
    "暂无评论",
    "没有评论",
    "快来抢沙发",
    "评论已关闭",
    "已关闭评论",
)


def _is_weibo_no_comment_message(text: str) -> bool:
    value = str(text or "")
    return any(marker in value for marker in WEIBO_NO_COMMENT_MARKERS)


def patch_weibo_comment_empty() -> bool:
    """微博「这条微博没有评论」不再当成 ERROR/失败（幂等）。

    现象：m.weibo.cn/comments/hotflow 对无评论微博返回
    ``{"ok":0,"msg":"还没有人评论哦~快来抢沙发！"}``。MediaCrawler 把 ok=0
    一律当错误：先 ERROR 日志，再 tenacity 重试 5 次（每次 3s），最后抛
    DataFetchError，采集日志里会反复出现 ERROR，且无谓拖慢采集。

    本 patch 对评论接口做一次请求：识别无评论文案后返回空评论结构
    （max_id=0 让调用方立即结束），并抑制这条预期内的 ERROR 日志；
    其它评论接口错误仍按原逻辑抛出。
    """
    if _WEIBO_COMMENT_EMPTY_PATCH.get("installed"):
        return True
    try:
        client_mod = importlib.import_module("media_platform.weibo.client")
    except Exception as exc:  # noqa: BLE001 - 可选增强：降级回 MediaCrawler 原逻辑
        emit_patch_warning("wb", "patch_weibo_comment_empty", f"导入客户端失败，跳过补丁：{type(exc).__name__}: {exc}")
        return False
    client_cls = getattr(client_mod, "WeiboClient", None)
    if client_cls is None:
        emit_patch_warning("wb", "patch_weibo_comment_empty", "上游缺少 WeiboClient，空评论处理降级为原逻辑")
        return False
    if getattr(client_cls, "_engine_wb_comment_empty_patched", False):
        _WEIBO_COMMENT_EMPTY_PATCH["installed"] = True
        return True

    original_request = client_cls.request
    # tenacity 装饰器可能有多层（async_wrapped -> wrapped_f -> 原始 async 函数）；
    # 一直解到真正的协程函数，避免「补丁里又触发 5 次重试」。
    base_request = original_request
    while getattr(base_request, "__wrapped__", None) is not None:
        base_request = base_request.__wrapped__
    utils_mod = getattr(client_mod, "utils", None)
    data_fetch_error = getattr(client_mod, "DataFetchError", Exception)

    async def _request_with_empty_comment_handling(self, method, url, **kwargs):
        if "/comments/hotflow" not in str(url):
            return await original_request(self, method, url, **kwargs)
        logger = getattr(utils_mod, "logger", None) if utils_mod is not None else None
        original_error = getattr(logger, "error", None) if logger is not None else None

        def _filtered_error(message, *args, **kw):
            # 抑制「还没有人评论」这类正常业务响应，不污染采集日志
            if _is_weibo_no_comment_message(str(message)):
                return
            if original_error is not None:
                original_error(message, *args, **kw)

        if logger is not None and original_error is not None:
            logger.error = _filtered_error
        try:
            return await base_request(self, method, url, **kwargs)
        except data_fetch_error as exc:
            if _is_weibo_no_comment_message(str(exc)):
                print(
                    f"[engine] 微博评论为空（{str(exc)[:80]}），按 0 条处理（不重试、不报错）",
                    flush=True,
                )
                return {"max_id": 0, "max_id_type": 0, "data": []}
            raise
        finally:
            if logger is not None and original_error is not None:
                logger.error = original_error

    client_cls.request = _request_with_empty_comment_handling
    client_cls._engine_wb_comment_empty_patched = True
    _WEIBO_COMMENT_EMPTY_PATCH["installed"] = True
    print("[engine] 已安装微博空评论补丁（无评论不再 ERROR/重试）", flush=True)
    return True

# --------------------------------------------------------------------------------------
# 上游契约（微博客户端）
# --------------------------------------------------------------------------------------
UPSTREAM_CONTRACT = [
    UpstreamSymbol("wb", "media_platform/weibo/client.py", "WeiboClient", platforms=("wb",)),
    UpstreamSymbol("wb", "media_platform/weibo/client.py", "WeiboClient.request", platforms=("wb",)),
    UpstreamSymbol("wb", "media_platform/weibo/client.py", "WeiboClient.get_note_by_keyword", platforms=("wb",)),
    UpstreamSymbol("wb", "media_platform/weibo/client.py", "WeiboClient.get_note_all_comments", platforms=("wb",)),
]

register_contract("wb", "微博：空评论处理与主流程符号", UPSTREAM_CONTRACT)

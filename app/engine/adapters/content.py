# -*- coding: utf-8 -*-
"""内容流水线适配器：数据不脱敏（user_hash 恒等）+ 采集进度日志 -> progress 事件。

``UPSTREAM_CONTRACT`` 覆盖 store 层绑定的 ``tools.user_hash``、配置注入依赖的
``config`` 符号，以及 crawl 模式的主入口/建表函数。
补丁内异常：必需（数据解析 / 配置注入）-> EnginePatchError；
可选（进度日志 Handler）-> warning 事件 + 计数。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional, TYPE_CHECKING

from adapters.base import (
    UpstreamSymbol,
    emit_patch_warning,
    identity_nickname,
    identity_user_id,
    register_contract,
    require_patch,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查（避免与 patches 循环导入）
    from patches import EventEmitter

log = logging.getLogger("engine.adapters.content")

# --------------------------------------------------------------------------------------
# Patch 1：数据不脱敏（产品默认行为：保留 raw user_id / raw nickname，交付前可按部署方策略脱敏）
# --------------------------------------------------------------------------------------
def patch_user_hash(user_hash_module: Any) -> Dict[str, Any]:
    """把脱敏函数换成恒等（store 层数据解析的必需前置）：上游符号缺失即 EnginePatchError。"""
    try:
        user_hash_module.anonymize_user_id = identity_user_id
        user_hash_module.mask_nickname = identity_nickname
    except Exception as exc:  # noqa: BLE001 - 不脱敏是必需能力
        require_patch("all", "patch_user_hash", exc)
    return {
        "anonymize_user_id": identity_user_id,
        "mask_nickname": identity_nickname,
    }

# --------------------------------------------------------------------------------------
# Patch 6：日志 -> progress 事件
# --------------------------------------------------------------------------------------
_RE_KEYWORD = re.compile(r"Current (?:search )?keyword[:：]\s*(.+?)\s*$")
_RE_PAGE = re.compile(r"search .*?keyword[:：]\s*(.+?),\s*page[:：]\s*(\d+)")
_RE_DETAIL = re.compile(r"(get_note_detail_async_task|get_(?:video|aweme|note)_(?:detail|info)_task)")
_RE_COMMENTS = re.compile(r"(get_comments|batch_get_.*comments|get_note_all_comments|get_note_comments)")
_RE_STORE = re.compile(r"\[store\.[a-z]+\.update_")
_RE_FINISHED = re.compile(r"Crawler finished")


class ProgressLogHandler(logging.Handler):
    """把 MediaCrawler 的关键日志行转换为 progress 事件。"""

    def __init__(self, emitter: EventEmitter) -> None:
        super().__init__(level=logging.INFO)
        self.emitter = emitter
        self.stage = "boot"
        self.page = 0
        self.saved = 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception as exc:  # noqa: BLE001 - 日志格式化失败：丢弃这条日志，不影响采集
            log.debug("日志 record 格式化失败：%s", exc)
            return
        try:
            if "comment mode is not enabled" in message:
                return  # 关闭评论时的提示，不应当作 comments 阶段事件
            if _RE_STORE.search(message):
                self.saved += 1
                if self.saved == 1 or self.saved % 5 == 0:
                    self.emitter.progress(self.stage or "search", self.page, self.saved, f"已保存 {self.saved} 条")
                return
            match = _RE_KEYWORD.search(message)
            if match:
                self.stage = "search"
                self.page = 0
                self.emitter.progress("search", 0, self.saved, f"开始搜索：{match.group(1)}")
                return
            match = _RE_PAGE.search(message)
            if match:
                self.stage = "search"
                self.page = int(match.group(2))
                self.emitter.progress("search", self.page, self.saved, f"搜索 {match.group(1)} 第 {self.page} 页")
                return
            if _RE_DETAIL.search(message):
                self.stage = "detail"
                self.emitter.progress("detail", self.page, self.saved, "获取详情")
                return
            if _RE_COMMENTS.search(message):
                self.stage = "comments"
                self.emitter.progress("comments", self.page, self.saved, "获取评论")
                return
            if _RE_FINISHED.search(message):
                self.stage = "done"
                self.emitter.progress("done", self.page, self.saved, "采集流程结束")
        except Exception as exc:  # noqa: BLE001 - 日志 Handler 绝不能影响主流程
            log.debug("progress Handler 处理日志失败：%s: %s", type(exc).__name__, exc)
            return


# 当前挂着的 Handler：同进程重跑时先摘掉旧的，避免同一条日志发两次 progress
_ACTIVE_PROGRESS_HANDLER: Optional["ProgressLogHandler"] = None


def install_progress_handler(mc: Dict[str, Any], emitter: EventEmitter) -> ProgressLogHandler:
    """挂 MediaCrawler 日志 Handler（可选增强）：挂不上也返回可用 handler，调用方不改。

    幂等：先摘掉本模块上一次挂的 Handler（M2 同进程多次跑时不会重复发 progress 事件）。
    """
    global _ACTIVE_PROGRESS_HANDLER
    handler = ProgressLogHandler(emitter)
    try:
        logger = mc["utils"].logger
        previous = _ACTIVE_PROGRESS_HANDLER
        if previous is not None:
            try:
                logger.removeHandler(previous)
            except Exception as exc:  # noqa: BLE001 - 旧 Handler 已不在 logger 上
                log.debug("移除旧 progress Handler 失败：%s: %s", type(exc).__name__, exc)
        logger.addHandler(handler)
        _ACTIVE_PROGRESS_HANDLER = handler
    except Exception as exc:  # noqa: BLE001 - 进度事件是可观测性增强，不阻断采集
        emit_patch_warning(
            getattr(emitter, "platform", "-") or "-",
            "install_progress_handler",
            f"挂载进度日志 Handler 失败，进度事件降级：{type(exc).__name__}: {exc}",
        )
    return handler

# --------------------------------------------------------------------------------------
# 上游契约（不脱敏 / 配置注入 / crawl 主入口）
# --------------------------------------------------------------------------------------
UPSTREAM_CONTRACT = [
    UpstreamSymbol("content", "tools/user_hash.py", "anonymize_user_id"),
    UpstreamSymbol("content", "tools/user_hash.py", "mask_nickname"),
    UpstreamSymbol("content", "tools/utils.py", "logger"),
    UpstreamSymbol("content", "config/__init__.py", "sqlite_db_config"),
    UpstreamSymbol("content", "config/__init__.py", "SQLITE_DB_PATH"),
    UpstreamSymbol("content", "config/__init__.py", "USER_DATA_DIR"),
    UpstreamSymbol("content", "config/__init__.py", "CRAWLER_MAX_NOTES_COUNT"),
    UpstreamSymbol("content", "config/__init__.py", "CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES"),
    UpstreamSymbol("content", "config/__init__.py", "ENABLE_GET_COMMENTS"),
    UpstreamSymbol("content", "config/__init__.py", "SAVE_DATA_OPTION"),
    UpstreamSymbol("content", "config/__init__.py", "HEADLESS"),
    UpstreamSymbol("content", "config/__init__.py", "KEYWORDS"),
    UpstreamSymbol("content", "main.py", "CrawlerFactory", modes=("crawl",)),
    UpstreamSymbol("content", "database/db.py", "init_db", modes=("crawl",)),
]

register_contract("content", "内容流水线：不脱敏 / 配置注入 / crawl 主入口", UPSTREAM_CONTRACT)

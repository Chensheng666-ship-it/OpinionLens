# -*- coding: utf-8 -*-
"""浏览器层适配器：Playwright channel（默认系统 Edge）与 Page.goto 导航重试。

上游契约见 ``UPSTREAM_CONTRACT``（Playwright 符号）；符号缺失由 engine 入口的
``detect_upstream_contract`` 统一报告 ENGINE_INCOMPATIBLE，不在补丁里静默降级。
本模块失败一律可见：可选增强 -> ``emit_patch_warning``（warning 事件 + 计数）。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, TYPE_CHECKING

from adapters.base import UpstreamSymbol, emit_patch_warning, register_contract

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查（避免与 patches 循环导入）
    from patches import EventEmitter

log = logging.getLogger("engine.adapters.browser")

DEFAULT_CHANNEL = "msedge"

_INITIAL_PAGE_REUSE_PATCH: Dict[str, Any] = {}


def install_initial_page_reuse_patch() -> bool:
    """持久化浏览器上下文会自带一个 about:blank 初始页；MediaCrawler 随后又
    ``new_page()`` 一次，导致登录时出现两个页面/标签页。这里把“空白初始页”
    复用为 crawler.context_page，避免多开一个无用页面。

    仅在 context 恰好只有一个空白页时复用；后续业务代码再 new_page（如贴吧
    弹窗页）仍走 Playwright 原逻辑。ENGINE_REUSE_INITIAL_PAGE=0 可关闭。
    """
    if os.environ.get("ENGINE_REUSE_INITIAL_PAGE", "1") == "0":
        return False
    if _INITIAL_PAGE_REUSE_PATCH.get("installed"):
        return True
    try:
        from playwright.async_api import BrowserContext
    except Exception as exc:  # noqa: BLE001 - 离线自测/未装 playwright：可选增强降级但要可见
        emit_patch_warning("-", "install_initial_page_reuse_patch", f"playwright 不可用，跳过初始空白页复用：{type(exc).__name__}: {exc}")
        return False
    original_new_page = BrowserContext.new_page

    async def _new_page_or_reuse_blank(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            pages = list(self.pages)
        except Exception as exc:  # noqa: BLE001 - 上下文已关闭：退回原生 new_page
            log.debug("读取 context.pages 失败：%s: %s", type(exc).__name__, exc)
            pages = []
        if len(pages) == 1:
            page = pages[0]
            try:
                is_blank = str(page.url) in ("about:blank", "", "chrome://newtab/")
            except Exception as exc:  # noqa: BLE001 - 页面正要关闭：按非空白处理
                log.debug("读取 page.url 判断空白页失败：%s: %s", type(exc).__name__, exc)
                is_blank = False
            if is_blank and not getattr(page, "_engine_reused_blank_page", False):
                page._engine_reused_blank_page = True  # type: ignore[attr-defined]
                print("[engine] 复用持久化上下文初始空白页，避免登录多开页面", flush=True)
                return page
        return await original_new_page(self, *args, **kwargs)

    BrowserContext.new_page = _new_page_or_reuse_blank  # type: ignore[assignment]
    _INITIAL_PAGE_REUSE_PATCH["installed"] = True
    return True

# --------------------------------------------------------------------------------------
# Patch 3：Playwright 浏览器 channel（默认系统 Edge，免装 Playwright Chromium）
# --------------------------------------------------------------------------------------
_BROWSER_PATCH: Dict[str, Any] = {}


def patch_browser_channel(channel: Optional[str] = None) -> Dict[str, Any]:
    """把 BrowserType.launch / launch_persistent_context 的 channel 强制为指定值。

    - channel 为 None 时取环境变量 BROWSER_CHANNEL，再回退 DEFAULT_CHANNEL(msedge)
    - channel 为空串 / "none"：不 patch（允许走 Playwright 默认 Chromium）
    - 对 MediaCrawler 各平台显式传入的 channel="chrome" 同样覆盖为 Edge，
      避免目标机未装 Chrome 时启动失败（可用环境变量改回 chrome）
    """
    raw = channel if channel is not None else os.environ.get("BROWSER_CHANNEL", DEFAULT_CHANNEL)
    channel = (raw or "").strip()
    if channel.lower() in ("", "none", "default"):
        return {"channel": None, "patched": False}
    if _BROWSER_PATCH.get("channel") == channel:
        return {"channel": channel, "patched": True}

    try:
        from playwright.async_api import BrowserType
    except Exception as exc:  # noqa: BLE001 - 无 playwright 时按未打补丁处理（可选增强）
        emit_patch_warning("-", "patch_browser_channel", f"playwright 不可用，无法注入 channel={channel}：{type(exc).__name__}: {exc}")
        return {"channel": None, "patched": False}

    if "orig_launch_persistent_context" not in _BROWSER_PATCH:
        _BROWSER_PATCH["orig_launch_persistent_context"] = BrowserType.launch_persistent_context
        _BROWSER_PATCH["orig_launch"] = BrowserType.launch

    async def _launch_persistent_context(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["channel"] = channel
        return await _BROWSER_PATCH["orig_launch_persistent_context"](self, *args, **kwargs)

    async def _launch(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["channel"] = channel
        return await _BROWSER_PATCH["orig_launch"](self, *args, **kwargs)

    BrowserType.launch_persistent_context = _launch_persistent_context
    BrowserType.launch = _launch
    _BROWSER_PATCH["channel"] = channel
    return {"channel": channel, "patched": True}


# --------------------------------------------------------------------------------------
# Patch 3b：Page.goto 导航重试（修复微博 passport 页 ERR_CONNECTION_CLOSED/RESET 抖动）
# --------------------------------------------------------------------------------------
_PAGE_GOTO_PATCH: Dict[str, Any] = {}

GOTO_RETRYABLE_ERRORS = (
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_ABORTED",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "ERR_ABORTED",
    "ERR_FAILED",
    "ERR_NETWORK_CHANGED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_HTTP2",
    "net::ERR_",
    "Timeout",
    "timed out",
)


def install_goto_retry(emitter: Optional[EventEmitter] = None, attempts: int = 5, base_delay: float = 1.0) -> Dict[str, Any]:
    """包装 Page.goto：对网络类错误自动重试（默认 1 次初始 + 4 次重试，1~2s 退避）。

    attempts 为总尝试次数（=重试次数+1）。重试过程写原始日志（sidecar 可见）并尽量发 progress 事件。
    """
    if _PAGE_GOTO_PATCH.get("installed"):
        return {"installed": True, "attempts": _PAGE_GOTO_PATCH.get("attempts", attempts)}

    try:
        from playwright.async_api import Page
    except Exception as exc:  # noqa: BLE001 - 无 playwright 时导航重试不可用（可选增强）
        emit_patch_warning("-", "install_goto_retry", f"playwright 不可用，跳过 Page.goto 重试：{type(exc).__name__}: {exc}")
        return {"installed": False, "attempts": int(attempts)}

    _PAGE_GOTO_PATCH.update(installed=True, attempts=int(attempts), original=Page.goto)

    async def _goto_with_retry(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        import asyncio

        # 每次调用现取当前会话的 emitter：同进程重跑时不会把 progress 发到上一次的 emitter
        def _current_emitter() -> Optional[Any]:
            from adapters.session import current_session

            active = current_session().emitter
            return active if active is not None else emitter

        # 单次导航超时上限（默认 15s）：避免一次慢连接吃光整个登录等待预算，尽快进入重试
        if "timeout" not in kwargs:
            try:
                kwargs["timeout"] = int(os.environ.get("ENGINE_GOTO_TIMEOUT_MS", "15000"))
            except (TypeError, ValueError):
                kwargs["timeout"] = 15000
        last_exc: Optional[BaseException] = None
        total = max(1, int(_PAGE_GOTO_PATCH.get("attempts") or attempts))
        for attempt in range(1, total + 1):
            try:
                result = await _PAGE_GOTO_PATCH["original"](self, url, *args, **kwargs)
                if attempt > 1:
                    line = f"[engine] Page.goto 第 {attempt} 次尝试成功：{str(url)[:140]}"
                    print(line, flush=True)
                    active = _current_emitter()
                    if active is not None:
                        active.progress("login", 0, 0, f"导航重试成功（第 {attempt} 次）：{str(url)[:100]}")
                return result
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                text = str(exc)
                retryable = any(key in text for key in GOTO_RETRYABLE_ERRORS)
                if attempt >= total or not retryable:
                    raise
                delay = 0.0 if base_delay <= 0 else min(2.0, max(1.0, base_delay * attempt))
                first_line = (text.splitlines() or [text])[0][:180]
                line = (
                    f"[engine] Page.goto 失败（{attempt}/{total}），{delay:.1f}s 后重试："
                    f"{str(url)[:140]} | {first_line}"
                )
                print(line, flush=True)
                active = _current_emitter()
                if active is not None:
                    active.progress("login", 0, 0, f"导航失败，{delay:.1f}s 后重试（{attempt}/{total}）：{first_line[:100]}")
                await asyncio.sleep(delay)
        if last_exc is not None:  # pragma: no cover - 循环必然 return/raise
            raise last_exc
        return None

    Page.goto = _goto_with_retry  # type: ignore[assignment]
    return {"installed": True, "attempts": int(attempts)}

# --------------------------------------------------------------------------------------
# 上游契约（Playwright：channel 注入 + 导航重试依赖这些符号）
# --------------------------------------------------------------------------------------
UPSTREAM_CONTRACT = [
    UpstreamSymbol("browser", "playwright/async_api/__init__.py", "BrowserType.launch"),
    UpstreamSymbol("browser", "playwright/async_api/__init__.py", "BrowserType.launch_persistent_context"),
    UpstreamSymbol("browser", "playwright/async_api/__init__.py", "BrowserContext.new_page"),
    UpstreamSymbol("browser", "playwright/async_api/__init__.py", "Page.goto"),
]

register_contract("browser", "浏览器通道 / 导航重试（默认系统 Edge，免装 Playwright Chromium）", UPSTREAM_CONTRACT)

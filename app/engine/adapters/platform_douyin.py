# -*- coding: utf-8 -*-
"""抖音适配器：登录面板稳健点击（短超时 + force + ESC，避免被浮层拦截卡 30s）。

上游契约见 ``UPSTREAM_CONTRACT``。登录面板属登录必需能力：上游 ``popup_login_dialog``
缺失时抛 EnginePatchError（error 事件 + 出口层 ok:false），不静默继续。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from adapters.base import UpstreamSymbol, register_contract, require_patch
from adapters.login import _login_class
from adapters.session import ensure_session, remember_originals, restore_originals

if TYPE_CHECKING:
    # 仅注解使用（patches.EventEmitter 在运行期由 patches.py 注入，避免循环 import）
    from patches import EventEmitter

log = logging.getLogger("engine.adapters.platform_douyin")

# --------------------------------------------------------------------------------------
# Patch 5b：抖音登录按钮点击稳健化（避免被 login-full-panel 浮层拦截卡 30s）
# --------------------------------------------------------------------------------------
_DY_LOGIN_PANEL_SELECTORS = [
    "div#login-panel-new",
    "div[id^='login-full-panel']",
    "div[id*='login-full-panel']",
    "div#animate_qrcode_container",
    "div[class*='login-panel']",
]
_DY_LOGIN_BUTTON_SELECTORS = ["//p[text()='登录']", "text=登录"]


async def _any_visible(page: Any, selectors: List[str]) -> bool:
    if page is None:
        return False
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _wait_any_visible(page: Any, selectors: List[str], timeout_ms: int) -> bool:
    import asyncio

    deadline = time.monotonic() + max(0.1, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await _any_visible(page, selectors):
            return True
        await asyncio.sleep(0.3)
    return False


async def _robust_douyin_popup_login_dialog(self: Any, initial_wait_ms: int = 6000) -> None:
    """替代 DouYinLogin.popup_login_dialog：

    - 已有登录面板（login-panel-new / login-full-panel）→ 直接返回，不重复点击；
    - 未弹出 → 短超时(3s/2s)+force 点击“登录”，失败时 ESC 关遮罩后重试；
    - 仍失败不抛 30s TimeoutError，快速返回让后续二维码兜底/重载重试处理。
    """
    import asyncio

    page = getattr(self, "context_page", None)
    if page is None:
        return
    if await _wait_any_visible(page, _DY_LOGIN_PANEL_SELECTORS, initial_wait_ms):
        print("[engine] dy 登录面板已打开，跳过点击登录", flush=True)
        return
    for attempt in range(1, 4):
        try:
            await page.keyboard.press("Escape")
        except Exception as exc:  # noqa: BLE001 - ESC 只是关遮罩的尽力而为动作
            log.debug("dy 按 ESC 关闭遮罩失败：%s: %s", type(exc).__name__, exc)
        await asyncio.sleep(0.3)
        clicked = False
        for selector in _DY_LOGIN_BUTTON_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                try:
                    await locator.click(timeout=3000)
                    clicked = True
                except Exception as exc:  # noqa: BLE001 - 常速点击被浮层拦截：降级 force 点击
                    log.debug("dy 登录按钮常速点击失败（%s）：%s: %s", selector, type(exc).__name__, exc)
                    try:
                        await locator.click(timeout=2000, force=True)
                        clicked = True
                    except Exception as exc2:  # noqa: BLE001 - force 也失败：降级 JS 点击
                        log.debug("dy 登录按钮 force 点击失败（%s）：%s: %s", selector, type(exc2).__name__, exc2)
                        try:
                            await page.evaluate(
                                """(sel) => {
                                  const els = [...document.querySelectorAll('p,div,span,button')];
                                  const el = els.find(e => (e.innerText || '').trim() === '登录');
                                  if (el) { el.click(); return true; }
                                  return false;
                                }""",
                                selector,
                            )
                            clicked = True
                        except Exception as exc:  # noqa: BLE001 - JS 点击也失败：记为未点到，继续下一轮
                            log.debug("dy JS 点击登录失败：%s: %s", type(exc).__name__, exc)
            except Exception as exc:  # noqa: BLE001 - 选择器不可用：试下一个候选
                log.debug("dy 登录按钮选择器处理失败（%s）：%s: %s", selector, type(exc).__name__, exc)
                continue
            if clicked:
                break
        print(f"[engine] dy 尝试打开登录面板（第 {attempt}/3 次，clicked={clicked}）", flush=True)
        if await _wait_any_visible(page, _DY_LOGIN_PANEL_SELECTORS, 3000):
            return
        await asyncio.sleep(0.5)
    print("[engine] dy 登录面板仍未弹出（短超时+force+ESC 兜底后），继续尝试二维码兜底", flush=True)


def patch_douyin_login_dialog(mc: Dict[str, Any], emitter: Optional[EventEmitter] = None) -> None:
    """安装抖音登录面板稳健点击补丁（幂等：按 PatchSession 记，新一次运行可重装）。"""
    session = ensure_session()
    marker = "patch_douyin_login_dialog:dy"
    if session.is_installed(marker):
        return
    try:
        cls = _login_class("dy")
        remember_originals(cls, ("popup_login_dialog",))
        restore_originals(cls, ("popup_login_dialog",))
        if not callable(getattr(cls, "popup_login_dialog", None)):
            raise AttributeError(f"{cls.__name__} 缺少 popup_login_dialog()")
        cls.popup_login_dialog = _robust_douyin_popup_login_dialog
        cls._engine_dy_popup_patched = True  # 兼容旧标记（跨模块可读）
    except Exception as exc:  # noqa: BLE001 - 登录面板是登录必需能力
        require_patch("dy", "patch_douyin_login_dialog", exc, emitter=emitter)
        raise  # pragma: no cover - require_patch 必然抛出
    session.installed.add(marker)
    print("[engine] 已安装抖音登录按钮稳健点击补丁（短超时+force+ESC，不再卡 30s）", flush=True)

# --------------------------------------------------------------------------------------
# 上游契约（抖音登录面板 / 客户端）
# begin / check_login_state 由 login.py 遍历 LOGIN_CLASSES 自动生成，这里只留抖音特有符号。
# --------------------------------------------------------------------------------------
UPSTREAM_CONTRACT = [
    UpstreamSymbol(
        "dy",
        "media_platform/douyin/login.py",
        "DouYinLogin.popup_login_dialog",
        platforms=("dy",),
        modes=("login",),
    ),
    UpstreamSymbol("dy", "media_platform/douyin/client.py", "DouYinClient", platforms=("dy",)),
    UpstreamSymbol(
        "dy",
        "media_platform/douyin/client.py",
        "DouYinClient.search_info_by_keyword",
        platforms=("dy",),
    ),
    UpstreamSymbol("dy", "media_platform/douyin/client.py", "DouYinClient.get_aweme_comments", platforms=("dy",)),
]

register_contract("dy", "抖音：登录面板稳健点击 + 客户端搜索/评论符号", UPSTREAM_CONTRACT)

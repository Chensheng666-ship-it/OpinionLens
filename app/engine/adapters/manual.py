# -*- coding: utf-8 -*-
"""验证页检测 / 人工接管适配器（短信验证码 / 滑块 / 拼图 / 安全验证）。

M1.6 从 ``adapters/login.py`` 纯搬迁（函数体一行未改）。本模块不依赖 qr / login：
页面文本、标题、URL 判定 + ``ManualGate`` 去重逻辑都在这里，便于离线单测。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from patches import EventEmitter

log = logging.getLogger("engine.adapters.manual")

# ---- 人工接管 / 验证页检测 ---------------------------------------------------------
# 以“可见文本/标题/URL”判断是否处于平台安全验证页（产品策略：宁可多提示不要卡死）
# 短信验证只在“扫码之后、二维码/登录弹窗已切换”才算二次验证；短语必须是平台短信验证弹层专属。
# 不要包含「短信验证登录」「获取验证码」「验证码登录」等普通登录页 tab/表单文案。
SMS_PHRASES = (
    "短信验证码",
    "短信已发送",
    "验证码已发送",
    "验证码已发送至",
    "请填写短信验证码",
    "请输入短信验证码",
    "请填写验证码",
    "发送至手机",
    "短信发送成功",
    "为保障账号安全",
)
SLIDER_PHRASES = (
    "验证码中间页",
    "安全验证",
    "请通过验证",
    "请完成下方验证",
    "请向右滑动",
    "拖动滑块",
    "向右滑动完成拼图",
    "拖动滑块完成拼图",
    "滑块验证",
    "拼图",
    "人机验证",
    "请完成下列验证",
    "完成验证",
    "按住左边",
    "点击按钮进行验证",
    "请完成安全验证",
    "验证失败，请重试",
)
SLIDER_TITLE_HINTS = ("验证码中间页", "安全验证", "请通过验证", "滑块", "身份验证")
VERIFY_URL_HINTS = ("verify", "captcha", "security", "safe_verify", "antispam")
MANUAL_KIND_LABELS = {"sms": "短信验证码", "slider": "滑块/拼图安全验证", "captcha": "安全验证"}
MANUAL_WAIT_SECONDS = 240.0  # 人工接管最长等待（任务要求 240s）

# --------------------------------------------------------------------------------------
# Patch 2b：登录验证页检测 + 人工接管等待（短信验证码 / 滑块 / 拼图 / 安全验证）
# --------------------------------------------------------------------------------------
async def _safe_page_title(page: Any) -> str:
    try:
        return str(await page.title() or "")
    except Exception as exc:  # noqa: BLE001 - 页面已关闭/跳转中，取不到标题不是补丁失败
        log.debug("读取页面标题失败：%s: %s", type(exc).__name__, exc)
        return ""


async def _safe_page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception as exc:  # noqa: BLE001 - 同上：页面不可读时按空 URL 处理
        log.debug("读取页面 URL 失败：%s: %s", type(exc).__name__, exc)
        return ""


async def _safe_page_inner_text(page: Any, limit: int = 20000) -> str:
    """取页面可见文本：innerText 不包含 display:none 的隐藏登录表单，避免误判。"""
    if page is None:
        return ""
    try:
        text = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)")
        return str(text or "")[:limit]
    except Exception as exc:  # noqa: BLE001 - 页面切换瞬间取不到 innerText，退回 content()
        log.debug("读取页面 innerText 失败，退回 content()：%s: %s", type(exc).__name__, exc)
        try:
            return str(await page.content() or "")[:limit]
        except Exception as exc2:  # noqa: BLE001 - 页面已关闭：空文本交给状态机兜底
            log.debug("读取页面 content 失败：%s: %s", type(exc2).__name__, exc2)
            return ""


def _manual_message(kind: str) -> str:
    label = MANUAL_KIND_LABELS.get(kind, "安全验证")
    if kind == "sms":
        action = "输入手机收到的短信验证码"
    elif kind == "slider":
        action = "拖动滑块/完成拼图"
    else:
        action = "按页面提示完成验证"
    return (
        f"[kind={kind}] 检测到{label}：请在弹出的浏览器窗口完成验证（{action}），"
        "完成后会自动继续登录；如窗口已关闭，请重新发起登录"
    )


async def _has_visible_sms_input(page: Any) -> bool:
    """已废弃：普通登录页也可能展示可见短信验证码输入框，不能作为二次验证依据。"""
    return False


async def detect_verification(page: Any) -> Optional[Dict[str, Any]]:
    """检测当前页面是否处于平台二次验证页。返回 {kind, reason, title, url} 或 None。

    kind: sms（短信验证码）/ slider（滑块/拼图）/ captcha（其他安全验证）
    判定依据仅限：标题 + URL + 可见文本（innerText）；不使用输入框 placeholder / “验证码登录” tab。
    短信(kind=sms)是否真正触发 need_manual 还要满足“二维码已消失/登录弹窗已切换”，由上层闸门判断。
    """
    if page is None:
        return None
    title = await _safe_page_title(page)
    url = await _safe_page_url(page)
    text = await _safe_page_inner_text(page)
    blob = f"{title}\n{text}"
    low_url = url.lower()

    for phrase in SMS_PHRASES:
        if phrase in blob:
            return {"kind": "sms", "reason": phrase, "title": title[:160], "url": url[:300]}

    reason = ""
    for phrase in SLIDER_PHRASES:
        if phrase in blob:
            reason = phrase
            break
    title_hit = next((hint for hint in SLIDER_TITLE_HINTS if hint in title), "")
    url_hit = next((hint for hint in VERIFY_URL_HINTS if hint in low_url), "")
    if reason or title_hit or url_hit:
        is_slider = reason in ("拖动滑块", "拼图", "滑块验证", "按住左边", "点击按钮进行验证") or title_hit
        return {
            "kind": "slider" if is_slider else "captcha",
            "reason": reason or title_hit or f"url:{url_hit}",
            "title": title[:160],
            "url": url[:300],
        }
    return None


class ManualGate:
    """人工接管闸门：检测到验证页 → 发 login.status need_manual(kind=...)；验证结束后继续。

    同一 kind 在 20s 内不重复发 need_manual；验证结束会发一次 waiting_scan 提示“继续登录”。
    """

    def __init__(
        self,
        emitter: EventEmitter,
        platform: str,
        timeout: float = MANUAL_WAIT_SECONDS,
    ) -> None:
        self.emitter = emitter
        self.platform = platform
        self.timeout = float(timeout)
        self.active_kind: Optional[str] = None
        self._last_emit_ts = 0.0
        self._lock = threading.Lock()
        self.manual_events = 0

    def report(self, info: Optional[Dict[str, Any]]) -> bool:
        """报告一次检测结果；返回是否新发出 need_manual。"""
        emitted = False
        with self._lock:
            if info is None:
                if self.active_kind:
                    self.active_kind = None
                    self._last_emit_ts = 0.0
                    self.emitter.status("waiting_scan", "验证已完成，继续登录…")
                return False
            kind = str(info.get("kind") or "captcha")
            now = time.monotonic()
            if kind != self.active_kind or (now - self._last_emit_ts) >= 20.0:
                self.active_kind = kind
                self._last_emit_ts = now
                self.manual_events += 1
                print(
                    f"[engine] 检测到验证页（platform={self.platform}, kind={kind}, reason={info.get('reason', '')!r}）"
                    "，等待用户在浏览器窗口完成…",
                    flush=True,
                )
                self.emitter.status("need_manual", _manual_message(kind), kind=kind)
                emitted = True
        return emitted

    async def report_async(self, info: Optional[Dict[str, Any]], page: Any = None) -> bool:
        """report 的异步版：首次发 need_manual 时把登录浏览器窗口置前（用户实测需要）。"""
        emitted = self.report(info)
        if emitted and page is not None:
            try:
                if hasattr(page, "bring_to_front"):
                    await page.bring_to_front()
                    print(f"[engine] 已将登录浏览器窗口置前（platform={self.platform}），请在窗口内完成验证", flush=True)
            except Exception as exc:  # noqa: BLE001 - 置前失败不影响 need_manual 已发出
                log.debug("登录窗口置前失败：%s: %s", type(exc).__name__, exc)
        return emitted

    async def wait_clear(
        self,
        page: Any,
        timeout: Optional[float] = None,
        poll: float = 2.0,
    ) -> bool:
        """轮询等待验证页消失；返回 True=已无验证（或从未出现），False=超时仍在验证。"""
        import asyncio

        wait_timeout = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.2, wait_timeout)
        saw_verification = False
        while time.monotonic() < deadline:
            info = await detect_verification(page)
            if info:
                saw_verification = True
                await self.report_async(info, page)
                await asyncio.sleep(poll)
                continue
            if not saw_verification:
                self.report(None)
                return True
            # 已从验证页恢复：再确认一次，避免页面跳转瞬间误判
            await asyncio.sleep(min(poll, 1.0))
            if await detect_verification(page):
                continue
            self.report(None)
            return True
        print(
            f"[engine] 人工验证等待超时（{wait_timeout:.0f}s，platform={self.platform}，kind={self.active_kind}）",
            flush=True,
        )
        return False


async def wait_manual_before_begin(
    page: Any,
    emitter: EventEmitter,
    platform: str,
    manual: Optional[ManualGate],
    poll: float = 2.0,
) -> bool:
    """登录流程开始前：若页面已是验证页（dy/tieba 整页滑块、安全验证），先等用户完成。"""
    if page is None or manual is None:
        return True
    info = await detect_verification(page)
    if info is None:
        return True
    if str(info.get("kind")) == "sms":
        # 登录前不因普通手机号登录表单（含“获取验证码”）触发人工等待；短信二次验证只在出码后处理
        return True
    print(f"[engine] {platform} 登录前检测到验证页（kind={info.get('kind')}），等待人工处理后继续登录", flush=True)
    return await manual.wait_clear(page, poll=poll)


__all__ = [
    "SMS_PHRASES",
    "SLIDER_PHRASES",
    "SLIDER_TITLE_HINTS",
    "VERIFY_URL_HINTS",
    "MANUAL_KIND_LABELS",
    "MANUAL_WAIT_SECONDS",
    "detect_verification",
    "ManualGate",
    "wait_manual_before_begin",
    "_manual_message",
    "_safe_page_title",
    "_safe_page_url",
    "_safe_page_inner_text",
    "_has_visible_sms_input",
]

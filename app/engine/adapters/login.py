# -*- coding: utf-8 -*-
"""登录补丁边界：安装 / 拦截 / 仪器化（M1.6 后只留补丁入口与登录页观察）。

M1.6 按职责拆出（纯搬迁，函数体未改）：
* ``adapters/qr.py``      二维码校验 / 截获 / 兜底提取
* ``adapters/manual.py``  验证页检测 / 人工接管
* ``adapters/network.py`` 网络异常捕获（全局 httpx 补丁）

本模块保留：``LOGIN_CLASSES`` / ``_login_class``（登录类契约自动生成）、
``install_qr_fallback`` / ``patch_login_only`` / ``patch_login_blocked`` /
``instrument_login_class`` 四个补丁边界，以及 ``should_trigger_sms`` /
``watch_login_page`` / ``close_crawler``。运行期状态全部走 ``PatchSession``。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

from adapters.base import (
    AlreadyLoggedIn,
    LoginOnlyDone,
    NetworkTransient,
    NotLoggedIn,
    UpstreamSymbol,
    register_contract,
    require_patch,
)
from adapters.manual import ManualGate, _manual_message, detect_verification, wait_manual_before_begin
from adapters.network import _recent_network_exception
from adapters.qr import (
    DEFAULT_QR_FALLBACK_SELECTORS,
    QR_FALLBACK_SELECTORS,
    _qr_visible_for_platform,
    _run_qr_rounds,
    _warn_once,
)
from adapters.session import ensure_session, remember_originals, restore_originals, session_lock

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查（避免与 patches 循环导入）
    from patches import EventEmitter

log = logging.getLogger("engine.adapters.login")

# platform -> (login 模块, Login 类名)。登录类契约（begin / check_login_state）由本表
# **自动生成**，7 个平台一份来源：既不再在各 platform_*.py 手写重复，也不会漏声明
# （M1 review：ks / wb 曾漏声明，上游改名时探测发现不了）。
LOGIN_CLASSES: Dict[str, Tuple[str, str]] = {
    "xhs": ("media_platform.xhs.login", "XiaoHongShuLogin"),
    "dy": ("media_platform.douyin.login", "DouYinLogin"),
    "ks": ("media_platform.kuaishou.login", "KuaishouLogin"),
    "bili": ("media_platform.bilibili.login", "BilibiliLogin"),
    "wb": ("media_platform.weibo.login", "WeiboLogin"),
    "tieba": ("media_platform.tieba.login", "BaiduTieBaLogin"),
    "zhihu": ("media_platform.zhihu.login", "ZhiHuLogin"),
}


def _login_class(platform: str) -> Any:
    """按平台取上游 Login 类（导入失败直接抛，由调用方转 EnginePatchError）。"""
    module_name, class_name = LOGIN_CLASSES[platform]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)



def install_qr_fallback(
    mc: Dict[str, Any],
    emitter: Optional[EventEmitter],
    platform: str,
    manual: Optional[ManualGate] = None,
    keepalive: bool = False,
) -> Dict[str, Any]:
    """包装 find_login_qrcode / find_qrcode_img_from_canvas：验证页等待 + 原选择器 + 平台兜底。

    幂等按 :class:`PatchSession` 记（新一次运行会重新包装，闭包拿到新 emitter）；
    重新包装前先 ``restore_originals`` 回到上游实现，避免「包装器套包装器」。
    """
    session = ensure_session()
    marker = f"install_qr_fallback:{platform}"
    if session.is_installed(marker):
        return {"installed": True, "selectors": QR_FALLBACK_SELECTORS.get(platform, DEFAULT_QR_FALLBACK_SELECTORS)}
    try:
        utils = mc["utils"]
        crawler_util = mc["crawler_util"]
        remember_originals(utils, ("find_login_qrcode", "find_qrcode_img_from_canvas"))
        restore_originals(utils, ("find_login_qrcode", "find_qrcode_img_from_canvas"))
        original_find = utils.find_login_qrcode
        original_canvas = utils.find_qrcode_img_from_canvas
    except Exception as exc:  # noqa: BLE001 - 二维码提取是登录必需能力
        require_patch(platform, "install_qr_fallback", exc, emitter=emitter)
        raise  # pragma: no cover - require_patch 必然抛出

    with session_lock():
        session.state["qr_keepalive"] = bool(keepalive)
        session.state["qr_platform"] = platform

    def _keepalive_state(default: bool) -> bool:
        return bool(session.state.get("qr_keepalive", default))

    async def _find_with_fallback(page: Any, selector: Any = None, **kwargs) -> str:
        sel = selector if selector is not None else kwargs.get("selector")
        sel = str(sel or "")
        print(f"[engine] find_login_qrcode 包装器进入（platform={platform}, selector={sel[:140]}）", flush=True)
        return await _run_qr_rounds(
            page, platform, [sel] if sel else [], emitter, manual, keepalive=_keepalive_state(keepalive)
        )

    async def _find_canvas_with_fallback(page: Any, canvas_selector: Any = None, **kwargs) -> str:
        sel = canvas_selector if canvas_selector is not None else kwargs.get("canvas_selector")
        sel = str(sel or "")
        print(f"[engine] find_qrcode_img_from_canvas 包装器进入（platform={platform}, selector={sel[:140]}）", flush=True)
        return await _run_qr_rounds(
            page, platform, [sel] if sel else [], emitter, manual, keepalive=_keepalive_state(keepalive)
        )

    _ = (original_find, original_canvas)  # 仅作为"已记住基线"的显式说明（wrapper 走兜底流程）
    utils.find_login_qrcode = _find_with_fallback
    utils.find_qrcode_img_from_canvas = _find_canvas_with_fallback
    crawler_util.find_login_qrcode = _find_with_fallback
    crawler_util.find_qrcode_img_from_canvas = _find_canvas_with_fallback
    utils._engine_qr_fallback_installed = True  # 兼容旧标记（跨模块可读）
    session.installed.add(marker)
    return {"installed": True, "selectors": QR_FALLBACK_SELECTORS.get(platform, DEFAULT_QR_FALLBACK_SELECTORS)}

# --------------------------------------------------------------------------------------
# Patch 5：login 模式登录成功即停 / crawl 模式未登录快速失败
# --------------------------------------------------------------------------------------
def patch_login_only(crawler: Any, state: Optional[Dict[str, Any]] = None) -> None:
    """把实例的 search 替换为中止异常（login 模式不采集）。

    - state["login_attempted"] 为 True（确实进入过 login.begin 扫码）→ LoginOnlyDone
      （扫码成功由 instrument_login_class 的 check_login_state 包装发 confirmed）
    - 否则说明 crawler.start() 的 pong 已判定登录态有效（跳过 login.begin）→ AlreadyLoggedIn
    这样"已登录，无需扫码"与"扫码成功"两条路径可区分，UI 不会误报“二维码截图失败”。
    """
    if not callable(getattr(crawler, "search", None)):
        require_patch("all", "patch_login_only", "上游 crawler 缺少 search()：login 模式无法在登录成功后中止采集")
    login_attempted = bool(state and state.get("login_attempted"))

    async def _search_stop(*_args, **_kwargs):
        if login_attempted:
            raise LoginOnlyDone()
        raise AlreadyLoggedIn()

    crawler.search = _search_stop  # 实例属性，调用时不绑定 self




def patch_login_blocked(mc: Dict[str, Any], platform: str) -> None:
    """crawl 模式：需要登录时直接抛 NotLoggedIn，绝不拉起扫码。

    若刚刚发生过网络异常（平台 pong 超时等），改抛 NetworkTransient，
    避免把网络抖动误报成“未登录”，让 sidecar 按网络错误自动重试。

    幂等按 PatchSession 记（同进程可重装），重装前先把 ``begin`` 恢复到基线。
    """
    session = ensure_session()
    marker = f"patch_login_blocked:{platform}"
    if session.is_installed(marker):
        return

    try:
        cls = _login_class(platform)
        originals = remember_originals(cls, ("begin",))
        restore_originals(cls, ("begin",))
        original_begin = originals["begin"]
        if not callable(original_begin):
            raise AttributeError(f"{cls.__name__} 缺少 begin()")
    except Exception as exc:  # noqa: BLE001 - crawl 模式「未登录快速失败」是必需能力
        require_patch(platform, "patch_login_blocked", exc)
        raise  # pragma: no cover - require_patch 必然抛出

    async def _blocked_begin(self, *_args, **_kwargs):
        network_error = _recent_network_exception()
        if network_error:
            raise NetworkTransient(f"平台网络异常，自动重试：{network_error}")
        raise NotLoggedIn()

    cls.begin = _blocked_begin
    cls._engine_login_blocked = True  # 兼容旧标记（跨模块可读）
    session.installed.add(marker)

def instrument_login_class(
    mc: Dict[str, Any],
    emitter: EventEmitter,
    platform: str,
    state: Optional[Dict[str, Any]] = None,
    manual: Optional[ManualGate] = None,
) -> None:
    """login 模式仪器化登录类：

    - 包装 begin()：先等待整页验证（dy 验证码中间页 / tieba 百度安全验证）由用户完成 → 再走原登录流程；
      并标记 state["login_attempted"]=True（区分“需要扫码”与“已登录跳过登录”）
    - 包装 check_login_state()：扫码登录成功时发 login.status confirmed，并标记 state["confirmed"]

    幂等：同一 PatchSession 内只包装一次（重复调用不再套一层包装器，否则事件翻倍）；
    新一次运行（新 session）会先 ``restore_originals`` 回到基线再包装，闭包拿到当次 emitter。
    """
    session = ensure_session()
    marker = f"instrument_login_class:{platform}"
    if session.is_installed(marker):
        return
    try:
        cls = _login_class(platform)
        originals = remember_originals(cls, ("begin", "check_login_state"))
        original_check = originals["check_login_state"]
        original_begin = originals["begin"]
        restore_originals(cls, ("begin", "check_login_state"))
        if not callable(original_check) or not callable(original_begin):
            raise AttributeError(f"{cls.__name__} 缺少 begin()/check_login_state()")
    except Exception as exc:  # noqa: BLE001 - 登录仪器化（confirmed 事件）是必需能力
        require_patch(platform, "instrument_login_class", exc, emitter=emitter)
        raise  # pragma: no cover - require_patch 必然抛出

    # 当次运行的上下文进 session：下一次运行重装时读到的就是新对象，不会用到旧 emitter/state
    if state is not None:
        session.state["login_state"] = state
    if manual is not None:
        session.manual = manual
    session.emitter = session.emitter if session.emitter is not None else emitter

    def _active_state() -> Optional[Dict[str, Any]]:
        return session.state.get("login_state") or state

    def _active_emitter() -> Any:
        return session.emitter or emitter

    async def _wrapped_check(self, *args, **kwargs):
        ok = await original_check(self, *args, **kwargs)
        if ok:
            active_state = _active_state()
            if active_state is not None:
                active_state["confirmed"] = True
            _active_emitter().status("confirmed", "登录成功")
        return ok

    async def _wrapped_begin(self, *args, **kwargs):
        import asyncio

        active_state = _active_state()
        if active_state is not None:
            active_state["login_attempted"] = True
        page = getattr(self, "context_page", None)
        cleared = await wait_manual_before_begin(
            page, _active_emitter(), platform, session.manual or manual
        )
        if not cleared:
            print(f"[engine] {platform} 人工验证未完成，继续尝试原登录流程（大概率失败）", flush=True)
        await asyncio.sleep(0.5)  # 等页面跳转稳定
        return await original_begin(self, *args, **kwargs)

    # 包装层数标记：自测用它断言"只包装一次"（重复安装 → 2）
    _wrapped_check.__engine_wrap_count__ = 1 + int(  # type: ignore[attr-defined]  # 原因：给包装后的登录函数挂计数标记（Callable 无该属性声明）
        getattr(original_check, "__engine_wrap_count__", 0) or 0
    )
    _wrapped_begin.__engine_wrap_count__ = 1 + int(  # type: ignore[attr-defined]  # 原因：给包装后的登录函数挂计数标记（Callable 无该属性声明）
        getattr(original_begin, "__engine_wrap_count__", 0) or 0
    )

    cls.check_login_state = _wrapped_check
    cls.begin = _wrapped_begin
    cls._engine_instrumented = True  # 兼容旧标记（跨模块可读）
    session.installed.add(marker)


# --------------------------------------------------------------------------------------
# 清理 & 登录页观察（login.status: waiting_scan / need_manual）
# --------------------------------------------------------------------------------------
async def close_crawler(crawler: Any, timeout: float = 15.0) -> None:
    import asyncio

    context = getattr(crawler, "browser_context", None)
    if context is None:
        return
    try:
        await asyncio.wait_for(context.close(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 清理尽力而为，不能影响终态事件
        log.debug("关闭浏览器上下文失败（忽略）：%s: %s", type(exc).__name__, exc)



async def should_trigger_sms(page: Any, platform: str, qr_count: int) -> bool:
    """短信二次验证触发条件（严格）：

    - 必须已经出过二维码（qr_count > 0，说明用户已进入扫码流程）
    - 页面出现短信验证专属文案
    - 且二维码元素已不可见（二维码消失/登录弹窗已切换）
    仅仅页面上有短信验证码输入框/「验证码登录」tab 一律不算。
    """
    if qr_count <= 0:
        return False
    info = await detect_verification(page)
    if not info or str(info.get("kind")) != "sms":
        return False
    if await _qr_visible_for_platform(page, platform):
        return False
    return True


async def watch_login_page(
    crawler: Any,
    emitter: EventEmitter,
    interval: float = 2.0,
    manual: Optional[ManualGate] = None,
    capture: Optional[Any] = None,
    platform: str = "",
) -> None:
    """轮询登录页：验证页 → need_manual（并把浏览器窗口置前）；出码后周期性 waiting_scan。

    - 短信二次验证必须同时满足“已出码 + 二维码消失 + 专属文案”（见 should_trigger_sms），
      普通登录页的手机号/验证码 tab 不触发 need_manual。
    - 不再凭页面文字猜测“已扫码”；只发周期性的“等待手机扫码/确认”，避免未扫码误报。
    """
    import asyncio

    last_tick = 0.0
    while True:
        await asyncio.sleep(interval)
        page = getattr(crawler, "context_page", None)
        if page is None:
            continue
        qr_count = int(getattr(capture, "count", 0) or 0) if capture is not None else 0
        info = await detect_verification(page)
        if info is not None and str(info.get("kind")) == "sms":
            if not await should_trigger_sms(page, platform, qr_count):
                info = None
        if manual is not None:
            await manual.report_async(info, page)
        elif info is not None:
            emitter.status("need_manual", _manual_message(str(info.get("kind") or "captcha")), kind=str(info.get("kind") or "captcha"))
            try:
                if hasattr(page, "bring_to_front"):
                    await page.bring_to_front()
            except Exception as exc:  # noqa: BLE001 - 置前失败不影响已发出的 need_manual
                log.debug("登录窗口置前失败：%s: %s", type(exc).__name__, exc)
        if info is not None:
            continue
        if qr_count <= 0:
            continue
        now = time.monotonic()
        if now - last_tick >= 15.0:
            last_tick = now
            emitter.status("waiting_scan", "等待手机扫码/确认…（二维码已展示，请在手机 App 上操作）")


# --------------------------------------------------------------------------------------
# 上游契约（登录 / 二维码）
# --------------------------------------------------------------------------------------
UPSTREAM_CONTRACT = [
    UpstreamSymbol("login", "tools/utils.py", "show_qrcode"),
    UpstreamSymbol("login", "tools/utils.py", "find_login_qrcode"),
    UpstreamSymbol("login", "tools/utils.py", "find_qrcode_img_from_canvas"),
    UpstreamSymbol("login", "tools/crawler_util.py", "show_qrcode"),
    UpstreamSymbol("login", "tools/crawler_util.py", "find_login_qrcode"),
    UpstreamSymbol("login", "tools/crawler_util.py", "find_qrcode_img_from_canvas"),
]

# 登录类契约（begin / check_login_state）：遍历 LOGIN_CLASSES **自动生成** —— 7 个平台
# 一份来源，既不会漏声明（M1 review：ks / wb 曾漏），也不需要各 platform_*.py 手写重复。
LOGIN_CLASS_CONTRACTS = [
    UpstreamSymbol(
        platform,
        module_name.replace(".", "/") + ".py",
        f"{class_name}.{method}",
        platforms=(platform,),
    )
    for platform, (module_name, class_name) in sorted(LOGIN_CLASSES.items())
    for method in ("begin", "check_login_state")
]

register_contract("login", "登录 / 二维码：工具层二维码钩子", UPSTREAM_CONTRACT)
for _platform, (_module_name, _class_name) in sorted(LOGIN_CLASSES.items()):
    register_contract(
        _platform,
        f"登录类 {_class_name}：begin / check_login_state（由 LOGIN_CLASSES 自动生成）",
        [symbol for symbol in LOGIN_CLASS_CONTRACTS if symbol.owner == _platform],
    )

__all__ = [
    "LOGIN_CLASSES",
    "LOGIN_CLASS_CONTRACTS",
    "_login_class",
    "install_qr_fallback",
    "patch_login_only",
    "patch_login_blocked",
    "instrument_login_class",
    "close_crawler",
    "should_trigger_sms",
    "watch_login_page",
]

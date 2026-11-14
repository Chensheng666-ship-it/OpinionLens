# -*- coding: utf-8 -*-
"""网络异常捕获适配器：记录最近一次网络抖动，供「真未登录 vs 网络异常」判定。

M1.6 从 ``adapters/login.py`` 纯搬迁（函数体一行未改）。它打的是全局 httpx
（``AsyncClient/Client.request``），与登录流程无关，因此单独成文件；
记录状态挂在 :class:`adapters.session.PatchSession` 上（同进程重跑不串状态）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from adapters.base import emit_patch_warning
from adapters.session import current_session, session_lock

log = logging.getLogger("engine.adapters.network")

_NETWORK_TRANSIENT_TOKENS = (
    "connecttimeout",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "connecterror",
    "remoteprotocolerror",
    "networkerror",
    "retryerror",
    "timeout",
    "timed out",
    "connection reset",
    "connection closed",
    "connection refused",
    "econnreset",
    "econnrefused",
    "err_connection",
    "err_name_not_resolved",
    "network is unreachable",
    "proxy",
)
# 网络异常记录（放在 PatchSession.state，避免同进程下一次运行读到上一次的旧异常）
_NET_TS_KEY = "network_exception_ts"
_NET_MESSAGE_KEY = "network_exception_message"
_NET_HOOK_KEY = "install_network_error_hook"


def _is_network_exception(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in _NETWORK_TRANSIENT_TOKENS)


def _record_network_exception(exc: BaseException) -> None:
    """记录最近一次网络异常；登录被拒时据此区分“网络抖动”和“真未登录”。"""
    session = current_session()
    with session_lock():
        session.state[_NET_TS_KEY] = time.monotonic()
        session.state[_NET_MESSAGE_KEY] = f"{type(exc).__name__}: {str(exc)[:240]}"


def _recent_network_exception(window_seconds: float = 90.0) -> str:
    state = current_session().state
    age = time.monotonic() - float(state.get(_NET_TS_KEY) or 0.0)
    message = state.get(_NET_MESSAGE_KEY)
    if message and age <= window_seconds:
        return str(message)
    return ""


def clear_network_transient() -> None:
    """清空网络异常记录（例如忽略页面首页加载失败后，单独判断 pong 结果）。"""
    state = current_session().state
    with session_lock():
        state[_NET_TS_KEY] = 0.0
        state[_NET_MESSAGE_KEY] = ""


def install_network_error_hook() -> Dict[str, Any]:
    """抓取所有平台 HTTP 请求的网络异常，供 crawl 模式统一识别。

    很多平台 pong() 内部会吞掉异常并返回 False，随后 core 调用 login.begin()；
    crawl 模式又把 login.begin() 拒绝成 NotLoggedIn，导致任何平台的网络抖动
    都被误报成“未登录”。这里记录最近网络异常，让 patch_login_blocked 能改抛
    NetworkTransient，由 sidecar 自动重试。
    """
    session = current_session()
    if session.is_installed(_NET_HOOK_KEY):
        return {"installed": True}
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001 - 可选增强：缺失时网络异常退化为通用错误
        emit_patch_warning("-", "install_network_error_hook", f"httpx 不可用，网络抖动识别降级：{type(exc).__name__}: {exc}")
        return {"installed": False}

    original_async_request = httpx.AsyncClient.request

    async def async_request_with_network_capture(self, *args, **kwargs):
        try:
            return await original_async_request(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_network_exception(exc):
                _record_network_exception(exc)
            raise

    httpx.AsyncClient.request = async_request_with_network_capture

    try:
        original_sync_request = httpx.Client.request

        def sync_request_with_network_capture(self, *args, **kwargs):
            try:
                return original_sync_request(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if _is_network_exception(exc):
                    _record_network_exception(exc)
                raise

        httpx.Client.request = sync_request_with_network_capture
    except Exception as exc:  # noqa: BLE001 - 同步客户端包装属可选增强
        log.debug("同步 httpx.Client.request 包装失败（仅影响同步请求）：%s: %s", type(exc).__name__, exc)

    session.installed.add(_NET_HOOK_KEY)
    return {"installed": True}

__all__ = [
    "install_network_error_hook",
    "clear_network_transient",
    "_is_network_exception",
    "_record_network_exception",
    "_recent_network_exception",
]

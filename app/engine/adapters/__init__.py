# -*- coding: utf-8 -*-
"""MediaCrawler 适配器包（P0-1：patches.py 适配器化 + 上游契约探测 + PatchSession）。

导入顺序即契约登记顺序：``base`` 提供基础设施，``login`` 提供 ``LOGIN_CLASSES``
（登录类契约由其自动生成），其余适配器在导入时 ``register_contract(...)`` 登记依赖的
上游符号。

对外入口（``patches.py`` 门面会转发这些名字）：
* :func:`detect_upstream_contract` / :func:`iter_contracts` / :func:`adapter_registry`
* :class:`EngineIncompatible` / :class:`EnginePatchError`
* :func:`emit_patch_warning` / :func:`emit_patch_error` / :func:`require_patch`
* :class:`PatchSession` / :func:`current_session` / :func:`start_session` / :func:`reset_session`
"""

from __future__ import annotations

from adapters import base, browser, content, login, manual, network, qr, session
from adapters import platform_bilibili, platform_douyin, platform_kuaishou, platform_tieba
from adapters import platform_weibo, platform_xhs, platform_zhihu
from adapters.base import (
    AlreadyLoggedIn,
    EngineIncompatible,
    EnginePatchError,
    LoginOnlyDone,
    MissingSymbol,
    NetworkTransient,
    NotLoggedIn,
    PLATFORMS,
    QrLoadFailed,
    UpstreamSymbol,
    adapter_registry,
    clear_contract_cache,
    contract_cache_info,
    detect_upstream_contract,
    emit_patch_error,
    emit_patch_warning,
    get_warning_emitter,
    iter_contracts,
    patch_error_counts,
    patch_warning_counts,
    register_contract,
    require_patch,
    reset_patch_warning_counts,
    set_warning_emitter,
)
from adapters.login import LOGIN_CLASSES, LOGIN_CLASS_CONTRACTS, _login_class
from adapters.manual import ManualGate, detect_verification, wait_manual_before_begin
from adapters.network import clear_network_transient, install_network_error_hook
from adapters.qr import (
    QrCapture,
    extract_qr_fallback,
    inspect_qr_candidates,
    install_qr_capture,
    validate_qr_bytes,
    validate_qr_data_uri,
)
from adapters.session import (
    PatchSession,
    current_session,
    ensure_session,
    remember_originals,
    reset_session,
    restore_originals,
    start_session,
)

__all__ = [
    "base",
    "browser",
    "content",
    "login",
    "manual",
    "network",
    "qr",
    "session",
    "platform_bilibili",
    "platform_douyin",
    "platform_kuaishou",
    "platform_tieba",
    "platform_weibo",
    "platform_xhs",
    "platform_zhihu",
    "EngineIncompatible",
    "EnginePatchError",
    "LOGIN_CLASSES",
    "LOGIN_CLASS_CONTRACTS",
    "ManualGate",
    "MissingSymbol",
    "PatchSession",
    "QrCapture",
    "UpstreamSymbol",
    "adapter_registry",
    "clear_contract_cache",
    "clear_network_transient",
    "contract_cache_info",
    "detect_upstream_contract",
    "detect_verification",
    "emit_patch_error",
    "emit_patch_warning",
    "extract_qr_fallback",
    "inspect_qr_candidates",
    "install_network_error_hook",
    "install_qr_capture",
    "iter_contracts",
    "patch_warning_counts",
    "require_patch",
    "reset_session",
    "start_session",
    "validate_qr_bytes",
    "validate_qr_data_uri",
    "wait_manual_before_begin",
]

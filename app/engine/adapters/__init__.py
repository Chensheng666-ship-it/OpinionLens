# -*- coding: utf-8 -*-
"""MediaCrawler 适配器包（P0-1：patches.py 适配器化 + 上游契约探测）。

导入顺序即契约登记顺序：``base`` 提供基础设施，其余适配器在导入时
``register_contract(...)`` 登记自己依赖的上游符号。

对外入口（``patches.py`` 门面会转发这些名字）：
* :func:`detect_upstream_contract` / :func:`iter_contracts` / :func:`adapter_registry`
* :class:`EngineIncompatible` / :class:`EnginePatchError`
* :func:`emit_patch_warning` / :func:`emit_patch_error` / :func:`require_patch`
"""

from __future__ import annotations

from adapters import base, browser, content, login
from adapters import platform_bilibili, platform_douyin, platform_kuaishou, platform_tieba
from adapters import platform_weibo, platform_xhs, platform_zhihu
from adapters.base import (
    AlreadyLoggedIn,
    EngineIncompatible,
    EnginePatchError,
    LOGIN_CLASSES,
    LoginOnlyDone,
    MissingSymbol,
    NetworkTransient,
    NotLoggedIn,
    PLATFORMS,
    QrLoadFailed,
    UpstreamSymbol,
    adapter_registry,
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

__all__ = [
    "base",
    "browser",
    "content",
    "login",
    "platform_bilibili",
    "platform_douyin",
    "platform_kuaishou",
    "platform_tieba",
    "platform_weibo",
    "platform_xhs",
    "platform_zhihu",
    "EngineIncompatible",
    "EnginePatchError",
    "MissingSymbol",
    "UpstreamSymbol",
    "adapter_registry",
    "detect_upstream_contract",
    "emit_patch_error",
    "emit_patch_warning",
    "iter_contracts",
    "patch_warning_counts",
    "require_patch",
]

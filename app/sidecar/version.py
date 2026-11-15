# -*- coding: utf-8 -*-
"""应用版本号的**唯一来源**（Python sidecar 侧）。

约定
----
* ``app/desktop/package.json`` 的 ``version`` 是产品版本号的来源（Electron
  ``app.getVersion()`` 读它），主进程启动 sidecar 时通过环境变量
  ``FS_APP_VERSION`` 传下来，见 ``app/desktop/main.js``。
* 打包运行 / ``python app/sidecar/main.py`` 直跑 / CI 自测时该变量可能不存在，
  此时回退到 ``FALLBACK_VERSION``。
* ``FALLBACK_VERSION`` 必须与 ``app/desktop/package.json`` 的 ``version`` 一致，
  离线自测 ``app/sidecar/selftest.py`` 会断言这一点（``版本号一致性``）。

改版本号流程：改 ``package.json`` → 同步 ``FALLBACK_VERSION`` → 补 ``CHANGELOG.md``。
不要再在别处硬编码版本字符串（历史上 ``0.6.0`` / ``0.3.0`` / ``0.1.0`` 三个值同时存在过）。
"""
from __future__ import annotations

import os
from typing import Optional

#: ``package.json`` 版本不可用时的回退值（需与 package.json 保持一致）
FALLBACK_VERSION = "0.6.0"


def app_version(env: Optional[dict] = None) -> str:
    """返回当前应用版本号：优先 ``FS_APP_VERSION``，否则回退 ``FALLBACK_VERSION``。"""
    source = os.environ if env is None else env
    raw = (source.get("FS_APP_VERSION") or "").strip()
    return raw or FALLBACK_VERSION


#: 进程内统一引用的版本号（导入时求值一次；环境变量由主进程在校验启动前注入）
APP_VERSION = app_version()

# -*- coding: utf-8 -*-
"""贴吧适配器：声明上游契约（客户端 / 搜索与评论方法）。

login.py 里的 ``_switch_tieba_to_passport_qr_page`` 属登录/二维码流程；
``BaiduTieBaLogin.begin`` / ``check_login_state`` 的契约由 ``adapters/login.py``
遍历 ``LOGIN_CLASSES`` 自动生成，这里不再手写重复声明。
"""

from __future__ import annotations

import logging

from adapters.base import UpstreamSymbol, register_contract

log = logging.getLogger("engine.adapters.platform_tieba")

UPSTREAM_CONTRACT = [
    UpstreamSymbol("tieba", "media_platform/tieba/core.py", "TieBaCrawler", platforms=("tieba",)),
    UpstreamSymbol(
        "tieba", "media_platform/tieba/core.py", "TieBaCrawler.create_tieba_client", platforms=("tieba",)
    ),
    UpstreamSymbol("tieba", "media_platform/tieba/client.py", "BaiduTieBaClient", platforms=("tieba",)),
    UpstreamSymbol(
        "tieba", "media_platform/tieba/client.py", "BaiduTieBaClient.get_notes_by_keyword", platforms=("tieba",)
    ),
    UpstreamSymbol(
        "tieba", "media_platform/tieba/client.py", "BaiduTieBaClient.get_note_all_comments", platforms=("tieba",)
    ),
    UpstreamSymbol("tieba", "media_platform/tieba/client.py", "BaiduTieBaClient.pong", platforms=("tieba",)),
]

register_contract("tieba", "贴吧：核心爬虫 / 客户端（登录类契约由 login.py 自动生成）", UPSTREAM_CONTRACT)

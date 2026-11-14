# -*- coding: utf-8 -*-
"""贴吧适配器：声明上游契约（登录走 MediaCrawler 原生 + login.py 的 passport 二维码页切换）。

贴吧二维码页切换逻辑（``_switch_tieba_to_passport_qr_page``）属登录/二维码流程，
放在 ``adapters/login.py``，这里只声明它依赖的上游符号。
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
    UpstreamSymbol("tieba", "media_platform/tieba/login.py", "BaiduTieBaLogin", platforms=("tieba",)),
    UpstreamSymbol("tieba", "media_platform/tieba/login.py", "BaiduTieBaLogin.begin", platforms=("tieba",)),
    UpstreamSymbol(
        "tieba", "media_platform/tieba/login.py", "BaiduTieBaLogin.check_login_state", platforms=("tieba",)
    ),
]

register_contract("tieba", "贴吧：核心爬虫 / 客户端 / 登录类（含 passport 二维码页）", UPSTREAM_CONTRACT)

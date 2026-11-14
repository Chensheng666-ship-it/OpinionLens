# -*- coding: utf-8 -*-
"""B 站适配器：无行为补丁，只声明上游契约（BilibiliClient / 搜索与评论方法）。

登录类（``BilibiliLogin.begin`` / ``check_login_state``）的契约由
``adapters/login.py`` 遍历 ``LOGIN_CLASSES`` 自动生成，这里不再手写重复声明。
"""

from __future__ import annotations

import logging

from adapters.base import UpstreamSymbol, register_contract

log = logging.getLogger("engine.adapters.platform_bilibili")

UPSTREAM_CONTRACT = [
    UpstreamSymbol("bili", "media_platform/bilibili/core.py", "BilibiliCrawler", platforms=("bili",)),
    UpstreamSymbol(
        "bili", "media_platform/bilibili/core.py", "BilibiliCrawler.create_bilibili_client", platforms=("bili",)
    ),
    UpstreamSymbol("bili", "media_platform/bilibili/client.py", "BilibiliClient", platforms=("bili",)),
    UpstreamSymbol(
        "bili",
        "media_platform/bilibili/client.py",
        "BilibiliClient.search_video_by_keyword",
        platforms=("bili",),
    ),
    UpstreamSymbol(
        "bili", "media_platform/bilibili/client.py", "BilibiliClient.get_video_comments", platforms=("bili",)
    ),
    UpstreamSymbol("bili", "media_platform/bilibili/client.py", "BilibiliClient.pong", platforms=("bili",)),
]

register_contract("bili", "B 站：核心爬虫 / 客户端（登录类契约由 login.py 自动生成）", UPSTREAM_CONTRACT)

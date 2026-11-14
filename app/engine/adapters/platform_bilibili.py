# -*- coding: utf-8 -*-
"""B 站适配器：无行为补丁，只声明上游契约（BilibiliClient / 登录类 / 搜索与评论方法）。"""

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
    UpstreamSymbol("bili", "media_platform/bilibili/login.py", "BilibiliLogin", platforms=("bili",)),
    UpstreamSymbol("bili", "media_platform/bilibili/login.py", "BilibiliLogin.begin", platforms=("bili",)),
    UpstreamSymbol(
        "bili", "media_platform/bilibili/login.py", "BilibiliLogin.check_login_state", platforms=("bili",)
    ),
]

register_contract("bili", "B 站：核心爬虫 / 客户端 / 登录类", UPSTREAM_CONTRACT)

# -*- coding: utf-8 -*-
"""小红书适配器：无行为补丁（登录/搜索走 MediaCrawler 原生实现），只声明上游契约。

作用：上游改名/重构时，engine 入口会先跑契约探测并明确报错（ENGINE_INCOMPATIBLE），
而不是等到「采不到数据但显示完成」才发现补丁打空枪。
"""

from __future__ import annotations

import logging

from adapters.base import UpstreamSymbol, register_contract

log = logging.getLogger("engine.adapters.platform_xhs")

UPSTREAM_CONTRACT = [
    UpstreamSymbol("xhs", "media_platform/xhs/core.py", "XiaoHongShuCrawler", platforms=("xhs",)),
    UpstreamSymbol("xhs", "media_platform/xhs/core.py", "XiaoHongShuCrawler.create_xhs_client", platforms=("xhs",)),
    UpstreamSymbol("xhs", "media_platform/xhs/client.py", "XiaoHongShuClient", platforms=("xhs",)),
    UpstreamSymbol(
        "xhs", "media_platform/xhs/client.py", "XiaoHongShuClient.get_note_by_keyword", platforms=("xhs",)
    ),
    UpstreamSymbol(
        "xhs", "media_platform/xhs/client.py", "XiaoHongShuClient.get_note_all_comments", platforms=("xhs",)
    ),
    UpstreamSymbol("xhs", "media_platform/xhs/client.py", "XiaoHongShuClient.pong", platforms=("xhs",)),
    UpstreamSymbol("xhs", "media_platform/xhs/login.py", "XiaoHongShuLogin", platforms=("xhs",)),
    UpstreamSymbol("xhs", "media_platform/xhs/login.py", "XiaoHongShuLogin.begin", platforms=("xhs",)),
    UpstreamSymbol(
        "xhs", "media_platform/xhs/login.py", "XiaoHongShuLogin.check_login_state", platforms=("xhs",)
    ),
]

register_contract("xhs", "小红书：核心爬虫 / 客户端 / 登录类", UPSTREAM_CONTRACT)

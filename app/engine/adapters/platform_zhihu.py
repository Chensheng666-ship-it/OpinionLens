# -*- coding: utf-8 -*-
"""知乎适配器：无行为补丁，只声明上游契约（登录类 / 搜索与评论方法）。

知乎签名脚本走 execjs（见 ``entry._configure_javascript_runtime``），
客户端符号缺失时契约探测会先报错，不会静默「扫码成功但未登录」。
"""

from __future__ import annotations

import logging

from adapters.base import UpstreamSymbol, register_contract

log = logging.getLogger("engine.adapters.platform_zhihu")

UPSTREAM_CONTRACT = [
    UpstreamSymbol("zhihu", "media_platform/zhihu/core.py", "ZhihuCrawler", platforms=("zhihu",)),
    UpstreamSymbol(
        "zhihu", "media_platform/zhihu/core.py", "ZhihuCrawler.create_zhihu_client", platforms=("zhihu",)
    ),
    UpstreamSymbol("zhihu", "media_platform/zhihu/client.py", "ZhiHuClient", platforms=("zhihu",)),
    UpstreamSymbol(
        "zhihu", "media_platform/zhihu/client.py", "ZhiHuClient.get_note_by_keyword", platforms=("zhihu",)
    ),
    UpstreamSymbol(
        "zhihu", "media_platform/zhihu/client.py", "ZhiHuClient.get_note_all_comments", platforms=("zhihu",)
    ),
    UpstreamSymbol("zhihu", "media_platform/zhihu/client.py", "ZhiHuClient.pong", platforms=("zhihu",)),
    UpstreamSymbol("zhihu", "media_platform/zhihu/login.py", "ZhiHuLogin", platforms=("zhihu",)),
    UpstreamSymbol("zhihu", "media_platform/zhihu/login.py", "ZhiHuLogin.begin", platforms=("zhihu",)),
    UpstreamSymbol(
        "zhihu", "media_platform/zhihu/login.py", "ZhiHuLogin.check_login_state", platforms=("zhihu",)
    ),
]

register_contract("zhihu", "知乎：核心爬虫 / 客户端 / 登录类", UPSTREAM_CONTRACT)

# -*- coding: utf-8 -*-
"""知乎适配器：无行为补丁，只声明上游契约（客户端 / 搜索与评论方法）。

知乎签名脚本走 execjs（见 ``entry._configure_javascript_runtime``），客户端符号缺失时
契约探测会先报错，不会静默「扫码成功但未登录」。
登录类（``ZhiHuLogin.begin`` / ``check_login_state``）的契约由 ``adapters/login.py``
遍历 ``LOGIN_CLASSES`` 自动生成，这里不再手写重复声明。
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
]

register_contract("zhihu", "知乎：核心爬虫 / 客户端（登录类契约由 login.py 自动生成）", UPSTREAM_CONTRACT)

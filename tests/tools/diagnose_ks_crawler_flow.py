# -*- coding: utf-8 -*-
"""复刻 engine crawl 的 KuaishouCrawler.start()+search() 流程，并打印搜索请求响应的前几条，
定位 engine 运行中为什么拿到推荐流。"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
MC = Path(os.environ.get("MC_ROOT", PROJECT / "MediaCrawler"))
DATA = PROJECT / "app" / "data"
os.chdir(MC)
sys.path.insert(0, str(MC))

import config  # noqa: E402
from media_platform.kuaishou.core import KuaishouCrawler  # noqa: E402


async def main():
    config.PLATFORM = "ks"
    config.HEADLESS = True
    config.SAVE_LOGIN_STATE = True
    config.USER_DATA_DIR = str(DATA / "browser_data" / "%s_user_data_dir")
    config.KEYWORDS = "示例科技"
    config.CRAWLER_MAX_NOTES_COUNT = 5
    config.ENABLE_GET_COMMENTS = False
    config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = 0
    config.ENABLE_CDP_MODE = False
    crawler = KuaishouCrawler()
    orig_start = crawler.start

    async def wrapped_start():
        r = await orig_start()
        # start 内部已创建 self.ks_client；在这里包一层打印
        client = getattr(crawler, "ks_client", None)
        if client is not None:
            orig_search = client.search_info_by_keyword_v2

            async def logged_search(keyword, pcursor, search_session_id=""):
                payload = {"keyword": keyword, "pcursor": pcursor, "page": "search", "searchSessionId": search_session_id}
                resp = await orig_search(keyword, pcursor, search_session_id)
                caps = [(f.get("photo") or {}).get("caption", "")[:80] for f in (resp.get("feeds") or [])[:5]]
                print(f"[probe] search payload={payload} result={resp.get('result')} feeds={len(resp.get('feeds') or [])}")
                for i, c in enumerate(caps, 1):
                    print(f"[probe]   {i}. {c.replace(chr(10),' ')}")
                return resp

            client.search_info_by_keyword_v2 = logged_search
        return r

    crawler.start = wrapped_start
    await crawler.start()
    print("[probe] page url:", crawler.context_page.url[:120])
    await crawler.search()
    await crawler.close()


if __name__ == "__main__":
    asyncio.run(main())

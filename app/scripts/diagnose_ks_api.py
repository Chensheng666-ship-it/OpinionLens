# -*- coding: utf-8 -*-
"""对比快手搜索接口两种请求体，定位"关键词不生效"的根因。
A) MediaCrawler 当前实现：{"keyword","pcursor","page":"search","searchSessionId":""}
B) 网页真实抓包：{"keyword","page":"search","webPageArea":"","pcursor":""}
输出各自返回的前 8 条 caption，看是否与关键词相关。
"""
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
from media_platform.kuaishou.core import KS_SIGN_CAPTURE_SCRIPT, KuaishouCrawler  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402


def captions(resp: dict) -> list[str]:
    out = []
    for feed in (resp.get("feeds") or [])[:8]:
        photo = feed.get("photo") or {}
        out.append((photo.get("caption") or "")[:100])
    return out


async def run() -> None:
    config.PLATFORM = "ks"
    config.HEADLESS = True
    config.SAVE_LOGIN_STATE = True
    config.USER_DATA_DIR = str(DATA / "browser_data" / "%s_user_data_dir")
    crawler = KuaishouCrawler()
    async with async_playwright() as p:
        ctx = await crawler.launch_browser(p.chromium, None, None, headless=True)
        await ctx.add_init_script(path="libs/stealth.min.js")
        page = await ctx.new_page()
        await page.add_init_script(KS_SIGN_CAPTURE_SCRIPT)
        await page.goto("https://www.kuaishou.com?isHome=1", wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        crawler.browser_context = ctx
        crawler.context_page = page
        client = await crawler.create_ks_client(None)

        for name, payload in (
            ("A_current", {"keyword": "示例科技", "pcursor": "", "page": "search", "searchSessionId": ""}),
            ("B_webstyle", {"keyword": "示例科技", "page": "search", "webPageArea": "", "pcursor": ""}),
            ("C_webstyle_no_kw", {"keyword": "无人机", "page": "search", "webPageArea": "", "pcursor": ""}),
        ):
            try:
                resp = await client.request_rest_v2_signed("/rest/v/search/feed", payload)
                caps = captions(resp)
                print(f"\n=== {name} result={resp.get('result')} feeds={len(resp.get('feeds') or [])} ===")
                for i, c in enumerate(caps, 1):
                    print(f"  {i}. {c}")
            except Exception as exc:  # noqa: BLE001
                print(f"\n=== {name} ERROR: {type(exc).__name__}: {exc}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(run())

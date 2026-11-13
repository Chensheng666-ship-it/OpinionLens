# -*- coding: utf-8 -*-
"""对比 Chrome vs Edge(msedge) 浏览器内核下，快手搜索接口是否返回不同结果。
用法: python diagnose_ks_channel.py chrome|msedge
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
from media_platform.kuaishou import core as ks_core  # noqa: E402
from media_platform.kuaishou.core import KS_SIGN_CAPTURE_SCRIPT, KuaishouCrawler  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

CHANNEL = sys.argv[1] if len(sys.argv) > 1 else "chrome"


async def patched_launch_browser(self, chromium, playwright_proxy, user_agent, headless=True):
    os.makedirs(os.path.join(os.getcwd(), "browser_data"), exist_ok=True)
    return await chromium.launch_persistent_context(
        user_data_dir=config.USER_DATA_DIR % config.PLATFORM,
        accept_downloads=True,
        headless=headless,
        proxy=playwright_proxy,
        viewport={"width": 1920, "height": 1080},
        user_agent=user_agent,
        channel=CHANNEL,
    )


ks_core.KuaishouCrawler.launch_browser = patched_launch_browser


def captions(resp):
    out = []
    for feed in (resp.get("feeds") or [])[:6]:
        photo = feed.get("photo") or {}
        out.append((photo.get("caption") or "")[:90].replace("\n", " "))
    return out


async def run():
    config.PLATFORM = "ks"
    config.HEADLESS = True
    config.SAVE_LOGIN_STATE = True
    config.USER_DATA_DIR = str(DATA / "browser_data" / "%s_user_data_dir")
    crawler = KuaishouCrawler()
    async with async_playwright() as p:
        ctx = await crawler.launch_browser(p.chromium, None, None, headless=True)
        try:
            await ctx.add_init_script(path="libs/stealth.min.js")
        except Exception as e:
            print("stealth inject failed:", e)
        page = await ctx.new_page()
        await page.add_init_script(KS_SIGN_CAPTURE_SCRIPT)
        await page.goto("https://www.kuaishou.com?isHome=1", wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        crawler.browser_context = ctx
        crawler.context_page = page
        client = await crawler.create_ks_client(None)
        print(f"channel={CHANNEL} UA={crawler.user_agent[:80]}")
        for name, payload in (
            ("A_current", {"keyword": "示例科技", "pcursor": "", "page": "search", "searchSessionId": ""}),
            ("B_webstyle", {"keyword": "示例科技", "page": "search", "webPageArea": "", "pcursor": ""}),
        ):
            resp = await client.request_rest_v2_signed("/rest/v/search/feed", payload)
            print(f"--- {name}: result={resp.get('result')} feeds={len(resp.get('feeds') or [])}")
            for i, c in enumerate(captions(resp), 1):
                print(f"  {i}. {c}")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(run())

# -*- coding: utf-8 -*-
"""捕获快手搜索页滚动加载时真实的分页请求参数（pcursor/searchSessionId/其他字段）。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT = Path(__file__).resolve().parents[2]
PROFILE = PROJECT / "app" / "data" / "browser_data" / "ks_user_data_dir"
OUT = PROJECT / "app" / "data" / "logs" / "ks_diag"
OUT.mkdir(parents=True, exist_ok=True)

captures = []


def main() -> int:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE), channel="msedge", headless=False, viewport={"width": 1440, "height": 900}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if "search/feed" in req.url:
                captures.append({"url": req.url[:120], "post": (req.post_data or "")[:800]})

        page.on("request", on_request)
        page.goto("https://www.kuaishou.com/search/video?searchKey=%E9%A3%9E%E5%B0%9A%E7%A7%91%E6%8A%80", timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(6000)
        for i in range(5):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(2500)
            print("scroll", i + 1, "captures:", len(captures))
        (OUT / "scroll_requests.json").write_text(json.dumps(captures, ensure_ascii=False, indent=1), encoding="utf-8")
        print("captured:", len(captures))
        for i, c in enumerate(captures):
            print(f"--[{i}]", c["post"][:500])
        page.screenshot(path=str(OUT / "ks_search_scroll.png"))
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

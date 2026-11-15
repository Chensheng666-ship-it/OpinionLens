# -*- coding: utf-8 -*-
"""诊断快手搜索：用真实已登录 profile 打开快手搜索页，抓 /rest/v/search/feed 的请求体与响应，
对比不同关键词返回是否不同，判断"关键词未生效"还是"平台确实无强相关内容"。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT = Path(__file__).resolve().parents[2]
PROFILE = PROJECT / "app" / "data" / "browser_data" / "ks_user_data_dir"
OUT = PROJECT / "app" / "data" / "logs" / "ks_diag"
OUT.mkdir(parents=True, exist_ok=True)

KEYWORD = "示例科技"


def main() -> int:
    captures = []
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            channel="msedge",
            headless=True,
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_request(req):
            if "/rest/v/search/feed" in req.url or "search/feed" in req.url:
                try:
                    captures.append({
                        "url": req.url,
                        "method": req.method,
                        "post_data": (req.post_data or "")[:1200],
                        "headers": {k: v for k, v in req.headers.items() if k.lower() in ("cookie", "content-type", "referer", "user-agent")},
                    })
                except Exception as e:  # noqa: BLE001
                    captures.append({"error": str(e)})

        page.on("request", on_request)

        print("[1] 打开关键词搜索页:", KEYWORD)
        try:
            page.goto(f"https://www.kuaishou.com/search/video?searchKey={KEYWORD}", timeout=45000, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            print("goto 失败:", str(e)[:160])
        page.wait_for_timeout(8000)
        print("title:", page.title(), "url:", page.url[:140])
        page.screenshot(path=str(OUT / "ks_search_page.png"))

        # 页面可见文案/可能的结果标题
        try:
            text = page.inner_text("body")[:1200]
        except Exception:
            text = ""
        print("页面文本片段:", text.replace("\n", " | ")[:500])

        # 从 DOM 中抓可见标题（快手结果卡片文字）
        try:
            titles = page.evaluate("""() => {
                const nodes = Array.from(document.querySelectorAll('[class*=card], [class*=video], [class*=feed]'))
                  .map(n => (n.innerText || '').trim()).filter(t => t.length > 4 && t.length < 120);
                return Array.from(new Set(nodes)).slice(0, 20);
            }""")
            print("DOM 候选标题:", json.dumps(titles, ensure_ascii=False)[:1200])
        except Exception as e:  # noqa: BLE001
            print("DOM 抓取失败:", e)

        # 再搜一个对照关键词（无人机），看接口请求是否不同
        captures.clear()
        print("\n[2] 对照关键词: 无人机")
        try:
            page.goto("https://www.kuaishou.com/search/video?searchKey=%E6%97%A0%E4%BA%BA%E6%9C%BA", timeout=45000, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            print("goto 失败:", str(e)[:160])
        page.wait_for_timeout(8000)
        print("title:", page.title(), "url:", page.url[:140])

        (OUT / "requests.json").write_text(json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n捕获搜索请求数:", len(captures))
        for c in captures[:3]:
            print("REQ:", json.dumps(c, ensure_ascii=False)[:900])
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

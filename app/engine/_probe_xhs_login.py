# -*- coding: utf-8 -*-
"""一次性验证：用 MediaCrawler 真实跑小红书扫码登录，把二维码截获保存为 PNG。

不修改 MediaCrawler 源码；全部通过运行时 patch：
  1) show_qrcode -> 保存 PNG + 打印事件
  2) Playwright BrowserType.launch* -> channel='msedge'（免装 Chromium）
  3) 登录成功后 search() 抛 LoginOnlyDone 结束进程
"""
import asyncio
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[2]
MC_ROOT = Path(os.environ.get("MC_ROOT", _PROJECT / "MediaCrawler"))
OUT = _PROJECT / "app" / "data" / "qr_test"
OUT.mkdir(parents=True, exist_ok=True)

os.chdir(MC_ROOT)
sys.path.insert(0, str(MC_ROOT))


def emit(obj):
    print("##EVENT##" + json.dumps(obj, ensure_ascii=False), flush=True)


# ---- 0) 先按 MediaCrawler 正常顺序导入 tools.utils（避免循环导入问题） ----
import tools.utils as tutil

# ---- 1) 早期 patch：不脱敏 ----
import tools.user_hash as uh

uh.anonymize_user_id = lambda uid: "" if uid is None else str(uid)
uh.mask_nickname = lambda name: "" if name is None else str(name)

# ---- 2) patch show_qrcode ----
import tools.crawler_util as cu

_seq = {"n": 0}
_hashes = set()


def patched_show_qrcode(qr_code):
    if "," in str(qr_code):
        qr_code = str(qr_code).split(",")[1]
    raw = base64.b64decode(qr_code)
    import hashlib

    h = hashlib.sha256(raw).hexdigest()[:12]
    fp = OUT / f"xhs_qr_{h}.png"
    fp.write_bytes(raw)
    if h not in _hashes:
        _hashes.add(h)
        _seq["n"] += 1
        emit({"type": "login.qrcode", "platform": "xhs", "seq": _seq["n"],
              "image": "data:image/png;base64," + base64.b64encode(raw).decode(),
              "file": str(fp), "bytes": len(raw)})


tutil.show_qrcode = patched_show_qrcode
cu.show_qrcode = patched_show_qrcode

# ---- 3) patch Playwright 浏览器 channel ----
from playwright.async_api import BrowserType as _BT

_orig_lpc = _BT.launch_persistent_context
_orig_launch = _BT.launch


async def _lpc(self, *args, **kwargs):
    kwargs.setdefault("channel", "msedge")
    return await _orig_lpc(self, *args, **kwargs)


async def _launch(self, *args, **kwargs):
    kwargs.setdefault("channel", "msedge")
    return await _orig_launch(self, *args, **kwargs)


_BT.launch_persistent_context = _lpc
_BT.launch = _launch

# ---- 4) 配置 ----
import config

config.PLATFORM = "xhs"
config.LOGIN_TYPE = "qrcode"
config.HEADLESS = True
config.SAVE_LOGIN_STATE = True
config.USER_DATA_DIR = str(OUT / "browser_data" / "%s_user_data_dir")
config.SAVE_DATA_OPTION = "sqlite"
config.ENABLE_GET_COMMENTS = False
config.CRAWLER_MAX_NOTES_COUNT = 1

# ---- 5) 登录成功后停止 ----
import main as mc_main


class LoginOnlyDone(Exception):
    pass


async def _search_stop(self, *a, **kw):
    emit({"type": "login.status", "platform": "xhs", "status": "confirmed", "message": "登录成功（login only 模式）"})
    raise LoginOnlyDone()


async def run():
    crawler = mc_main.CrawlerFactory.create_crawler("xhs")
    crawler.search = _search_stop.__get__(crawler, type(crawler))
    try:
        await crawler.start()
    except LoginOnlyDone:
        emit({"type": "login.status", "platform": "xhs", "status": "done", "message": "login-only 完成"})
    finally:
        try:
            if getattr(crawler, "browser_context", None):
                await crawler.browser_context.close()
        except Exception:
            pass


if __name__ == "__main__":
    emit({"type": "engine.start", "platform": "xhs", "mode": "login"})
    try:
        asyncio.run(asyncio.wait_for(run(), timeout=90))
        emit({"type": "engine.exit", "ok": True})
    except Exception as e:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        emit({"type": "engine.exit", "ok": False, "error": f"{type(e).__name__}: {e}"})
        sys.exit(1)



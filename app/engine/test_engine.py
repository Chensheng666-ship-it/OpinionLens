# -*- coding: utf-8 -*-
"""Engine 离线自测（不联网、不启动浏览器）。

覆盖 CONTRACT §1.4 要求：
  - 事件前缀/字段/顺序（6 类事件，engine.finished 必出且最后一行）
  - patch 生效：user_hash 恒等、show_qrcode 替换、浏览器 channel 默认 msedge、登录拦截
  - 参数映射：CLI -> config（sqlite 路径 / USER_DATA_DIR / keywords / 评论与数量）
  - 进度日志 -> progress 事件
  - 异常场景仍有 engine.finished（ok:false，退出码 0）

运行：
    E:\\...\\MediaCrawler\\.venv\\Scripts\\python.exe app/engine/test_engine.py
"""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True  # 不往 MediaCrawler/ 写 __pycache__

import asyncio
import base64
import contextlib
import io
import json
import logging
import tempfile
import traceback
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import entry  # noqa: E402
import patches  # noqa: E402

CHECKS = []
_FAILED = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    if not ok:
        _FAILED.append((name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  |  {detail}" if detail else ""))
    return bool(ok)


def synth_png(tag: str) -> bytes:
    import hashlib
    import random

    from PIL import Image, ImageDraw

    seed = int(hashlib.sha256(tag.encode("utf-8")).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    image = Image.new("RGB", (128, 128), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 127, 127), outline=(0, 0, 0), width=3)
    for y in range(10, 118, 8):
        for x in range(10, 118, 8):
            if rnd.getrandbits(1):
                draw.rectangle((x, y, x + 6, y + 6), fill=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def run_async(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------------------
# 1. 事件协议
# --------------------------------------------------------------------------------------
def test_event_protocol() -> None:
    sink = []
    emitter = patches.EventEmitter("xhs", "login", sink.append)
    emitter.started()
    emitter.qrcode("data:image/png;base64,AAAA", "/tmp/xhs_abcd.png", 3)
    emitter.status("waiting_qr", "二维码已就绪")
    emitter.test_result(True, "已登录", 812)
    emitter.progress("search", 1, 7, "第 1 页")
    emitter.finished(True, 123456, None, "")

    check("事件数量 = 6（started/qrcode/status/test.result/progress/finished）", len(sink) == 6, f"got {len(sink)}")
    ok_fields = True
    for event in sink:
        expected = patches.EVENT_FIELDS.get(event["type"])
        if expected is None:
            ok_fields = False
            check(f"未知事件类型 {event['type']}", False)
            continue
        actual = set(event.keys())
        if actual != expected:
            ok_fields = False
            print(f"  字段不匹配 {event['type']}: 多={actual - expected} 缺={expected - actual}")
        if event.get("platform") != "xhs" or not isinstance(event.get("ts"), int):
            ok_fields = False
    check("6 类事件字段名严格等于契约（不多不少）+ platform/ts 正确", ok_fields)
    check("engine.finished 是最后一个事件", sink[-1]["type"] == "engine.finished")
    check("engine.started 带 mode/pid", sink[0]["mode"] == "login" and isinstance(sink[0]["pid"], int))
    check("login.qrcode.seq 从 1 递增", sink[1]["seq"] == 1)
    check("engine.finished 字段正确", sink[-1]["ok"] is True and sink[-1]["mode"] == "login" and sink[-1]["db_path"] is None)


def test_event_line_format() -> None:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        emitter = patches.EventEmitter("xhs", "crawl", None)
        emitter.started()
        emitter.finished(True, 1, "/tmp/db.sqlite", "")
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    check("stdout 事件行 = ##EVENT## 前缀 + 单行 JSON", len(lines) == 2 and all(line.startswith("##EVENT##") for line in lines))
    try:
        parsed = [json.loads(line[len("##EVENT##"):]) for line in lines]
        ok_json = True
    except Exception:
        parsed, ok_json = [], False
    check("事件行可被 json.loads 解析", ok_json and len(parsed) == 2)
    check("最后一行是 engine.finished", bool(parsed) and parsed[-1]["type"] == "engine.finished")


# --------------------------------------------------------------------------------------
# 2. MediaCrawler 导入顺序 + 不脱敏 patch
# --------------------------------------------------------------------------------------
def test_mediacrawler_patches() -> None:
    mc_root = patches.resolve_mc_root()
    mc = patches.import_mediacrawler(mc_root)
    check("MediaCrawler 根目录解析成功", (mc_root / "main.py").is_file(), str(mc_root))
    check("CrawlerFactory 含 7 平台", len(mc["main"].CrawlerFactory.CRAWLERS) == 7, str(sorted(mc["main"].CrawlerFactory.CRAWLERS)))

    import tools.user_hash as user_hash

    check("patch: anonymize_user_id 为恒等（raw user_id）", user_hash.anonymize_user_id("u_123") == "u_123")
    check("patch: mask_nickname 为恒等（raw nickname）", user_hash.mask_nickname("张三丰") == "张三丰")
    check("patch: None 输入安全", user_hash.anonymize_user_id(None) == "" and user_hash.mask_nickname(None) == "")

    # store 模块在 import 时 from tools.user_hash import ... -> 应已绑定恒等函数
    import store.xhs as xhs_store

    check(
        "patch 生效于 store 层绑定（store.xhs.anonymize_user_id 为恒等）",
        xhs_store.anonymize_user_id("abc") == "abc" and xhs_store.mask_nickname("李四") == "李四",
    )
    check("MediaCrawler 源码未写 __pycache__（dont_write_bytecode）", sys.dont_write_bytecode is True)
    check("cwd 已切到 MediaCrawler 根", Path.cwd().resolve() == mc_root.resolve(), str(Path.cwd()))


# --------------------------------------------------------------------------------------
# 3. 配置映射
# --------------------------------------------------------------------------------------
def test_config_mapping() -> None:
    mc = patches.import_mediacrawler(patches.resolve_mc_root())
    config = mc["config"]
    tmp = Path(tempfile.mkdtemp(prefix="mc_engine_cfg_"))
    info = patches.apply_config(
        mc,
        "xhs",
        tmp,
        headless=True,
        keywords=["示例科技", "A"],
        max_notes=7,
        comments=False,
        max_comments=9,
    )
    expected_db = str(tmp / "crawler" / "media_crawler.db")
    checks = {
        "PLATFORM": config.PLATFORM == "xhs",
        "LOGIN_TYPE=qrcode": config.LOGIN_TYPE == "qrcode",
        "CRAWLER_TYPE=search": config.CRAWLER_TYPE == "search",
        "SAVE_LOGIN_STATE=True": config.SAVE_LOGIN_STATE is True,
        "HEADLESS=True": config.HEADLESS is True,
        "SAVE_DATA_OPTION=sqlite": config.SAVE_DATA_OPTION == "sqlite",
        "SQLITE_DB_PATH": config.SQLITE_DB_PATH == expected_db,
        "sqlite_db_config['db_path']": config.sqlite_db_config["db_path"] == expected_db,
        "USER_DATA_DIR 模板(绝对)": config.USER_DATA_DIR == str(tmp / "browser_data" / "%s_user_data_dir"),
        "KEYWORDS 合并": config.KEYWORDS == "示例科技,A",
        "CRAWLER_MAX_NOTES_COUNT": config.CRAWLER_MAX_NOTES_COUNT == 7,
        "MAX_COMMENTS": config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES == 9,
        "ENABLE_GET_COMMENTS=False": config.ENABLE_GET_COMMENTS is False,
        "ENABLE_GET_MEDIA=False": config.ENABLE_GET_MEDIA is False,
        "目录已创建": all((tmp / d).is_dir() for d in ("qr", "crawler", "logs", "browser_data", "exports")),
    }
    bad = [name for name, ok in checks.items() if not ok]
    check("apply_config 参数映射与绝对路径全部正确", not bad, "失败项: " + ",".join(bad) if bad else "")
    check(
        "info 摘要（db_path/user_data_dir）正确",
        info["db_path"] == expected_db and info["user_data_dir"].endswith("%s_user_data_dir"),
    )
    check("关键词解析支持中英文逗号/空白", patches.parse_keywords("示例科技， 某某示例 ,,A") == ["示例科技", "某某示例", "A"])


# --------------------------------------------------------------------------------------
# 4. 二维码截获
# --------------------------------------------------------------------------------------
def test_qr_capture() -> None:
    sink = []
    emitter = patches.EventEmitter("xhs", "login", sink.append)
    tmp = Path(tempfile.mkdtemp(prefix="mc_engine_qr_"))
    capture = patches.QrCapture(emitter, tmp, "xhs")

    png1 = synth_png("first")
    capture("data:image/png;base64," + base64.b64encode(png1).decode())
    qr_events = [e for e in sink if e["type"] == "login.qrcode"]
    status_events = [e for e in sink if e["type"] == "login.status"]
    check("show_qrcode 替换后：data URI 被解码保存 PNG", bool(qr_events) and Path(qr_events[0]["file"]).is_file())
    check("login.qrcode 字段完整（seq/image/file/bytes）", bool(qr_events) and qr_events[0]["seq"] == 1 and qr_events[0]["image"].startswith("data:image/png;base64,") and qr_events[0]["bytes"] == len(png1))
    check("首张二维码触发 login.status waiting_qr", len(status_events) == 1 and status_events[0]["status"] == "waiting_qr")
    check("PNG 存放在 data-dir/qr 下且命名含平台", qr_events and "xhs_" in Path(qr_events[0]["file"]).name and Path(qr_events[0]["file"]).parent == tmp / "qr")

    png2 = synth_png("second")
    capture(base64.b64encode(png2).decode())  # 纯 base64（无 data URI 前缀）
    capture(base64.b64encode(png2).decode())  # 重复同一张 -> 去重
    qr_events = [e for e in sink if e["type"] == "login.qrcode"]
    check("纯 base64 可用 + 换图 seq=2", len(qr_events) == 2 and qr_events[1]["seq"] == 2)
    check("同图 sha 去重：不重复发事件", len(qr_events) == 2)
    check("capture.count = 2", capture.count == 2)


# --------------------------------------------------------------------------------------
# 5. 浏览器 channel patch（离线验证：不真正启动浏览器）
# --------------------------------------------------------------------------------------
def test_browser_channel_patch() -> None:
    from playwright.async_api import BrowserType

    info = patches.patch_browser_channel("msedge")
    check("patch_browser_channel 报告已 patch", info.get("patched") is True and info.get("channel") == "msedge")
    check("BROWSER_CHANNEL 为空时不 patch", patches.patch_browser_channel("").get("patched") is False)

    calls = []

    async def fake_persistent(self, *args, **kwargs):
        calls.append(dict(kwargs))
        return "CTX"

    async def fake_launch(self, *args, **kwargs):
        calls.append(dict(kwargs))
        return "BROWSER"

    original_pc = patches._BROWSER_PATCH.get("orig_launch_persistent_context")
    original_launch = patches._BROWSER_PATCH.get("orig_launch")
    patches._BROWSER_PATCH["orig_launch_persistent_context"] = fake_persistent
    patches._BROWSER_PATCH["orig_launch"] = fake_launch
    try:
        context_result = run_async(
            BrowserType.launch_persistent_context(object(), user_data_dir="x", channel="chrome", headless=True)
        )
        browser_result = run_async(BrowserType.launch(object(), headless=True))
    finally:
        patches._BROWSER_PATCH["orig_launch_persistent_context"] = original_pc
        patches._BROWSER_PATCH["orig_launch"] = original_launch

    check("launch_persistent_context 被包装", context_result == "CTX")
    check("显式 channel=chrome 也被覆盖为 msedge", calls and calls[0].get("channel") == "msedge", str(calls[:1]))
    check("BrowserType.launch 同样注入 msedge", browser_result == "BROWSER" and calls[1].get("channel") == "msedge")
    check("原有 kwargs 保留（headless/user_data_dir）", calls[0].get("headless") is True and calls[0].get("user_data_dir") == "x")


# --------------------------------------------------------------------------------------
# 6. 登录拦截 / 登录即停 / 进度日志 Handler
# --------------------------------------------------------------------------------------
def test_login_patches_and_progress() -> None:
    mc = patches.import_mediacrawler(patches.resolve_mc_root())

    # 6.1 login 模式：实例 search 替换后抛 LoginOnlyDone
    class DummyCrawler:
        async def search(self):  # pragma: no cover - 应被替换
            raise AssertionError("原生 search 不应被调用")

    crawler = DummyCrawler()
    patches.patch_login_only(crawler, {"login_attempted": True})
    try:
        run_async(crawler.search())
        login_only_ok = False
    except patches.LoginOnlyDone:
        login_only_ok = True
    check("patch_login_only: 走过扫码流程 -> LoginOnlyDone（登录后不进采集）", login_only_ok)

    crawler2 = DummyCrawler()
    patches.patch_login_only(crawler2, {"login_attempted": False})
    try:
        run_async(crawler2.search())
        already_logged = False
    except patches.AlreadyLoggedIn:
        already_logged = True
    check("patch_login_only: 未走登录流程(pong=true) -> AlreadyLoggedIn（区分已登录）", already_logged)

    # 6.2 crawl 模式：登录类 begin 被替换为 NotLoggedIn
    patches.patch_login_blocked(mc, "xhs")
    login_cls = patches._login_class("xhs")
    try:
        run_async(login_cls.begin(object()))
        blocked = False
    except patches.NotLoggedIn:
        blocked = True
    check("patch_login_blocked: 未登录时 begin() -> NotLoggedIn（不拉起扫码）", blocked)

    # 6.3 日志 -> progress
    sink = []
    emitter = patches.EventEmitter("xhs", "crawl", sink.append)
    handler = patches.install_progress_handler(mc, emitter)
    logger = mc["utils"].logger
    try:
        records = [
            "[XiaoHongShuCrawler.search] Current search keyword: 示例科技",
            "[XiaoHongShuCrawler.search] search Xiaohongshu keyword: 示例科技, page: 2",
            "[get_note_detail_async_task] Begin get note detail, note_id: abc",
            "[XiaoHongShuCrawler.get_comments] Begin get note id comments abc",
            "[store.xhs.update_xhs_note] xhs note: {...}",
            "[XiaoHongShuCrawler.start] Xhs Crawler finished ...",
        ]
        for index in range(1, 6):  # 再补 4 条 store 日志，触发第 5 条进度
            records.append(f"[store.xhs.update_xhs_note] xhs note: {{...}}")
        for message in records:
            logger.handle(logging.LogRecord("MediaCrawler", logging.INFO, __file__, 1, message, None, None))
    finally:
        logger.removeHandler(handler)

    progress = [e for e in sink if e["type"] == "progress"]
    messages = [e["message"] for e in progress]
    check("progress: 关键词 -> stage=search", any(e["stage"] == "search" and "开始搜索" in e["message"] for e in progress), str(messages[:3]))
    check("progress: 翻页 -> page=2", any(e["page"] == 2 for e in progress))
    check("progress: 详情/评论阶段被识别", any(e["stage"] == "detail" for e in progress) and any(e["stage"] == "comments" for e in progress))
    check("progress: store 日志累计 saved（1 与 5 条各发一次）", any(e["saved"] == 1 for e in progress) and any(e["saved"] == 5 for e in progress))
    check("progress: 结束日志 -> stage=done", any(e["stage"] == "done" for e in progress))
    check("progress 字段完整", all(set(e.keys()) == patches.EVENT_FIELDS["progress"] for e in progress))

    # 6.4 关闭评论的提示日志不能被误判为 comments 阶段
    sink2 = []
    emitter2 = patches.EventEmitter("xhs", "crawl", sink2.append)
    handler2 = patches.install_progress_handler(mc, emitter2)
    try:
        logger.handle(
            logging.LogRecord(
                "MediaCrawler",
                logging.INFO,
                __file__,
                1,
                "[XiaoHongShuCrawler.batch_get_note_comments] Crawling comment mode is not enabled",
                None,
                None,
            )
        )
    finally:
        logger.removeHandler(handler2)
    check("关闭评论提示不产生 comments 阶段 progress", not any(e["stage"] == "comments" for e in sink2), str([e.get("message") for e in sink2]))


# --------------------------------------------------------------------------------------
# 7. login 模式终态：已登录 -> confirmed（不误报二维码失败）；需扫码 -> waiting_qr + expired
# --------------------------------------------------------------------------------------
def test_login_mode_explicit_states() -> None:
    import types

    tmp = Path(tempfile.mkdtemp(prefix="mc_engine_loginmode_"))
    args = entry.parse_args(
        ["--mode", "login", "--platform", "xhs", "--data-dir", str(tmp), "--headless", "true", "--timeout", "5"]
    )
    names = (
        "apply_config",
        "patch_browser_channel",
        "install_qr_capture",
        "instrument_login_class",
        "patch_login_only",
        "install_goto_retry",
        "install_qr_fallback",
    )
    saved = {name: getattr(patches, name) for name in names}

    def fake_mc(create_crawler):
        return {
            "main": types.SimpleNamespace(CrawlerFactory=types.SimpleNamespace(create_crawler=create_crawler)),
            "config": types.SimpleNamespace(),
            "utils": types.SimpleNamespace(),
            "crawler_util": types.SimpleNamespace(),
        }

    try:
        patches.apply_config = lambda *a, **k: {"platform": "xhs"}
        patches.patch_browser_channel = lambda *a, **k: {"channel": "msedge", "patched": True}
        patches.instrument_login_class = lambda *a, **k: None
        patches.patch_login_only = lambda crawler, state=None: None
        patches.install_goto_retry = lambda *a, **k: {"installed": True}
        patches.install_qr_fallback = lambda *a, **k: {"installed": True}

        # A. 已登录：crawler.start() 内 pong=true 跳过 login.begin -> AlreadyLoggedIn
        holder: dict = {}

        class FakeCrawlerAlreadyLogged:
            async def start(self):
                raise patches.AlreadyLoggedIn()

        patches.install_qr_capture = lambda *a, **k: types.SimpleNamespace(count=0)
        sink_a = []
        emitter_a = patches.EventEmitter("xhs", "login", sink_a.append)
        run_async(entry._login_async(fake_mc(lambda platform: FakeCrawlerAlreadyLogged()), args, tmp, emitter_a))
        status_a = [e for e in sink_a if e["type"] == "login.status"]
        confirmed_a = [e for e in status_a if e["status"] == "confirmed"]
        check(
            "login 模式: 已有登录态 -> login.status confirmed（message 含“已登录，无需重新扫码”）",
            bool(confirmed_a) and "无需重新扫码" in confirmed_a[0]["message"],
            str([e.get("message") for e in status_a]),
        )
        check(
            "login 模式: 已登录时不再误报“未截获二维码”failed",
            not any(e["status"] == "failed" and "未截获二维码" in e.get("message", "") for e in status_a),
        )

        # B. 需要扫码：先出 login.qrcode/waiting_qr，再超时 -> expired（不应误报二维码失败）
        def real_install(mc, emitter, data_dir, platform):
            capture = patches.QrCapture(emitter, Path(data_dir), platform)
            holder["capture"] = capture
            return capture

        png_b64 = base64.b64encode(synth_png("login-mode-qr")).decode()

        class FakeCrawlerNeedsScan:
            async def start(self):
                holder["capture"]("data:image/png;base64," + png_b64)
                raise asyncio.TimeoutError()

        patches.install_qr_capture = real_install
        sink_b = []
        emitter_b = patches.EventEmitter("xhs", "login", sink_b.append)
        timeout_raised = False
        try:
            run_async(entry._login_async(fake_mc(lambda platform: FakeCrawlerNeedsScan()), args, tmp, emitter_b))
        except asyncio.TimeoutError:
            timeout_raised = True
        status_b = [e for e in sink_b if e["type"] == "login.status"]
        check(
            "login 模式: 需扫码 -> login.qrcode + waiting_qr + expired，且超时向上抛给 main",
            timeout_raised
            and any(e["type"] == "login.qrcode" for e in sink_b)
            and any(e["status"] == "waiting_qr" for e in status_b)
            and any(e["status"] == "expired" for e in status_b),
            str([(e.get("type"), e.get("status")) for e in sink_b]),
        )
        check(
            "login 模式: 超时路径不误报“未截获二维码”failed",
            not any(e["status"] == "failed" and "未截获二维码" in e.get("message", "") for e in status_b),
        )

        # C. 扫码成功：确实走过 login.begin -> LoginOnlyDone；confirmed 由仪器化 check_login_state 发
        def fake_instrument(mc, emitter, platform, state=None, manual=None):
            if state is not None:
                state["login_attempted"] = True
                state["confirmed"] = True
            emitter.status("confirmed", "登录成功")

        class FakeCrawlerScanned:
            async def start(self):
                raise patches.LoginOnlyDone()

        patches.instrument_login_class = fake_instrument
        patches.install_qr_capture = lambda *a, **k: types.SimpleNamespace(count=1)
        sink_c = []
        emitter_c = patches.EventEmitter("xhs", "login", sink_c.append)
        run_async(entry._login_async(fake_mc(lambda platform: FakeCrawlerScanned()), args, tmp, emitter_c))
        status_c = [e for e in sink_c if e["type"] == "login.status"]
        check(
            "login 模式: 扫码成功 -> confirmed，且无任何 failed 状态",
            any(e["status"] == "confirmed" for e in status_c) and not any(e["status"] == "failed" for e in status_c),
            str([e.get("status") for e in status_c]),
        )
    finally:
        for name, original in saved.items():
            setattr(patches, name, original)


# --------------------------------------------------------------------------------------
# 7b. 登录类仪器化：begin 标记 login_attempted；check_login_state 成功 -> confirmed
# --------------------------------------------------------------------------------------
def test_login_class_instrumentation() -> None:
    import types

    mc = patches.import_mediacrawler(patches.resolve_mc_root())
    login_cls = patches._login_class("xhs")
    sink = []
    emitter = patches.EventEmitter("xhs", "login", sink.append)
    state = {"login_attempted": False, "confirmed": False}

    class FakePage:
        async def is_visible(self, selector, timeout=500):
            return False

        async def content(self):
            return ""

    class FakeBrowserContext:
        async def cookies(self, urls=None):
            return [{"name": "web_session", "value": "NEW-SESSION", "domain": ".xiaohongshu.com", "path": "/"}]

    patches.instrument_login_class(mc, emitter, "xhs", state)
    fake_self = types.SimpleNamespace(context_page=FakePage(), browser_context=FakeBrowserContext())

    try:
        run_async(login_cls.begin(fake_self))
        begin_blocked = False
    except Exception:
        begin_blocked = True  # 之前的 patch_login_blocked 已把 begin 换成 NotLoggedIn，属预期
    check("instrument: begin() 被包装并标记 login_attempted=True", begin_blocked and state["login_attempted"] is True)

    ok = run_async(login_cls.check_login_state(fake_self, "OLD-SESSION"))
    confirmed = [e for e in sink if e["type"] == "login.status" and e["status"] == "confirmed"]
    check("instrument: check_login_state 成功 -> confirmed 且 state.confirmed=True", ok is True and bool(confirmed) and state["confirmed"] is True)


# --------------------------------------------------------------------------------------
# 7c. 有头默认 / 验证页检测 / 人工接管闸门 / 导航重试 / 二维码兜底
# --------------------------------------------------------------------------------------
class FakeVerifyPage:
    """最小页面假对象：供 detect_verification / goto 等离线测试。"""

    def __init__(self, title="", url="https://example.com/", text="", content=""):
        self.url = url
        self._title = title
        self._text = text
        self._content = content

    async def title(self):
        return self._title

    async def evaluate(self, script):
        return self._text

    async def content(self):
        return self._content


def test_headless_resolution() -> None:
    check("headless: login 默认有头（False）", entry.resolve_headless("login", None, env={}) is False)
    check("headless: login --headless true 显式无头（探针用）", entry.resolve_headless("login", True, env={}) is True)
    check(
        "headless: ENGINE_LOGIN_HEADLESS=1 强制无头（即使 --headless false）",
        entry.resolve_headless("login", False, env={"ENGINE_LOGIN_HEADLESS": "1"}) is True,
    )
    check("headless: test/crawl 默认无头（True）", entry.resolve_headless("test", None, env={}) is True and entry.resolve_headless("crawl", None, env={}) is True)
    args = entry.parse_args(["--mode", "login", "--platform", "xhs", "--data-dir", r"C:\tmp\x"])
    check("headless: 未显式传 --headless 时 parse_args 保留 None，交由 mode 默认值决定", args.headless is None)


def test_verification_detection() -> None:
    dy = FakeVerifyPage(title="验证码中间页")
    info = run_async(patches.detect_verification(dy))
    check("验证页检测: 抖音 title=验证码中间页 -> slider", bool(info) and info["kind"] == "slider", str(info))

    tieba = FakeVerifyPage(title="百度安全验证")
    info = run_async(patches.detect_verification(tieba))
    check("验证页检测: 贴吧 title=百度安全验证 -> slider", bool(info) and info["kind"] == "slider", str(info))

    xhs_sms = FakeVerifyPage(title="小红书", text="短信验证码\n请输入短信验证码")
    info = run_async(patches.detect_verification(xhs_sms))
    check("验证页检测: 可见文本含短信验证码 -> sms", bool(info) and info["kind"] == "sms", str(info))

    normal = FakeVerifyPage(title="小红书 - 你的生活兴趣社区", text="登录 扫码登录 验证码登录")
    check("验证页检测: 正常登录页（仅隐藏表单文案）不误报", run_async(patches.detect_verification(normal)) is None)

    phone_form = FakeVerifyPage(title="小红书", text="手机号登录\n获取验证码\n请输入验证码\n接收验证码")
    check(
        "验证页检测: 普通手机号登录表单（获取验证码）不误判为短信二次验证",
        run_async(patches.detect_verification(phone_form)) is None,
    )

    weibo_tabs = FakeVerifyPage(title="登录 - 微博", text="短信验证登录 | 账号密码登录 | 获取验证码 | 扫码登录")
    check(
        "验证页检测: 微博登录页“短信验证登录”tab 不误判为短信二次验证",
        run_async(patches.detect_verification(weibo_tabs)) is None,
    )

    class _FakeLocator:
        def __init__(self, count=1, visible=True):
            self._count = count
            self._visible = visible
            self.first = self

        async def count(self):
            return self._count

        async def is_visible(self):
            return self._visible

    class FakeSmsInputPage(FakeVerifyPage):
        def locator(self, selector):
            return _FakeLocator(1, True)

    sms_input_page = FakeSmsInputPage(title="验证", text="为了你的账号安全")
    info = run_async(patches.detect_verification(sms_input_page))
    check("验证页检测: 仅可见验证码输入框/普通登录页 -> 不触发 sms（lead 要求）", info is None, str(info))

    url_verify = FakeVerifyPage(title="", url="https://passport.example.com/verify?x=1", text="")
    info = run_async(patches.detect_verification(url_verify))
    check("验证页检测: URL 含 verify -> captcha", bool(info) and info["kind"] in ("slider", "captcha"), str(info))


def test_sms_secondary_gating() -> None:
    class _FakeLocator:
        def __init__(self, count=0, visible=False):
            self._count = count
            self._visible = visible
            self.first = self

        def nth(self, _index):
            return self

        async def count(self):
            return self._count

        async def is_visible(self):
            return self._visible

        async def bounding_box(self):
            if not self._visible:
                return None
            return {"width": 200.0, "height": 200.0, "x": 0.0, "y": 0.0}

    class FakeQRPage(FakeVerifyPage):
        def __init__(self, qr_visible, **kwargs):
            super().__init__(**kwargs)
            self.qr_visible = qr_visible

        def locator(self, selector):
            return _FakeLocator(1, True) if self.qr_visible else _FakeLocator(0, False)

    sms_text = "短信验证码\n请输入短信验证码"
    page_qr_visible = FakeQRPage(True, title="登录 - 微博", text=sms_text)
    check(
        "短信二次验证: 二维码仍可见（wb 正常登录页）-> 不触发",
        run_async(patches.should_trigger_sms(page_qr_visible, "wb", 1)) is False,
    )
    page_qr_gone = FakeQRPage(False, title="身份验证", text=sms_text)
    check(
        "短信二次验证: 已出码 + 二维码消失 + 专属文案 -> 触发",
        run_async(patches.should_trigger_sms(page_qr_gone, "xhs", 1)) is True,
    )
    check(
        "短信二次验证: 未出过二维码 -> 即便页面有短信文案也不触发",
        run_async(patches.should_trigger_sms(page_qr_gone, "xhs", 0)) is False,
    )
    weibo_page = FakeQRPage(True, title="登录 - 微博", text="短信验证登录 | 获取验证码 | 账号密码登录 | 扫码登录")
    check(
        "短信二次验证: 微博普通登录页（验证码登录 tab）不触发",
        run_async(patches.should_trigger_sms(weibo_page, "wb", 0)) is False,
    )


def test_need_manual_bring_to_front() -> None:
    sink = []
    emitter = patches.EventEmitter("tieba", "login", sink.append)
    gate = patches.ManualGate(emitter, "tieba", timeout=0.2)

    class FakePage:
        def __init__(self):
            self.calls = 0

        async def bring_to_front(self):
            self.calls += 1

    page = FakePage()
    first = run_async(gate.report_async({"kind": "slider", "reason": "拼图"}, page))
    second = run_async(gate.report_async({"kind": "slider", "reason": "拼图"}, page))
    need = [e for e in sink if e["type"] == "login.status" and e["status"] == "need_manual"]
    check(
        "need_manual: 首次触发调用 context_page.bring_to_front() 置前，同 kind 不重复",
        first is True and second is False and page.calls == 1 and bool(need),
        f"calls={page.calls}, need={len(need)}",
    )


def test_manual_gate_events() -> None:
    sink = []
    emitter = patches.EventEmitter("xhs", "login", sink.append)
    gate = patches.ManualGate(emitter, "xhs", timeout=0.2)
    gate.report({"kind": "sms", "reason": "短信验证码"})
    gate.report({"kind": "sms", "reason": "短信验证码"})  # 20s 内同 kind 去重
    need = [e for e in sink if e["type"] == "login.status" and e["status"] == "need_manual"]
    check(
        "ManualGate: need_manual 事件带 kind=sms + 窗口/短信提示，且同 kind 去重",
        len(need) == 1 and need[0].get("kind") == "sms" and "窗口" in need[0]["message"] and "短信" in need[0]["message"],
        str(need),
    )
    check(
        "ManualGate: login.status 字段=契约字段+可选 kind（兼容 sidecar）",
        bool(need) and set(need[0].keys()) == patches.EVENT_FIELDS["login.status"] | {"kind"},
    )
    gate.report(None)
    waiting = [e for e in sink if e["type"] == "login.status" and e["status"] == "waiting_scan"]
    check("ManualGate: 验证完成后发 waiting_scan“继续登录”", bool(waiting) and "验证已完成" in waiting[-1]["message"], str(waiting))


def test_manual_wait_clear() -> None:
    original_detect = patches.detect_verification
    try:
        seq = [{"kind": "slider"}, {"kind": "slider"}, None, None, None]
        calls = {"n": 0}

        async def fake_detect(page):
            calls["n"] += 1
            if calls["n"] - 1 < len(seq):
                return seq[calls["n"] - 1]
            return None

        patches.detect_verification = fake_detect
        sink = []
        emitter = patches.EventEmitter("xhs", "login", sink.append)
        gate = patches.ManualGate(emitter, "xhs", timeout=2)
        cleared = run_async(gate.wait_clear(object(), timeout=2, poll=0.01))
        statuses = [e.get("status") for e in sink if e["type"] == "login.status"]
        check("ManualGate.wait_clear: 验证页消失后返回 True", cleared is True)
        check(
            "ManualGate.wait_clear: 先 need_manual 后 waiting_scan",
            "need_manual" in statuses and "waiting_scan" in statuses,
            str(statuses),
        )

        async def always_verify(page):
            return {"kind": "captcha"}

        patches.detect_verification = always_verify
        sink2 = []
        gate2 = patches.ManualGate(patches.EventEmitter("xhs", "login", sink2.append), "xhs", timeout=0.15)
        timed_out = run_async(gate2.wait_clear(object(), timeout=0.15, poll=0.02))
        check("ManualGate.wait_clear: 一直验证中 -> 超时返回 False", timed_out is False)
    finally:
        patches.detect_verification = original_detect


def test_wait_manual_before_begin() -> None:
    original_detect = patches.detect_verification
    try:
        calls = {"n": 0}

        async def fake_detect(page):
            calls["n"] += 1
            return {"kind": "slider", "reason": "拼图"} if calls["n"] <= 3 else None

        patches.detect_verification = fake_detect
        sink = []
        emitter = patches.EventEmitter("tieba", "login", sink.append)
        gate = patches.ManualGate(emitter, "tieba", timeout=2)
        ok = run_async(patches.wait_manual_before_begin(object(), emitter, "tieba", gate, poll=0.01))
        need = [e for e in sink if e["type"] == "login.status" and e["status"] == "need_manual"]
        check("begin 前人工接管: 检测到整页验证 -> 等待人工完成后返回 True", ok is True and bool(need), str([e.get("status") for e in sink]))

        # 登录前若只检测到短信验证表单，不阻塞登录（短信二次验证只在出码后处理）
        async def fake_detect_sms(page):
            return {"kind": "sms", "reason": "短信验证码"}

        patches.detect_verification = fake_detect_sms
        sink2 = []
        emitter2 = patches.EventEmitter("xhs", "login", sink2.append)
        gate2 = patches.ManualGate(emitter2, "xhs", timeout=0.2)
        ok2 = run_async(patches.wait_manual_before_begin(object(), emitter2, "xhs", gate2, poll=0.01))
        check(
            "begin 前人工接管: 仅短信 kind 不阻塞登录（避免小红书首页误判卡住）",
            ok2 is True and not any(e.get("status") == "need_manual" for e in sink2),
            str([e.get("status") for e in sink2]),
        )
    finally:
        patches.detect_verification = original_detect


def test_goto_retry() -> None:
    from playwright.async_api import Page

    sink = []
    emitter = patches.EventEmitter("wb", "login", sink.append)
    info = patches.install_goto_retry(emitter, attempts=3, base_delay=0)
    check("goto 重试: install_goto_retry 返回配置", info.get("installed") is True and info.get("attempts") == 3)
    original = patches._PAGE_GOTO_PATCH.get("original")
    counters = {"n": 0}
    fail_times = {"n": 2}

    async def flaky_goto(fake_self, url, *args, **kwargs):
        counters["n"] += 1
        if counters["n"] <= fail_times["n"]:
            raise RuntimeError("net::ERR_CONNECTION_CLOSED at https://passport.weibo.com/")
        return "OK"

    patches._PAGE_GOTO_PATCH["original"] = flaky_goto
    try:
        result = run_async(Page.goto(object(), "https://passport.weibo.com/sso/signin"))
        progress = [e for e in sink if e["type"] == "progress"]
        check("goto 重试: 连续 2 次 ERR_CONNECTION_CLOSED 后第 3 次成功", result == "OK" and counters["n"] == 3)
        check("goto 重试: 重试过程产生 progress 事件（可见）", len(progress) >= 2, str([e.get("message") for e in progress]))
        check(
            "goto 重试: progress message 含“导航失败/重试”",
            any("导航失败" in (e.get("message") or "") for e in progress),
        )

        counters["n"] = 0

        async def hard_fail_goto(fake_self, url, *args, **kwargs):
            counters["n"] += 1
            raise RuntimeError("some non retryable error")

        patches._PAGE_GOTO_PATCH["original"] = hard_fail_goto
        raised = False
        try:
            run_async(Page.goto(object(), "https://example.com/"))
        except RuntimeError:
            raised = True
        check("goto 重试: 非网络类错误不重试、立即抛出", raised and counters["n"] == 1)
    finally:
        if original is not None:
            patches._PAGE_GOTO_PATCH["original"] = original


def test_qr_fallback() -> None:
    import types

    png_b64 = base64.b64encode(synth_png("fallback")).decode()

    class FakeElement:
        def __init__(self, src=None, shot=None):
            self.src = src
            self.shot = shot

        async def get_property(self, name):
            return self.src

        async def screenshot(self, timeout=5000):
            return self.shot

    class FakeFallbackPage:
        def __init__(self, mapping):
            self.mapping = mapping

        async def query_selector(self, selector):
            return self.mapping.get(selector)

    # 贴吧候选现在要求 cv2 可解码（防占位图/横幅），用真实可解码二维码
    tieba_qr_b64 = base64.b64encode(make_decodable_qr_png("https://tieba.baidu.com/qr-login")).decode()
    page = FakeFallbackPage({"img.tang-pass-qrcode-img": FakeElement(src="data:image/png;base64," + tieba_qr_b64)})
    result = run_async(patches.extract_qr_fallback(page, "tieba"))
    check("QR fallback: 贴吧 tang-pass-qrcode 变体 data:image 命中（cv2 可解码）", result.startswith("data:image") and tieba_qr_b64 in result)

    page2 = FakeFallbackPage({"img[class*='qrcode']": FakeElement(shot=synth_png("fallback-shot"))})
    result2 = run_async(patches.extract_qr_fallback(page2, "dy"))
    check("QR fallback: 元素 screenshot 兜底可用", result2.startswith("data:image/png;base64,") and len(result2) > 64)

    # install_qr_fallback：原生选择器短等待失败 -> 兜底选择器返回
    async def original_find(fake_page, selector):
        return ""

    async def original_canvas(fake_page, selector):
        return ""

    fake_mc = {
        "utils": types.SimpleNamespace(find_login_qrcode=original_find, find_qrcode_img_from_canvas=original_canvas),
        "crawler_util": types.SimpleNamespace(find_login_qrcode=original_find, find_qrcode_img_from_canvas=original_canvas),
    }
    sink = []
    emitter = patches.EventEmitter("xhs", "login", sink.append)
    manual = patches.ManualGate(emitter, "xhs", timeout=0.2)
    patches.install_qr_fallback(fake_mc, emitter, "xhs", manual)

    class PageForWrapper(FakeVerifyPage):
        async def wait_for_selector(self, selector, timeout=None):
            raise RuntimeError("Timeout 8000ms exceeded")

        async def query_selector(self, selector):
            if selector == "img.qrcode-img":
                return FakeElement(src="data:image/png;base64," + png_b64)
            return None

    wrapper = fake_mc["utils"].find_login_qrcode
    wrapped_result = run_async(wrapper(PageForWrapper(), "xpath=//img[@class='qrcode-img']"))
    progress = [e for e in sink if e["type"] == "progress"]
    check("QR fallback wrapper: 原生选择器短等待超时后使用兜底并返回", wrapped_result.startswith("data:image"))
    check("QR fallback wrapper: 兜底命中产生 progress 事件", bool(progress), str([e.get("message") for e in progress]))


# --------------------------------------------------------------------------------------
# 7e. 二维码图片有效性 / naturalWidth 等待 / 截图优先 / 3 轮失败
# --------------------------------------------------------------------------------------
def make_solid_png(size=(180, 180), color=(255, 255, 255)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def make_gradient_banner_png(size=(948, 168)) -> bytes:
    """模拟贴吧 948x168 淡蓝渐变横幅（非正方形，有方差但绝不是二维码）。"""
    from PIL import Image

    width, height = size
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(0, width, 8):
            value = int(220 + (x / max(1, width)) * 30)
            for dx in range(8):
                if x + dx < width:
                    pixels[x + dx, y] = (value - 20, value, 255)
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def make_decodable_qr_png(text: str = "https://example.com/qr-test", scale: int = 10) -> bytes:
    """用 cv2.QRCodeEncoder 生成真实可解码二维码（用于 cv2 校验分支测试）。"""
    import cv2
    import numpy as np

    encoder = cv2.QRCodeEncoder_create()
    modules = encoder.encode(text)
    qr = np.pad(modules, 4, constant_values=255)
    qr = cv2.resize(qr, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    ok, buffer = cv2.imencode(".png", qr)
    if not ok:
        raise RuntimeError("cv2 QR encode failed")
    return buffer.tobytes()


def make_pattern_png(size=(100, 100), seed: int = 7) -> bytes:
    """生成任意宽高比的高对比条纹图（用于宽高比边界测试）。"""
    import random

    from PIL import Image, ImageDraw

    width, height = size
    rnd = random.Random(seed)
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 6):
        for x in range(0, width, 6):
            if rnd.getrandbits(1):
                draw.rectangle((x, y, min(x + 5, width - 1), min(y + 5, height - 1)), fill=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def test_qr_image_validation() -> None:
    valid = synth_png("valid-qr-for-validation")
    info = patches.validate_qr_bytes(valid)
    check(
        "QR 校验: 正常二维码通过（128x128，std 高于阈值）",
        bool(info.get("ok")) and info.get("width") == 128 and info.get("height") == 128 and info.get("std", 0) > 20,
        str(info),
    )
    blank = make_solid_png((180, 180), (255, 255, 255))
    info = patches.validate_qr_bytes(blank)
    check("QR 校验: 纯白空白图判无效", info.get("ok") is False and "blank" in str(info.get("reason")), str(info))
    tiny = make_solid_png((40, 40), (255, 255, 255))
    info = patches.validate_qr_bytes(tiny)
    check("QR 校验: 过小图（40x40）判无效", info.get("ok") is False and "size" in str(info.get("reason")), str(info))
    info = patches.validate_qr_bytes(b"not-an-image")
    check("QR 校验: 非图片内容判无效", info.get("ok") is False and "decode" in str(info.get("reason")), str(info))
    ok, uri, info = patches.validate_qr_data_uri("data:image/png;base64," + base64.b64encode(valid).decode())
    check("QR 校验: data URI 有效时返回规范化 data:image", ok is True and uri.startswith("data:image/png;base64,"))

    real_qr = make_decodable_qr_png("https://example.com/real-qr")
    info = patches.validate_qr_bytes(real_qr)
    check(
        "QR 校验: cv2 可解码二维码通过且 decoded=True",
        bool(info.get("ok")) and info.get("decoded") is True and 0.75 <= float(info.get("aspect", 0)) <= 1.35,
        str(info),
    )
    banner = make_gradient_banner_png((948, 168))
    info = patches.validate_qr_bytes(banner)
    check(
        "QR 校验: 贴吧 948x168 横幅因宽高比被拒绝",
        info.get("ok") is False and "aspect" in str(info.get("reason")),
        str(info),
    )
    aspect_cases = [((80, 100), True), ((100, 80), True), ((80, 120), False), ((80, 50), False)]
    aspect_ok = True
    detail = []
    for size, expected in aspect_cases:
        info = patches.validate_qr_bytes(make_pattern_png(size))
        detail.append((size, info.get("ok"), info.get("reason")))
        if bool(info.get("ok")) is not expected:
            aspect_ok = False
    check("QR 校验: 宽高比 0.75~1.35 边界（80x100/100x80 通过，80x120/80x50 拒绝）", aspect_ok, str(detail))

    original_decode = patches._cv2_decode_qr
    patches._cv2_decode_qr = lambda raw: None
    try:
        info = patches.validate_qr_bytes(synth_png("non-decodable-but-square"))
    finally:
        patches._cv2_decode_qr = original_decode
    check(
        "QR 校验: cv2 解码失败时正方形理想图降级接受（decoded=False）",
        bool(info.get("ok")) and info.get("decoded") is False,
        str(info),
    )


def test_cv2_quiet_zone_decode() -> None:
    """紧贴裁切的二维码截图（无静区）经补白边后仍能 cv2 解码（贴吧 passport 实测场景）。"""
    from PIL import Image

    full = make_decodable_qr_png("https://example.com/quiet-zone", scale=8)
    image = Image.open(io.BytesIO(full))
    image.load()
    border = 4 * 8  # make_decodable_qr_png 的 pad=4 modules * scale=8
    cropped = image.crop((border, border, image.width - border, image.height - border))
    buf = io.BytesIO()
    cropped.save(buf, "PNG")
    tight = buf.getvalue()
    decoded = patches._cv2_decode_qr(tight)
    info = patches.validate_qr_bytes(tight)
    check(
        "cv2 解码: 无静区紧裁截图补白边后可解码（tieba passport QR 场景）",
        bool(decoded) and info.get("decoded") is True,
        f"decoded={bool(decoded)}, size={cropped.size}, info={info}",
    )


def test_qr_capture_rejects_invalid() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mc_engine_qr_valid_"))
    sink = []
    emitter = patches.EventEmitter("wb", "login", sink.append)
    capture = patches.QrCapture(emitter, tmp, "wb")
    blank = make_solid_png((180, 180))
    capture("data:image/png;base64," + base64.b64encode(blank).decode())
    events = [e for e in sink if e["type"] == "login.qrcode"]
    check(
        "QrCapture: 空白图不发送 login.qrcode（invalid_count/原因记录）",
        not events and capture.count == 0 and capture.invalid_count == 1 and bool(capture.last_invalid),
        f"count={capture.count}, invalid={capture.invalid_count}",
    )
    valid = synth_png("qr-capture-valid")
    capture("data:image/png;base64," + base64.b64encode(valid).decode())
    events = [e for e in sink if e["type"] == "login.qrcode"]
    check("QrCapture: 有效图发送 login.qrcode 并落盘", len(events) == 1 and capture.count == 1 and Path(events[0]["file"]).is_file())


def test_qr_capture_tieba_strict() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="mc_engine_tieba_strict_"))
    sink = []
    emitter = patches.EventEmitter("tieba", "login", sink.append)
    capture = patches.QrCapture(emitter, tmp, "tieba")
    capture("data:image/png;base64," + base64.b64encode(synth_png("tieba-placeholder-like")).decode())
    events = [e for e in sink if e["type"] == "login.qrcode"]
    check(
        "QrCapture(贴吧): 非 cv2 可解码的占位图不发 login.qrcode",
        not events and capture.count == 0 and "tieba_not_decodable" in str((capture.last_invalid or {}).get("reason")),
        str(capture.last_invalid),
    )
    real_qr = make_decodable_qr_png("https://tieba.baidu.com/qr-strict")
    capture("data:image/png;base64," + base64.b64encode(real_qr).decode())
    events = [e for e in sink if e["type"] == "login.qrcode"]
    check("QrCapture(贴吧): 真实可解码二维码正常发送", len(events) == 1 and capture.count == 1)


def test_wait_image_natural_width() -> None:
    class FakeImg:
        def __init__(self, sequence):
            self.sequence = list(sequence)
            self.calls = 0

        async def evaluate(self, _script):
            self.calls += 1
            index = min(self.calls - 1, len(self.sequence) - 1)
            return self.sequence[index]

    loaded = FakeImg(
        [
            {"img": True, "complete": False, "nw": 0},
            {"img": True, "complete": False, "nw": 0},
            {"img": True, "complete": True, "nw": 180},
        ]
    )
    ok = run_async(patches._wait_image_loaded(loaded, timeout=2))
    check("naturalWidth: 未加载时等待，加载完成后返回 True", ok is True and loaded.calls >= 3, f"calls={loaded.calls}")
    never = FakeImg([{"img": True, "complete": False, "nw": 0}])
    ok = run_async(patches._wait_image_loaded(never, timeout=0.2))
    check("naturalWidth: 始终未加载则超时返回 False", ok is False)
    non_img = FakeImg([{"img": False}])
    check("naturalWidth: 非 img 元素直接视为可截图", run_async(patches._wait_image_loaded(non_img, timeout=0.2)) is True)


def test_http_src_prefers_screenshot() -> None:
    valid = synth_png("http-src-valid")
    blank = make_solid_png((180, 180))

    class FakeElement:
        def __init__(self, src, shot):
            self.src = src
            self.shot = shot

        async def get_property(self, _name):
            return self.src

        async def evaluate(self, _script):
            return {"img": True, "complete": True, "nw": 180}

        async def screenshot(self, timeout=5000):
            return self.shot

    calls = {"n": 0}

    async def fake_http_download(src, page):
        calls["n"] += 1
        return valid

    original = patches._http_download_qr
    patches._http_download_qr = fake_http_download
    try:
        qr, info = run_async(
            patches._qr_candidate_from_element(
                FakeElement("https://v2.qr.weibo.cn/inf/gen?x=1", valid), "page"
            )
        )
        check(
            "http(s) src: 优先 Playwright 元素截图，截图有效时不再走 httpx",
            bool(qr) and info.get("ok") is True and calls["n"] == 0,
            f"http_calls={calls['n']}",
        )
        qr2, info2 = run_async(
            patches._qr_candidate_from_element(
                FakeElement("https://v2.qr.weibo.cn/inf/gen?x=1", blank), "page"
            )
        )
        check(
            "http(s) src: 截图无效时回退 httpx 下载（仍做 PIL 校验）",
            bool(qr2) and info2.get("ok") is True and calls["n"] == 1,
            f"http_calls={calls['n']}",
        )
    finally:
        patches._http_download_qr = original


def test_qr_three_rounds_failure() -> None:
    import types

    async def none_find(_page, _selector):
        return ""

    fake_mc = {
        "utils": types.SimpleNamespace(find_login_qrcode=none_find, find_qrcode_img_from_canvas=none_find),
        "crawler_util": types.SimpleNamespace(find_login_qrcode=none_find, find_qrcode_img_from_canvas=none_find),
    }
    sink = []
    emitter = patches.EventEmitter("wb", "login", sink.append)
    manual = patches.ManualGate(emitter, "wb", timeout=0.1)
    patches.install_qr_fallback(fake_mc, emitter, "wb", manual)

    class FailPage:
        url = "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog"

        def __init__(self):
            self.reloads = 0

        async def title(self):
            return "登录 - 微博"

        async def evaluate(self, _script):
            return ""

        async def content(self):
            return ""

        async def query_selector(self, _selector):
            return None

        async def wait_for_selector(self, _selector, timeout=None):
            raise RuntimeError("Timeout")

        async def reload(self, **_kwargs):
            self.reloads += 1
            return None

        async def goto(self, _url, **_kwargs):
            self.reloads += 1
            return None

    page = FailPage()
    raised = False
    error_text = ""
    try:
        run_async(fake_mc["utils"].find_login_qrcode(page, "xpath=//img[@class='w-full h-full']"))
    except patches.QrLoadFailed as exc:
        raised = True
        error_text = str(exc)
    failed = [e for e in sink if e["type"] == "login.status" and e["status"] == "failed"]
    check(
        "QR 3 轮失败: 抛 QrLoadFailed，且 login.status failed 提示网络/代理拦截",
        raised and "二维码加载失败" in error_text and "网络/代理" in error_text,
        error_text,
    )
    check(
        "QR 3 轮失败: 失败事件字段保持契约（不改字段名）",
        len(failed) == 1 and set(failed[0].keys()) == patches.EVENT_FIELDS["login.status"],
        str(failed),
    )
    check("QR 3 轮失败: 至少重载/重新导航登录页 2 次（3 轮取码）", page.reloads >= 2, f"reloads={page.reloads}")


def test_weibo_desktop_qr_page_switch() -> None:
    """微博二维码空白修复：切桌面 UA（CDP）+ 打开 popup 登录页，失败时回退 extra UA header。"""
    sent = []

    class FakeCdp:
        async def send(self, method, params):
            sent.append((method, params))

    class FakeContext:
        def __init__(self, fail=False):
            self.fail = fail

        async def new_cdp_session(self, _page):
            if self.fail:
                raise RuntimeError("CDP not available")
            return FakeCdp()

    class FakeLocator:
        def __init__(self):
            self.first = self

        async def count(self):
            return 0

        async def is_visible(self):
            return False

    class FakePage:
        def __init__(self, cdp_fail=False):
            self.context = FakeContext(fail=cdp_fail)
            self.url = "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog"
            self.goto_calls = []
            self.extra_headers = []

        async def goto(self, url, **_kwargs):
            self.goto_calls.append(url)

        def locator(self, _selector):
            return FakeLocator()

        async def set_extra_http_headers(self, headers):
            self.extra_headers.append(headers)

    page = FakePage()
    ok = run_async(patches._switch_weibo_to_desktop_qr_page(page))
    check(
        "微博修复: 通过 CDP 设置桌面 UA 并打开 popup 登录页",
        ok is True
        and bool(sent)
        and sent[0][0] == "Network.setUserAgentOverride"
        and sent[0][1].get("userAgent") == patches.WEIBO_QR_USER_AGENT
        and page.goto_calls
        and page.goto_calls[0] == patches.WEIBO_QR_LOGIN_URL,
        f"cdp={sent[:1]}, goto={page.goto_calls[:1]}",
    )
    goto_count = len(page.goto_calls)
    run_async(patches._switch_weibo_to_desktop_qr_page(page))
    check("微博修复: 同一 page 只切换一次（幂等，不重复导航）", len(page.goto_calls) == goto_count)

    fallback_page = FakePage(cdp_fail=True)
    ok2 = run_async(patches._switch_weibo_to_desktop_qr_page(fallback_page))
    check(
        "微博修复: CDP 不可用时回退 extra UA header 并仍打开 popup 页",
        ok2 is True and bool(fallback_page.extra_headers) and fallback_page.extra_headers[0].get("User-Agent") == patches.WEIBO_QR_USER_AGENT,
        str(fallback_page.extra_headers[:1]),
    )


def test_candidate_geometry_filter() -> None:
    square_b64 = base64.b64encode(synth_png("geometry-square")).decode()

    class FakeElement:
        def __init__(self, box):
            self.box = box

        async def is_visible(self):
            return True

        async def bounding_box(self):
            return self.box

        async def get_property(self, _name):
            return "data:image/png;base64," + square_b64

    class FakePage:
        def __init__(self, mapping):
            self.mapping = mapping

        async def query_selector(self, selector):
            return self.mapping.get(selector)

        async def wait_for_selector(self, selector, timeout=None):
            return self.mapping.get(selector)

    banner = FakeElement({"width": 304, "height": 56, "x": 0, "y": 0})
    square = FakeElement({"width": 141, "height": 141, "x": 0, "y": 0})
    page = FakePage({"img.extra": banner, "img.qrcode-img": square})
    qr, info = run_async(patches.inspect_qr_candidates(page, "xhs", extra_selectors=["img.extra"]))
    check(
        "候选几何过滤: 948x168 横幅（实际 304x56 box）被跳过，选正方形二维码",
        bool(qr) and info.get("width") == 128,
        str(info),
    )

    page_only_banner = FakePage({"img.extra": banner})
    qr2, info2 = run_async(patches.inspect_qr_candidates(page_only_banner, "xhs", extra_selectors=["img.extra"]))
    check(
        "候选几何过滤: 只有非正方形横幅时不误截容器（返回空并记录 box 几何原因）",
        qr2 == "" and "box_" in str(info2.get("reason")),
        str(info2),
    )


def test_tieba_passport_page_switch() -> None:
    class FakePage:
        def __init__(self):
            self.goto_calls = []

        async def goto(self, url, **_kwargs):
            self.goto_calls.append(url)

    page = FakePage()
    ok = run_async(patches._switch_tieba_to_passport_qr_page(page))
    check(
        "贴吧修复: 打开百度 passport 二维码页（避免首页横幅）",
        ok is True and bool(page.goto_calls) and page.goto_calls[0] == patches.TIEBA_QR_LOGIN_URL,
        str(page.goto_calls[:1]),
    )
    goto_count = len(page.goto_calls)
    run_async(patches._switch_tieba_to_passport_qr_page(page))
    check("贴吧修复: 同一 page 只切换一次（幂等）", len(page.goto_calls) == goto_count)


def test_douyin_click_robustness() -> None:
    import types

    class FakeLocator:
        def __init__(self, page, selector, kind):
            self.page = page
            self.selector = selector
            self.kind = kind
            self.first = self

        async def count(self):
            if self.kind == "panel":
                return 1
            return 1

        async def is_visible(self):
            if self.kind == "panel":
                return self.page.panel_visible
            return True

        async def click(self, timeout=None, force=False):
            self.page.click_calls.append((self.selector, timeout, force))
            if not force:
                raise RuntimeError("intercepts pointer events")
            self.page.panel_visible = True

    class FakeKeyboard:
        def __init__(self, page):
            self.page = page

        async def press(self, key):
            self.page.esc_presses += 1

    class FakePage:
        def __init__(self):
            self.panel_visible = False
            self.click_calls = []
            self.esc_presses = 0
            self.keyboard = FakeKeyboard(self)

        def locator(self, selector):
            is_panel = (
                selector.startswith("div#login-panel-new")
                or "login-full-panel" in selector
                or "animate_qrcode" in selector
                or "login-panel" in selector
            )
            return FakeLocator(self, selector, "panel" if is_panel else "login")

        async def evaluate(self, *_args, **_kwargs):
            return False

    page = FakePage()
    fake_self = types.SimpleNamespace(context_page=page)
    run_async(patches._robust_douyin_popup_login_dialog(fake_self, initial_wait_ms=20))
    short_timeouts = [timeout for _sel, timeout, _force in page.click_calls if timeout is not None]
    check(
        "dy 点击兜底: 浮层拦截时短超时+ESC+force 后打开登录面板，不抛 30s 异常",
        page.panel_visible is True
        and page.esc_presses >= 1
        and bool(short_timeouts)
        and max(short_timeouts) <= 3000,
        f"clicks={page.click_calls}, esc={page.esc_presses}",
    )

    page2 = FakePage()
    page2.panel_visible = True
    run_async(patches._robust_douyin_popup_login_dialog(types.SimpleNamespace(context_page=page2), initial_wait_ms=20))
    check("dy 点击兜底: 面板已打开时不再重复点击", page2.click_calls == [])


def test_kuaishou_search_patch() -> None:
    """快手搜索：首屏 pcursor=""/webPageArea=""，后续页复用服务端游标；env 可回退。"""
    import os

    calls = []
    signed = []

    class FakeClient:
        async def search_info_by_keyword_v2(self, keyword, pcursor, search_session_id=""):
            calls.append(("original", keyword, pcursor, search_session_id))
            return {"result": 1, "feeds": []}

        async def request_rest_v2_signed(self, uri, payload):
            signed.append((uri, dict(payload)))
            if len(signed) == 1:
                return {
                    "result": 1,
                    "feeds": [
                        {"photo": {"id": "a", "caption": "某某示例科技：守护城市安全"}},
                        {"photo": {"id": "x", "caption": "无人机吊运推荐"}},
                    ],
                    "pcursor": "PAGE2-CURSOR",
                    "searchSessionId": "SESS-1",
                }
            return {
                "result": 1,
                "feeds": [{"photo": {"id": "b", "caption": "示例科技董事长示例人物"}}],
                "pcursor": "PAGE3-CURSOR",
                "searchSessionId": "SESS-1",
            }

    previous = os.environ.pop("ENGINE_KS_SEARCH_WEB_STYLE", None)
    try:
        patched = patches.patch_kuaishou_search(FakeClient)
        check("快手搜索补丁: 默认安装成功", patched is True)
        client = FakeClient()
        response_first = run_async(client.search_info_by_keyword_v2("示例科技", "1"))
        check(
            "快手搜索补丁: 首屏响应过滤推荐补位（in=2 out=1）",
            len(response_first.get("feeds") or []) == 1
            and "示例" in ((response_first["feeds"][0].get("photo") or {}).get("caption") or ""),
            str(response_first.get("feeds")),
        )
        check(
            "快手搜索补丁: 首屏 payload 为网页真实格式（pcursor=\"\", webPageArea=\"\"）",
            len(signed) == 1
            and signed[0][0] == patches.KS_SEARCH_WEB_URI
            and signed[0][1] == {"keyword": "示例科技", "page": "search", "webPageArea": "", "pcursor": ""},
            str(signed[:1]),
        )
        check("快手搜索补丁: 首屏不再走 MediaCrawler 原 pcursor=\"1\" 请求", calls == [])
        run_async(client.search_info_by_keyword_v2("示例科技", "2"))
        check(
            "快手搜索补丁: 第 2 页复用服务端返回的 pcursor/searchSessionId",
            len(signed) == 2
            and signed[1][1].get("pcursor") == "PAGE2-CURSOR"
            and signed[1][1].get("searchSessionId") == "SESS-1",
            str(signed[1][1]),
        )
        run_async(client.search_info_by_keyword_v2("无人机", "1"))
        check(
            "快手搜索补丁: 新 keyword 重新走首屏（pcursor=\"\"）",
            signed[-1][1].get("keyword") == "无人机" and signed[-1][1].get("pcursor") == "",
            str(signed[-1][1]),
        )
        check("快手搜索补丁: 已安装时重复调用幂等", patches.patch_kuaishou_search(FakeClient) is True)

        class FakeClientDisabled:
            async def search_info_by_keyword_v2(self, keyword, pcursor, search_session_id=""):
                return {"result": 1, "feeds": []}

            async def request_rest_v2_signed(self, uri, payload):  # pragma: no cover
                return {}

        os.environ["ENGINE_KS_SEARCH_WEB_STYLE"] = "0"
        try:
            disabled = patches.patch_kuaishou_search(FakeClientDisabled)
        finally:
            os.environ.pop("ENGINE_KS_SEARCH_WEB_STYLE", None)
        check(
            "快手搜索补丁: ENGINE_KS_SEARCH_WEB_STYLE=0 时禁用（不包装）",
            disabled is False and getattr(FakeClientDisabled, "_engine_ks_search_patched", False) is False,
        )
    finally:
        if previous is not None:
            os.environ["ENGINE_KS_SEARCH_WEB_STYLE"] = previous


def test_kuaishou_relevance_filter() -> None:
    """快手相关过滤：token=keyword+去通用后缀核心词+env extra；命中 caption/昵称；大小写不敏感。"""
    import os

    tokens = patches._ks_relevance_tokens("示例科技")
    check(
        "相关词: keyword 本身 + 去“科技”核心词",
        tokens[:2] == ["示例科技", "示例"],
        str(tokens),
    )
    tokens2 = patches._ks_relevance_tokens("某某示例科技有限公司")
    check("相关词: 连续去“有限公司/科技”等后缀得到某某示例", "某某示例" in tokens2, str(tokens2))

    previous = os.environ.pop("ENGINE_KS_RELEVANCE_EXTRA", None)
    try:
        os.environ["ENGINE_KS_RELEVANCE_EXTRA"] = "示例人物,某某示例"
        tokens3 = patches._ks_relevance_tokens("示例科技")
        check("相关词: ENGINE_KS_RELEVANCE_EXTRA 追加（中英文逗号）", "示例人物" in tokens3 and "某某示例" in tokens3, str(tokens3))
    finally:
        os.environ.pop("ENGINE_KS_RELEVANCE_EXTRA", None)
        if previous is not None:
            os.environ["ENGINE_KS_RELEVANCE_EXTRA"] = previous

    feeds = [
        {"photo": {"caption": "某某示例科技守护城市安全"}, "author": {"name": "某媒体"}},
        {"photo": {"caption": "无人机吊运推荐"}},
        {"photo": {"caption": "Rokid glasses review"}},
    ]
    filtered = patches._ks_filter_feeds({"result": 1, "feeds": feeds, "pcursor": "1"}, "示例科技")
    check(
        "相关过滤: 仅保留命中 caption 的 feed（推荐补位被丢弃）",
        len(filtered.get("feeds") or []) == 1 and "示例" in filtered["feeds"][0]["photo"]["caption"],
        str(filtered.get("feeds")),
    )
    nickname_feed = [{"photo": {"caption": "无关标题"}, "author": {"name": "某某示例科技"}}]
    check(
        "相关过滤: 昵称命中（author.name）也保留",
        len(patches._ks_filter_feeds({"feeds": nickname_feed}, "示例科技")["feeds"]) == 1,
    )
    english_feed = [{"photo": {"caption": "Rokid glasses review"}}]
    check(
        "相关过滤: 英文大小写不敏感",
        len(patches._ks_filter_feeds({"feeds": english_feed}, "rokid")["feeds"]) == 1,
    )
    check(
        "相关过滤: token 为空时保守不过滤",
        len(patches._ks_filter_feeds({"feeds": nickname_feed}, "")["feeds"]) == 1,
    )
    check(
        "相关过滤: 全部无关时返回空 feeds（不再入库推荐流）",
        patches._ks_filter_feeds({"feeds": [{"photo": {"caption": "无人机"}}]}, "示例科技")["feeds"] == [],
    )


def test_kuaishou_notes_cap() -> None:
    """快手脚注上限：默认限制为 20（首屏一页），其它平台/回退开关不受影响。"""
    import os
    import types

    def make_mc():
        return {"config": types.SimpleNamespace(sqlite_db_config={})}

    tmp = Path(tempfile.mkdtemp(prefix="mc_engine_ks_cfg_"))
    previous = os.environ.pop("ENGINE_KS_SEARCH_WEB_STYLE", None)
    try:
        info_ks = patches.apply_config(make_mc(), "ks", tmp, max_notes=50, comments=False)
        check(
            "快手配置: CRAWLER_MAX_NOTES_COUNT 限制为 20（只取首屏一页）",
            info_ks["effective_max_notes"] == 20 and info_ks["max_notes"] == 50,
            str(info_ks),
        )
        info_xhs = patches.apply_config(make_mc(), "xhs", tmp, max_notes=50, comments=False)
        check("快手配置: 其它平台不限制（xhs max_notes=50 保持）", info_xhs["effective_max_notes"] == 50, str(info_xhs))
        os.environ["ENGINE_KS_SEARCH_WEB_STYLE"] = "0"
        info_off = patches.apply_config(make_mc(), "ks", tmp, max_notes=50, comments=False)
        check("快手配置: 回退开关开启时不限制 notes 数量（旧行为）", info_off["effective_max_notes"] == 50, str(info_off))
    finally:
        os.environ.pop("ENGINE_KS_SEARCH_WEB_STYLE", None)
        if previous is not None:
            os.environ["ENGINE_KS_SEARCH_WEB_STYLE"] = previous


# --------------------------------------------------------------------------------------
# 8. CLI 参数映射 & engine.finished 兜底
# --------------------------------------------------------------------------------------
def test_cli_args() -> None:
    args = entry.parse_args(
        [
            "--mode",
            "crawl",
            "--platform",
            "xhs",
            "--keywords",
            "示例科技,某某示例",
            "--max-notes",
            "5",
            "--comments",
            "false",
            "--max-comments",
            "3",
            "--headless",
            "false",
            "--timeout",
            "60",
            "--data-dir",
            r"C:\tmp\data",
        ]
    )
    check(
        "CLI 参数解析（mode/platform/keywords/max-notes/comments/headless/timeout）",
        args.mode == "crawl"
        and args.platform == "xhs"
        and args.max_notes == 5
        and args.comments is False
        and args.max_comments == 3
        and args.headless is False
        and args.timeout == 60,
    )
    check("str2bool 支持 true/1/yes 与 false/0/no", entry.str2bool("true") and entry.str2bool("1") and entry.str2bool("yes") and not entry.str2bool("false"))
    invalid = False
    try:
        entry.str2bool("maybe")
    except Exception:
        invalid = True
    check("str2bool 非法值报错", invalid)


def _capture_engine_events(argv) -> list:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = entry.main(argv)
    events = []
    for line in stdout.getvalue().splitlines():
        if line.startswith(patches.EVENT_PREFIX):
            events.append(json.loads(line[len(patches.EVENT_PREFIX):]))
    return code, events


def test_engine_finished_guard() -> None:
    original = entry.run_mode
    tmp = Path(tempfile.mkdtemp(prefix="mc_engine_guard_"))
    base_argv = ["--mode", "login", "--platform", "xhs", "--data-dir", str(tmp)]

    try:
        # 7.1 异常 -> 仍发 engine.finished ok:false，退出码 0
        def boom(_args, _data_dir, _mc_root, _emitter):
            raise RuntimeError("boom")

        entry.run_mode = boom
        code, events = _capture_engine_events(base_argv)
        check("异常时 engine.finished 必出且 ok:false", bool(events) and events[-1]["type"] == "engine.finished" and events[-1]["ok"] is False, str(events[-1] if events else None))
        check("异常时 message 携带错误信息", "boom" in (events[-1].get("message", "") if events else ""))
        check("异常时进程退出码 0（以事件为准）", code == 0)
        check("engine.started 是第一个事件", events and events[0]["type"] == "engine.started")

        # 7.2 SystemExit（MediaCrawler 登录失败路径）-> 也不崩溃
        def sys_exit(_args, _data_dir, _mc_root, _emitter):
            raise SystemExit(0)

        entry.run_mode = sys_exit
        code, events = _capture_engine_events(base_argv)
        check("sys.exit 场景仍发 engine.finished ok:false", events and events[-1]["type"] == "engine.finished" and events[-1]["ok"] is False)
        check("sys.exit 场景退出码 0", code == 0)

        # 7.3 正常 crawl -> ok:true 且 db_path 指向 sqlite
        def noop(_args, _data_dir, _mc_root, _emitter):
            return None

        entry.run_mode = noop
        code, events = _capture_engine_events(
            ["--mode", "crawl", "--platform", "xhs", "--keywords", "示例科技", "--data-dir", str(tmp)]
        )
        finished = events[-1] if events else {}
        check("正常结束 engine.finished ok:true", finished.get("type") == "engine.finished" and finished.get("ok") is True)
        check("crawl 的 db_path 指向 data-dir/crawler/media_crawler.db", str(finished.get("db_path", "")).endswith(str(Path("crawler") / "media_crawler.db")))
    finally:
        entry.run_mode = original


# --------------------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------------------
def main() -> int:
    entry._force_utf8()
    print("MediaCrawler Engine 离线自测（不联网 / 不启动浏览器）")
    print(f"engine 目录: {ENGINE_DIR}")
    tests = [
        test_event_protocol,
        test_event_line_format,
        test_mediacrawler_patches,
        test_config_mapping,
        test_qr_capture,
        test_browser_channel_patch,
        test_login_patches_and_progress,
        test_login_mode_explicit_states,
        test_login_class_instrumentation,
        test_headless_resolution,
        test_verification_detection,
        test_sms_secondary_gating,
        test_need_manual_bring_to_front,
        test_manual_gate_events,
        test_manual_wait_clear,
        test_wait_manual_before_begin,
        test_goto_retry,
        test_qr_fallback,
        test_qr_image_validation,
        test_cv2_quiet_zone_decode,
        test_qr_capture_rejects_invalid,
        test_qr_capture_tieba_strict,
        test_wait_image_natural_width,
        test_http_src_prefers_screenshot,
        test_qr_three_rounds_failure,
        test_weibo_desktop_qr_page_switch,
        test_candidate_geometry_filter,
        test_tieba_passport_page_switch,
        test_douyin_click_robustness,
        test_kuaishou_search_patch,
        test_kuaishou_relevance_filter,
        test_kuaishou_notes_cap,
        test_cli_args,
        test_engine_finished_guard,
    ]
    for test in tests:
        print(f"\n--- {test.__name__} ---")
        try:
            test()
        except Exception:  # noqa: BLE001
            _FAILED.append((test.__name__, "unexpected exception"))
            traceback.print_exc()
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    total = len(CHECKS)
    print("\n" + "=" * 70)
    if _FAILED:
        print(f"ENGINE SELFTEST: FAIL（{passed}/{total} 项通过；失败 {len(_FAILED)}）")
        for name, detail in _FAILED:
            print(f"  - {name}  {detail}")
        print("=" * 70)
        return 1
    print(f"ENGINE SELFTEST: PASS（{passed}/{total} 项通过）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

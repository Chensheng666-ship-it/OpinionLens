# -*- coding: utf-8 -*-
"""Engine 门面（facade）：对外 API 与签名保持不变，实现已拆到 ``app/engine/adapters/``。

拆分后的职责边界：
* ``adapters/base.py``       上游契约探测 / 显式错误 / 补丁 warning 计数
* ``adapters/browser.py``    Playwright channel（系统 Edge）+ Page.goto 重试
* ``adapters/login.py``      登录流 / 二维码 / 人工接管 / 网络异常
* ``adapters/content.py``    数据不脱敏 / 采集进度日志
* ``adapters/platform_*.py`` 各平台补丁与其上游契约声明

本模块只保留：事件协议（``EventEmitter`` / ``EVENT_FIELDS``）、MediaCrawler 导入引导、
配置注入、test 模式探针、契约探测入口与门面转发。``entry.py`` 的调用方式一行都不用改。
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import adapters
from adapters.base import (
    AlreadyLoggedIn,
    EngineIncompatible,
    EnginePatchError,
    LoginOnlyDone,
    MissingSymbol,
    NetworkTransient,
    NotLoggedIn,
    PLATFORMS,
    QrLoadFailed,
    UpstreamSymbol,
    adapter_registry,
    clear_contract_cache,
    contract_cache_info,
    detect_upstream_contract,
    emit_patch_error,
    emit_patch_warning,
    iter_contracts,
    patch_warning_counts,
    register_contract,
    require_patch,
    reset_patch_warning_counts,
    set_warning_emitter,
)
from adapters.browser import (
    DEFAULT_CHANNEL,
    GOTO_RETRYABLE_ERRORS,
    _BROWSER_PATCH,
    _PAGE_GOTO_PATCH,
    install_goto_retry,
    install_initial_page_reuse_patch,
    patch_browser_channel,
)
from adapters.content import (
    ProgressLogHandler,
    install_progress_handler,
    patch_user_hash,
)
from adapters.session import (
    PatchSession,
    current_session,
    ensure_session,
    reset_session,
    start_session,
)
from adapters.manual import (
    MANUAL_KIND_LABELS,
    MANUAL_WAIT_SECONDS,
    SLIDER_PHRASES,
    SLIDER_TITLE_HINTS,
    SMS_PHRASES,
    VERIFY_URL_HINTS,
    ManualGate,
    _manual_message,
    _safe_page_inner_text,
    _safe_page_title,
    _safe_page_url,
    detect_verification,
    wait_manual_before_begin,
)
from adapters.network import (
    _is_network_exception,
    _recent_network_exception,
    _record_network_exception,
    clear_network_transient,
    install_network_error_hook,
)
from adapters.qr import (
    DEFAULT_QR_FALLBACK_SELECTORS,
    DEFAULT_QR_USER_AGENT,
    QR_FAIL_MESSAGE,
    QR_FAIL_MESSAGE_TIEBA,
    QR_FALLBACK_SELECTORS,
    QR_HTTP_RETRIES,
    QR_IMAGE_LOAD_WAIT_S,
    QR_KEEPALIVE_PLACEHOLDER,
    QR_MAX_AREA,
    QR_MAX_ASPECT_RATIO,
    QR_MAX_ROUNDS,
    QR_MAX_SIDE,
    QR_MIN_AREA,
    QR_MIN_ASPECT_RATIO,
    QR_MIN_DARK_RATIO,
    QR_MIN_SIDE,
    QR_MIN_STD,
    QR_MIN_UNIQUE,
    QR_SHORT_WAIT_MS,
    QR_TIEBA_STRICT_DECODE,
    QR_VIEW_ENTRY_SELECTORS,
    TIEBA_QR_LOGIN_URL,
    WEIBO_QR_LOGIN_URL,
    WEIBO_QR_USER_AGENT,
    QrCapture,
    _acquire_qr_once,
    _cv2_decode_qr,
    _decode_base64_image,
    _element_geometry,
    _find_qr_element,
    _http_download_qr,
    _page_user_agent,
    _prepare_qr_view,
    _qr_already_visible,
    _qr_candidate_from_element,
    _qr_visible_for_platform,
    _reload_login_page,
    _run_qr_rounds,
    _short_wait_original_find,
    _switch_tieba_to_passport_qr_page,
    _switch_weibo_to_desktop_qr_page,
    _wait_image_loaded,
    extract_qr_fallback,
    inspect_qr_candidates,
    install_qr_capture,
    validate_qr_bytes,
    validate_qr_data_uri,
)
from adapters.login import (
    LOGIN_CLASSES,
    LOGIN_CLASS_CONTRACTS,
    _login_class,
    close_crawler,
    install_qr_fallback,
    instrument_login_class,
    patch_login_blocked,
    patch_login_only,
    should_trigger_sms,
    watch_login_page,
)
from adapters.platform_douyin import (
    _DY_LOGIN_BUTTON_SELECTORS,
    _DY_LOGIN_PANEL_SELECTORS,
    _any_visible,
    _robust_douyin_popup_login_dialog,
    _wait_any_visible,
    patch_douyin_login_dialog,
)
from adapters.platform_kuaishou import (
    KS_MAX_NOTES_PER_PAGE,
    KS_SEARCH_WEB_URI,
    _ks_client_class,
    _ks_feed_matches,
    _ks_feed_text,
    _ks_filter_feeds,
    _ks_relevance_tokens,
    _ks_web_style_enabled,
    patch_kuaishou_search,
)
from adapters.platform_weibo import (
    WEIBO_NO_COMMENT_MARKERS,
    _is_weibo_no_comment_message,
    patch_weibo_comment_empty,
)

log = logging.getLogger("engine.patches")

EVENT_PREFIX = "##EVENT##"

CLIENT_METHODS: Dict[str, str] = {
    "xhs": "create_xhs_client",
    "dy": "create_douyin_client",
    "ks": "create_ks_client",
    "bili": "create_bilibili_client",
    "wb": "create_weibo_client",
    "tieba": "create_tieba_client",
    "zhihu": "create_zhihu_client",
}


# 账号化：account_id 只在显式传入（--account-id 非空）时附加到事件上，
# 空账号时事件字段与历史版本逐字节一致（旧消费者/旧自测不受影响）。
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")

# 事件字段集合（契约 §1.2，字段名不可改）；自测用于严格校验
EVENT_FIELDS: Dict[str, set] = {
    "engine.started": {"type", "mode", "platform", "pid", "ts"},
    "login.qrcode": {"type", "platform", "seq", "image", "file", "bytes", "ts"},
    "login.status": {"type", "platform", "status", "message", "ts"},
    "test.result": {"type", "platform", "ok", "message", "duration_ms", "ts"},
    "progress": {"type", "platform", "stage", "page", "saved", "message", "ts"},
    "engine.finished": {
        "type",
        "ok",
        "mode",
        "platform",
        "elapsed_ms",
        "db_path",
        "message",
        "ts",
    },
}


def expected_event_fields(event_type: str, account_id: str = "") -> set:
    """事件字段期望集合：account_id 非空时附加 ``account_id``（账号化扩展）。"""
    fields = set(EVENT_FIELDS.get(event_type, ()))
    if account_id:
        fields.add("account_id")
    return fields


def validate_account_id(account_id: Any) -> str:
    """校验账号 ID（``[A-Za-z0-9_-]{1,40}``，空串合法=旧单账号行为）。"""
    text = "" if account_id is None else str(account_id).strip()
    if not text:
        return ""
    if not ACCOUNT_ID_RE.fullmatch(text):
        raise ValueError("--account-id 只能包含 [A-Za-z0-9_-] 且长度 1-40：" + repr(text))
    return text


def validate_profile_template(template: Any) -> Optional[str]:
    """校验 profile 模板：必须是含**恰好一个** ``%s`` 占位符的绝对路径。

    MediaCrawler 7 平台统一做 ``config.USER_DATA_DIR % config.PLATFORM``，
    因此占位符数量必须恰好为 1（0 个 TypeError / 2 个参数不足）。
    """
    if template is None:
        return None
    text = str(template).strip()
    if not text:
        return None
    if text.count("%s") != 1:
        raise ValueError("--profile-template 必须恰好包含一个 %s 占位符：" + repr(text))
    try:
        text % "xhs"  # 确认不存在其它 % 占位符导致格式化失败
    except (TypeError, ValueError) as exc:
        raise ValueError("--profile-template 不是合法模板（% 转义有误）：" + repr(text)) from exc
    if not Path(text).is_absolute():
        raise ValueError("--profile-template 必须是绝对路径：" + repr(text))
    return text

# --------------------------------------------------------------------------------------
# 事件输出（##EVENT##{json} 单行）
# --------------------------------------------------------------------------------------
class EventEmitter:
    """结构化事件输出。sink 为 None 时写 stdout；自测可注入 list.append 收集。"""

    def __init__(
        self,
        platform: str,
        mode: str,
        sink: Optional[Callable[[dict], None]] = None,
        account_id: str = "",
    ) -> None:
        self.platform = platform
        self.mode = mode
        self._sink = sink
        self.account_id = validate_account_id(account_id)
        self._lock = threading.Lock()
        self.seq = 0
        self.events: List[dict] = []
        # 每次运行一个补丁会话：绑定本次运行的 emitter，并清空 warned_once / installed /
        # 计数（M1.5：同进程多次跑时补丁不会串到上一次的 emitter 或"只提醒一次"状态）。
        self.session: PatchSession = start_session(emitter=self)

    # -- 内部 --------------------------------------------------------------
    def _emit(self, event_type: str, fields: Dict[str, Any]) -> dict:
        event: Dict[str, Any] = {"type": event_type, "platform": self.platform, "ts": int(time.time())}
        if self.account_id:
            # 账号化：只加字段不改名；空账号（旧单账号行为）时事件与历史完全一致
            event["account_id"] = self.account_id
        event.update(fields)
        with self._lock:
            self.events.append(event)
            if self._sink is not None:
                try:
                    self._sink(event)
                except Exception as exc:  # noqa: BLE001 - 自测/侧车 sink 异常不能影响事件流
                    log.debug("事件 sink 抛出异常（已忽略）：%s: %s", type(exc).__name__, exc)
            else:
                sys.stdout.write(
                    EVENT_PREFIX + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                sys.stdout.flush()
        return event

    # -- 契约事件 ----------------------------------------------------------
    def started(self) -> dict:
        return self._emit("engine.started", {"mode": self.mode, "pid": os.getpid()})

    def qrcode(self, image: str, file: str, nbytes: int) -> dict:
        with self._lock:
            self.seq += 1
            seq = self.seq
        return self._emit(
            "login.qrcode",
            {"seq": seq, "image": image, "file": str(file), "bytes": int(nbytes)},
        )

    def status(self, status: str, message: str = "", kind: Optional[str] = None) -> dict:
        fields: Dict[str, Any] = {"status": status, "message": message}
        if kind:
            # 可选扩展字段：kind=sms|slider|captcha（契约既有字段不变，sidecar 可忽略）
            fields["kind"] = kind
        return self._emit("login.status", fields)

    def test_result(self, ok: bool, message: str, duration_ms: int) -> dict:
        return self._emit(
            "test.result",
            {"ok": bool(ok), "message": message, "duration_ms": int(duration_ms)},
        )

    def progress(self, stage: str, page: int = 0, saved: int = 0, message: str = "") -> dict:
        return self._emit(
            "progress",
            {"stage": stage, "page": int(page), "saved": int(saved), "message": message},
        )

    def finished(
        self,
        ok: bool,
        elapsed_ms: int,
        db_path: Optional[str] = None,
        message: str = "",
    ) -> dict:
        return self._emit(
            "engine.finished",
            {
                "ok": bool(ok),
                "mode": self.mode,
                "elapsed_ms": int(elapsed_ms),
                "db_path": db_path,
                "message": message,
            },
        )

# --------------------------------------------------------------------------------------
# MediaCrawler 引导 / 导入
# --------------------------------------------------------------------------------------
_MC: Dict[str, Any] = {}

# 轻量登录/测试：只导入目标平台，避免 main.py 把所有平台（含重的 bilibili 依赖）都加载一遍。
CRAWLER_CLASSES: Dict[str, tuple[str, str]] = {
    "xhs": ("media_platform.xhs.core", "XiaoHongShuCrawler"),
    "dy": ("media_platform.douyin.core", "DouYinCrawler"),
    "ks": ("media_platform.kuaishou.core", "KuaishouCrawler"),
    "bili": ("media_platform.bilibili.core", "BilibiliCrawler"),
    "wb": ("media_platform.weibo.core", "WeiboCrawler"),
    "tieba": ("media_platform.tieba.core", "TieBaCrawler"),
    "zhihu": ("media_platform.zhihu.core", "ZhihuCrawler"),
}

def _light_crawler_factory(platform: str) -> Any:
    module_name, class_name = CRAWLER_CLASSES[platform]
    module = importlib.import_module(module_name)
    crawler_cls = getattr(module, class_name)

    class _LightCrawlerFactory:
        @staticmethod
        def create_crawler(name: str) -> Any:
            target = platform if name == platform else str(name)
            target_module, target_class = CRAWLER_CLASSES[target]
            target_cls = getattr(importlib.import_module(target_module), target_class)
            return target_cls()

    return _LightCrawlerFactory()

def resolve_mc_root() -> Path:
    """MediaCrawler 根目录：优先环境变量 MC_ROOT，否则 <project>/MediaCrawler。"""
    env = os.environ.get("MC_ROOT", "").strip()
    if env:
        root = Path(env).expanduser().resolve()
    else:
        # app/engine/patches.py -> parents[2] = 项目根
        root = (Path(__file__).resolve().parents[2] / "MediaCrawler").resolve()
    if not (root / "main.py").is_file():
        raise FileNotFoundError(f"MediaCrawler 根目录无效（找不到 main.py）: {root}")
    return root


def bootstrap(mc_root: Path) -> None:
    """固定 cwd 到 MediaCrawler 根（libs/stealth.min.js 等相对路径依赖），并加入 sys.path。"""
    sys.dont_write_bytecode = True  # 不往 MediaCrawler/ 写 __pycache__（只读约束）
    os.chdir(str(mc_root))
    if str(mc_root) not in sys.path:
        sys.path.insert(0, str(mc_root))


def import_mediacrawler(
    mc_root: Path, platform: Optional[str] = None, light: bool = False
) -> Dict[str, Any]:
    """按安全顺序导入 MediaCrawler 并打好 user_hash / show_qrcode 前置 patch。

    ``light=True``（仅登录/测试模式使用）时只导入目标平台 crawler，避免
    ``main.py`` 把所有平台都 import 一遍导致启动多等 2~4 秒；采集模式仍
    走完整 ``main``，保证 database/store 初始化与历史行为一致。

    返回 dict: {"main", "config", "utils", "crawler_util", "user_hash", "mc_root"}
    重复调用返回同一份 dict。
    """
    if _MC:
        return _MC

    bootstrap(mc_root)
    install_initial_page_reuse_patch()

    # 1) 先导入 tools.utils（必须最先，避免 tools 包内循环导入问题）
    tools_utils = importlib.import_module("tools.utils")
    # 2) 再导入 user_hash 并于 main 之前替换为恒等函数
    tools_user_hash = importlib.import_module("tools.user_hash")
    tools_crawler_util = importlib.import_module("tools.crawler_util")
    patch_user_hash(tools_user_hash)
    # 3) 最后导入 config 与 main（main 会导入各平台 core/store，store 在 import 时
    #    通过 `from tools.user_hash import anonymize_user_id` 绑定函数 -> 必须已 patch）
    config = importlib.import_module("config")
    if light and platform in CRAWLER_CLASSES:
        mc_main = types.SimpleNamespace(CrawlerFactory=_light_crawler_factory(platform))
        print(
            f"[engine] 轻量导入模式：只加载 {platform} 平台类（跳过其它平台 import）",
            flush=True,
        )
    else:
        mc_main = importlib.import_module("main")

    _MC.update(
        {
            "mc_root": Path(mc_root),
            "utils": tools_utils,
            "crawler_util": tools_crawler_util,
            "user_hash": tools_user_hash,
            "config": config,
            "main": mc_main,
        }
    )
    return _MC

# --------------------------------------------------------------------------------------
# Patch 4：配置注入（调用 crawler.start() 前）
# --------------------------------------------------------------------------------------
def ensure_data_dirs(data_dir: Path) -> Dict[str, Path]:
    data_dir = Path(data_dir)
    dirs = {
        "data": data_dir,
        "qr": data_dir / "qr",
        "crawler": data_dir / "crawler",
        "logs": data_dir / "logs",
        "browser_data": data_dir / "browser_data",
        "exports": data_dir / "exports",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def parse_keywords(keywords: Any) -> List[str]:
    if isinstance(keywords, (list, tuple)):
        items = [str(k) for k in keywords]
    else:
        items = re.split(r"[,，]", str(keywords or ""))
    return [k.strip() for k in items if k.strip()]

def apply_config(
    mc: Dict[str, Any],
    platform: str,
    data_dir: Path,
    *,
    headless: bool = True,
    keywords: Optional[List[str]] = None,
    max_notes: int = 15,
    comments: bool = True,
    max_comments: int = 20,
    account_id: str = "",
    profile_template: Optional[str] = None,
) -> Dict[str, Any]:
    """按契约 §1.3-4 注入配置；返回实际生效的关键路径/参数，便于日志与自测。

    ``profile_template``（账号化）：含恰好一个 ``%s`` 的绝对路径模板，交给
    MediaCrawler 做 ``% config.PLATFORM`` 替换；为空时按 ``account_id`` 生成
    v2 模板 ``<data-dir>/browser_data/%s/<account_id>/user_data_dir``；
    两者都为空时保持旧单账号模板 ``<data-dir>/browser_data/%s_user_data_dir``。
    """
    data_dir = Path(data_dir).resolve()
    ensure_data_dirs(data_dir)
    config = mc["config"]

    db_path = data_dir / "crawler" / "media_crawler.db"
    account_id = validate_account_id(account_id)
    template = validate_profile_template(profile_template)
    if template is not None:
        user_data_dir = template
    elif account_id:
        user_data_dir = str(data_dir / "browser_data" / "%s" / account_id / "user_data_dir")
    else:
        # 旧单账号行为：模板保持历史值（profile 目录 <data-dir>/browser_data/<platform>_user_data_dir）
        user_data_dir = str(data_dir / "browser_data" / "%s_user_data_dir")

    config.PLATFORM = platform
    config.LOGIN_TYPE = "qrcode"
    config.CRAWLER_TYPE = "search"
    config.HEADLESS = bool(headless)
    config.CDP_HEADLESS = bool(headless)
    config.ENABLE_CDP_MODE = False
    config.SAVE_LOGIN_STATE = True
    config.USER_DATA_DIR = user_data_dir
    config.SAVE_DATA_OPTION = "sqlite"
    config.ENABLE_GET_COMMENTS = bool(comments)
    config.ENABLE_GET_SUB_COMMENTS = False
    config.ENABLE_GET_MEDIA = False
    config.ENABLE_GET_WORDCLOUD = False
    config.CRAWLER_MAX_NOTES_COUNT = int(max_notes)
    if platform == "ks":
        if _ks_web_style_enabled():
            # 快手固定 20 条/页；只取首屏一页，避免第 2 页 pcursor 语义不明又回到推荐流
            config.CRAWLER_MAX_NOTES_COUNT = KS_MAX_NOTES_PER_PAGE
            if int(max_notes) != KS_MAX_NOTES_PER_PAGE:
                print(
                    f"[engine] ks 仅取首屏一页：CRAWLER_MAX_NOTES_COUNT {int(max_notes)} -> "
                    f"{KS_MAX_NOTES_PER_PAGE}（快手 20 条/页，首屏为强相关结果）",
                    flush=True,
                )
        else:
            print("[engine] ENGINE_KS_SEARCH_WEB_STYLE=0：保持旧分页行为，不限制 ks notes 数量", flush=True)
    config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES = int(max_comments)
    config.MAX_CONCURRENCY_NUM = 1
    config.START_PAGE = 1
    config.SQLITE_DB_PATH = str(db_path)
    config.sqlite_db_config["db_path"] = str(db_path)
    if keywords is not None:
        config.KEYWORDS = ",".join(parse_keywords(keywords))
    else:
        config.KEYWORDS = ""

    return {
        "platform": platform,
        "db_path": str(db_path),
        "account_id": account_id,
        "user_data_dir": user_data_dir,
        "user_data_dir_template": user_data_dir,
        "user_data_dir_platform": user_data_dir % platform,
        "keywords": parse_keywords(config.KEYWORDS),
        "headless": bool(headless),
        "max_notes": int(max_notes),
        "effective_max_notes": int(config.CRAWLER_MAX_NOTES_COUNT),
        "comments": bool(comments),
        "max_comments": int(max_comments),
    }

# --------------------------------------------------------------------------------------
# Patch 7：test 模式（pong 轻量探测，不触发登录/搜索）
# --------------------------------------------------------------------------------------
async def probe_login_state(
    mc: Dict[str, Any],
    platform: str,
    data_dir: Path,
    headless: bool = True,
    timeout: float = 60.0,
    emitter: Optional[EventEmitter] = None,
    account_id: str = "",
    profile_template: Optional[str] = None,
) -> Dict[str, Any]:
    """启动浏览器读取已保存登录态并调用 client.pong()，返回 {ok, message, duration_ms}。

    账号化：``account_id/profile_template`` 透传给 :func:`apply_config`，
    保证 test 模式也读取目标账号的独立 profile（而不是旧平台目录）。
    """
    import asyncio
    from playwright.async_api import async_playwright

    apply_config(
        mc,
        platform,
        data_dir,
        headless=headless,
        keywords=[],
        max_notes=1,
        comments=False,
        max_comments=1,
        account_id=account_id,
        profile_template=profile_template,
    )
    patch_browser_channel()
    install_goto_retry(None)  # 测试连接也可能遇到导航抖动（如微博 passport）
    install_network_error_hook()
    crawler = mc["main"].CrawlerFactory.create_crawler(platform)
    started = time.perf_counter()

    async def _run() -> bool:
        async with async_playwright() as playwright:
            context = await crawler.launch_browser(
                playwright.chromium, None, getattr(crawler, "user_agent", None), headless=headless
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                # 复刻 core.start() 的上下文赋值：create_*_client 依赖 crawler.browser_context/context_page
                crawler.browser_context = context
                crawler.context_page = page
                # 默认加载平台首页：部分平台 pong 需要页面 origin/localStorage（如抖音）；
                # 不加载首页会导致 localStorage Access denied。ENGINE_TEST_NAVIGATE_HOME=0
                # 仅在确认某平台不需要时使用（默认保持兼容）。
                if os.environ.get("ENGINE_TEST_NAVIGATE_HOME", "1") != "0":
                    try:
                        await page.goto(crawler.index_url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as exc:  # noqa: BLE001 - 首页加载失败也让 pong 决定（cookie 可能仍有效）
                        log.debug("test 模式加载平台首页失败（继续 pong）：%s: %s", type(exc).__name__, exc)
                # 首页导航失败不算登录态；只关注 pong 自身的网络错误
                clear_network_transient()
                client = await getattr(crawler, CLIENT_METHODS[platform])(None)
                # dy / tieba 的 pong 需要 browser_context 参数，其余平台无参
                import inspect

                try:
                    parameters = inspect.signature(client.pong).parameters
                except (TypeError, ValueError):
                    parameters = {}
                if "browser_context" in parameters:
                    return bool(await client.pong(browser_context=crawler.browser_context))
                return bool(await client.pong())
            finally:
                try:
                    await asyncio.wait_for(context.close(), timeout=15)
                except Exception as exc:  # noqa: BLE001 - 探测收尾清理失败不影响 test.result
                    log.debug("test 模式关闭浏览器上下文失败（忽略）：%s: %s", type(exc).__name__, exc)

    try:
        ok = bool(await asyncio.wait_for(_run(), timeout=timeout))
    except asyncio.TimeoutError:
        return {"ok": False, "message": f"测试连接超时（{int(timeout)}s）", "duration_ms": int(timeout * 1000)}
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"{type(exc).__name__}: {exc}",
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
    if not ok:
        network_error = _recent_network_exception()
        if network_error:
            return {
                "ok": False,
                "network_error": True,
                "message": f"网络异常，登录态未确认：{network_error[:180]}",
                "duration_ms": int((time.perf_counter() - started) * 1000),
            }
    return {
        "ok": ok,
        "message": "已登录" if ok else "未登录",
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }

# --------------------------------------------------------------------------------------
# 上游契约探测（engine 入口在 run_mode 之前调用一次）
# --------------------------------------------------------------------------------------
def check_upstream_contract(
    mc_root: Path, platform: Optional[str] = None, mode: Optional[str] = None
) -> List[Any]:
    """探测适配器依赖的上游符号；缺失即抛 EngineIncompatible（绝不带着空枪继续跑）。"""
    missing = detect_upstream_contract(mc_root, platform=platform, mode=mode)
    if missing:
        raise EngineIncompatible(missing)
    return []


# --------------------------------------------------------------------------------------
# 门面兼容层：对外名字 / 签名不变（实现已搬到 adapters/*）
# --------------------------------------------------------------------------------------
_ADAPTER_MODULES = (
    adapters.base,
    adapters.browser,
    adapters.login,
    adapters.content,
    adapters.session,
    adapters.qr,
    adapters.manual,
    adapters.network,
    adapters.platform_xhs,
    adapters.platform_douyin,
    adapters.platform_kuaishou,
    adapters.platform_bilibili,
    adapters.platform_weibo,
    adapters.platform_tieba,
    adapters.platform_zhihu,
)

# 名字 -> 绑定它的所有适配器模块（含"从别处 import 进来"的模块）。
# ``patches.xxx = ...`` 会同步写回这些模块，保持历史用法
# （entry / 自测通过门面替换实现，例如 detect_verification：manual 定义 + login 引用都要换）。
_MIRROR: Dict[str, List[types.ModuleType]] = {}
for _module in _ADAPTER_MODULES:
    for _name, _value in vars(_module).items():
        if _name.startswith("__") or isinstance(_value, types.ModuleType):
            continue
        if _name in globals() and globals()[_name] is _value:
            modules = _MIRROR.setdefault(_name, [])
            if _module not in modules:
                modules.append(_module)


class _FacadeModule(types.ModuleType):
    """门面模块：写属性时同步到实现模块，避免「只改门面、内部仍用旧实现」。"""

    def __setattr__(self, name: str, value: Any) -> None:
        for owner in _MIRROR.get(name, ()):
            if owner is not self:
                setattr(owner, name, value)
        super().__setattr__(name, value)


sys.modules[__name__].__class__ = _FacadeModule

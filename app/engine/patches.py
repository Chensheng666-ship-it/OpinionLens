# -*- coding: utf-8 -*-
"""Engine 运行时 patch 集合（唯一允许触碰 MediaCrawler 配置/行为的包装层）。

所有修改都在运行时进行，不修改 MediaCrawler/ 源码。导入顺序必须为：

    import tools.utils      # 1) MediaCrawler 的 tools 包先导入（避免 proxy 循环导入）
    import tools.user_hash  # 2) 立即把脱敏函数换成恒等
    import config / main    # 3) 之后才能导入 config 与 main（main 会导入各平台 core/store）

本模块顶层只依赖标准库，媒体库/Playwright 均在函数内导入，便于离线自测。
"""

from __future__ import annotations

import base64
import hashlib
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
from io import BytesIO
from typing import Any, Callable, Dict, List, Optional, Tuple

EVENT_PREFIX = "##EVENT##"
DEFAULT_CHANNEL = "msedge"

PLATFORMS = ("xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu")

# platform -> (login 模块, Login 类名, crawler create_client 方法名)
LOGIN_CLASSES: Dict[str, tuple[str, str]] = {
    "xhs": ("media_platform.xhs.login", "XiaoHongShuLogin"),
    "dy": ("media_platform.douyin.login", "DouYinLogin"),
    "ks": ("media_platform.kuaishou.login", "KuaishouLogin"),
    "bili": ("media_platform.bilibili.login", "BilibiliLogin"),
    "wb": ("media_platform.weibo.login", "WeiboLogin"),
    "tieba": ("media_platform.tieba.login", "BaiduTieBaLogin"),
    "zhihu": ("media_platform.zhihu.login", "ZhiHuLogin"),
}

CLIENT_METHODS: Dict[str, str] = {
    "xhs": "create_xhs_client",
    "dy": "create_douyin_client",
    "ks": "create_ks_client",
    "bili": "create_bilibili_client",
    "wb": "create_weibo_client",
    "tieba": "create_tieba_client",
    "zhihu": "create_zhihu_client",
}

# ---- 人工接管 / 验证页检测 ---------------------------------------------------------
# 以“可见文本/标题/URL”判断是否处于平台安全验证页（产品策略：宁可多提示不要卡死）
# 短信验证只在“扫码之后、二维码/登录弹窗已切换”才算二次验证；短语必须是平台短信验证弹层专属。
# 不要包含「短信验证登录」「获取验证码」「验证码登录」等普通登录页 tab/表单文案。
SMS_PHRASES = (
    "短信验证码",
    "短信已发送",
    "验证码已发送",
    "验证码已发送至",
    "请填写短信验证码",
    "请输入短信验证码",
    "请填写验证码",
    "发送至手机",
    "短信发送成功",
    "为保障账号安全",
)
SLIDER_PHRASES = (
    "验证码中间页",
    "安全验证",
    "请通过验证",
    "请完成下方验证",
    "请向右滑动",
    "拖动滑块",
    "向右滑动完成拼图",
    "拖动滑块完成拼图",
    "滑块验证",
    "拼图",
    "人机验证",
    "请完成下列验证",
    "完成验证",
    "按住左边",
    "点击按钮进行验证",
    "请完成安全验证",
    "验证失败，请重试",
)
SLIDER_TITLE_HINTS = ("验证码中间页", "安全验证", "请通过验证", "滑块", "身份验证")
VERIFY_URL_HINTS = ("verify", "captcha", "security", "safe_verify", "antispam")
MANUAL_KIND_LABELS = {"sms": "短信验证码", "slider": "滑块/拼图安全验证", "captcha": "安全验证"}
MANUAL_WAIT_SECONDS = 240.0  # 人工接管最长等待（任务要求 240s）
QR_SHORT_WAIT_MS = 8000  # 原生二维码选择器短等待，避免 wait_for_selector 默认 30s 卡死

# ---- 二维码图片有效性 / 加载重试 -----------------------------------------------------
QR_MAX_ROUNDS = 3  # 最多 3 轮：取码无效 -> 重载登录页后重试
QR_MIN_SIDE = 80  # 宽高下限（px）
QR_MIN_ASPECT_RATIO = 0.75  # 宽/高 下限（近似正方形；948x168 横幅 = 5.6 一律拒绝）
QR_MAX_ASPECT_RATIO = 1.35  # 宽/高 上限
QR_MAX_SIDE = 1200  # 单边上限（dy 512 允许；避免整页/大图）
QR_MIN_AREA = QR_MIN_SIDE * QR_MIN_SIDE
QR_MAX_AREA = 2_000_000
QR_MIN_STD = 12.0  # 灰度标准差下限（纯色/空白图 < 1；真实二维码 > 40）
QR_MIN_UNIQUE = 8  # 64x64 采样后最少唯一灰度值数
QR_MIN_DARK_RATIO = 0.03  # 暗像素比例下限（空白图=0）
QR_IMAGE_LOAD_WAIT_S = 10.0  # <img> naturalWidth 等待上限（秒）
QR_HTTP_RETRIES = 3  # http(s) src 的 httpx 下载重试次数
QR_FAIL_MESSAGE = "二维码加载失败（可能是网络/代理拦截），请检查网络后重试登录"
QR_FAIL_MESSAGE_TIEBA = "二维码定位失败（未找到有效的正方形二维码），请重试登录"
# 有头登录时若应用内读不到二维码，不急着判失败：用户仍可在已打开的浏览器窗口
# 直接扫码；返回一个占位二维码让 MediaCrawler 继续轮询登录态，成功后自动 confirmed。
QR_KEEPALIVE_PLACEHOLDER = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
QR_TIEBA_STRICT_DECODE = True  # 贴吧候选必须能被 cv2 解码（避免占位图/横幅）
DEFAULT_QR_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 微博登录专用桌面 UA 与 popup 登录页：
# MediaCrawler 用 iPhone UA 打开 passport，页面默认是“短信验证登录”移动布局，
# 切到扫码 tab 后二维码 img 在部分环境渲染/截图异常；改用桌面 UA + popup 登录页可稳定渲染二维码。
WEIBO_QR_USER_AGENT = os.environ.get("ENGINE_WEIBO_UA") or DEFAULT_QR_USER_AGENT
WEIBO_QR_LOGIN_URL = os.environ.get("ENGINE_WEIBO_QR_LOGIN_URL") or (
    "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&disp=popup"
    "&url=https%3A%2F%2Fweibo.com%2Fnewlogin%3Ftabtype%3Dweibo%26gid%3D102803"
    "%26openLoginLayer%3D0%26url%3D&from=weibopro"
)

# 贴吧登录专用 passport 页：现代贴吧首页的扫码卡只有占位图/横幅（948x168），
# 直接打开百度 passport 登录页可稳定拿到真实二维码（img.tang-pass-qrcode-img）。
TIEBA_QR_LOGIN_URL = os.environ.get("ENGINE_TIEBA_QR_LOGIN_URL") or (
    "https://passport.baidu.com/v2/?login&tpl=tb&u=https%3A%2F%2Ftieba.baidu.com%2F"
)

# 二维码兜底选择器（原生选择器取不到时按顺序尝试）
QR_FALLBACK_SELECTORS: Dict[str, List[str]] = {
    "xhs": ["img.qrcode-img", "img[class*='qrcode']", "img[src*='qrcode']", "img[src*='qr']", "div[class*='qrcode'] img", "canvas"],
    "dy": [
        "div#animate_qrcode_container img",
        "div[id*='qrcode'] img",
        "div[class*='qrcode'] img",
        "img[id*='qrcode']",
        "img[class*='qrcode']",
        "img[class*='scan']",
        "img[src*='qrcode']",
        "img[src*='qr']",
        "canvas",
    ],
    "ks": ["div.qrcode-img img", "img[class*='qrcode']", "img[src*='qrcode']", "img[src*='qr']", "canvas"],
    "bili": ["div.login-scan-box img", "img[class*='qrcode']", "img[src*='qrcode']", "img[src*='qr']", "canvas"],
    "wb": ["img[class*='w-full h-full']", "img[class*='qrcode']", "img[src*='qr']", "img[src*='qrcode']", "canvas"],
    "tieba": [
        "img.tang-pass-qrcode-img",
        "img[class*='tang-pass-qrcode']",
        "img[id*='tang-pass-qrcode']",
        "img[src*='tang-pass-qrcode']",
        "img[class*='qrcode']",
        "img[src*='qrcode']",
        "canvas",
    ],
    "zhihu": ["canvas.Qrcode-qrcode", "canvas[class*='Qrcode']", "canvas[class*='qrcode']", "img[class*='qrcode']"],
}
DEFAULT_QR_FALLBACK_SELECTORS = ["img[class*='qrcode']", "img[src*='qrcode']", "img[src*='qr']", "canvas"]

# 部分平台默认落在“手机号登录”标签页，需要先点“扫码登录”入口才渲染二维码（微博实测）
QR_VIEW_ENTRY_SELECTORS: Dict[str, List[str]] = {
    "wb": [
        "text=扫码登录",
        "xpath=//*[contains(text(),'扫码登录')]",
        "xpath=//*[normalize-space(text())='扫码登录']",
    ],
    # 抖音登录面板可能默认停在手机号 tab（login-full-panel），需要点“扫码登录”切到二维码
    "dy": [
        "text=扫码登录",
        "xpath=//*[contains(text(),'扫码登录')]",
        "xpath=//*[contains(text(),'二维码登录')]",
    ],
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
# 异常
# --------------------------------------------------------------------------------------
class LoginOnlyDone(Exception):
    """login 模式：扫码登录成功后，由被替换的 search() 抛出，用于中止采集。"""


class AlreadyLoggedIn(Exception):
    """login 模式：profile 已有有效登录态（pong=true，未进入 login.begin），无需扫码。"""


class NotLoggedIn(Exception):
    """crawl 模式：需要登录时由被替换的 login.begin() 抛出，用于快速失败。"""


class NetworkTransient(Exception):
    """采集中的瞬时网络错误（不是未登录）。让 sidecar 自动重试，而不是提示重新登录。"""


class QrLoadFailed(Exception):
    """login 模式：3 轮重载重试后二维码图片仍无效（空白/未加载/下载失败）。"""


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
                except Exception:
                    pass
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

_INITIAL_PAGE_REUSE_PATCH: Dict[str, Any] = {}


def install_initial_page_reuse_patch() -> bool:
    """持久化浏览器上下文会自带一个 about:blank 初始页；MediaCrawler 随后又
    ``new_page()`` 一次，导致登录时出现两个页面/标签页。这里把“空白初始页”
    复用为 crawler.context_page，避免多开一个无用页面。

    仅在 context 恰好只有一个空白页时复用；后续业务代码再 new_page（如贴吧
    弹窗页）仍走 Playwright 原逻辑。ENGINE_REUSE_INITIAL_PAGE=0 可关闭。
    """
    if os.environ.get("ENGINE_REUSE_INITIAL_PAGE", "1") == "0":
        return False
    if _INITIAL_PAGE_REUSE_PATCH.get("installed"):
        return True
    try:
        from playwright.async_api import BrowserContext
    except Exception:  # noqa: BLE001 - 离线自测可能没有 playwright
        return False
    original_new_page = BrowserContext.new_page

    async def _new_page_or_reuse_blank(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            pages = list(self.pages)
        except Exception:
            pages = []
        if len(pages) == 1:
            page = pages[0]
            try:
                is_blank = str(page.url) in ("about:blank", "", "chrome://newtab/")
            except Exception:
                is_blank = False
            if is_blank and not getattr(page, "_engine_reused_blank_page", False):
                page._engine_reused_blank_page = True  # type: ignore[attr-defined]
                print("[engine] 复用持久化上下文初始空白页，避免登录多开页面", flush=True)
                return page
        return await original_new_page(self, *args, **kwargs)

    BrowserContext.new_page = _new_page_or_reuse_blank  # type: ignore[assignment]
    _INITIAL_PAGE_REUSE_PATCH["installed"] = True
    return True


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
# Patch 1：数据不脱敏（产品默认行为：保留 raw user_id / raw nickname，交付前可按部署方策略脱敏）
# --------------------------------------------------------------------------------------
def _identity_user_id(user_id: Any) -> str:
    if user_id is None:
        return ""
    return str(user_id)


def _identity_nickname(name: Any) -> str:
    if name is None:
        return ""
    return str(name)


def patch_user_hash(user_hash_module: Any) -> Dict[str, Any]:
    user_hash_module.anonymize_user_id = _identity_user_id
    user_hash_module.mask_nickname = _identity_nickname
    return {
        "anonymize_user_id": _identity_user_id,
        "mask_nickname": _identity_nickname,
    }


# --------------------------------------------------------------------------------------
# Patch 2：二维码截获 -> PNG + login.qrcode 事件
# --------------------------------------------------------------------------------------
def _decode_base64_image(qr_code: Any) -> Optional[bytes]:
    try:
        text = str(qr_code)
        if "," in text:
            text = text.split(",", 1)[1]
        return base64.b64decode(text)
    except Exception:
        return None


def _cv2_decode_qr(raw: Optional[bytes]) -> Optional[str]:
    """用 cv2.QRCodeDetector 尝试解码（补白边/灰度/放大/二值化），成功返回解码文本。

    注意：元素截图经常是“紧贴二维码”裁切的，没有静区（quiet zone）cv2 会解码失败；
    这里先补 20% 宽度的白边再解码（贴吧 passport 二维码实测必须补边才能解出）。
    """
    if not raw:
        return None
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        array = np.frombuffer(raw, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            return None
        detector = cv2.QRCodeDetector()
        height, width = image.shape[:2]
        pad = max(16, int(min(height, width) * 0.2))
        bordered = cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        border_gray = cv2.copyMakeBorder(gray, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        candidates: List[Any] = [
            bordered,
            cv2.cvtColor(border_gray, cv2.COLOR_GRAY2BGR),
            image,
            cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR),
        ]
        for scale in (2, 3):
            candidates.append(cv2.resize(bordered, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST))
        for threshold in (100, 127, 150, 180):
            _, binary = cv2.threshold(border_gray, threshold, 255, cv2.THRESH_BINARY)
            candidates.append(cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR))
        for candidate in candidates:
            try:
                text, _points, _straight = detector.detectAndDecode(candidate)
            except Exception:
                continue
            if text:
                return str(text)
        return None
    except Exception:
        return None


def validate_qr_bytes(raw: Optional[bytes]) -> Dict[str, Any]:
    """PIL + cv2 校验二维码图片是否可用：

    - 能解码、宽高都 >=80、宽高比 0.75~1.35（近似正方形）、单边 <=1200、面积适中；
    - 非纯色/空白：64x64 灰度采样 std / 唯一灰度 / 暗像素比；
    - 有 cv2 时做 detectAndDecode：能解码=最可靠（decoded=True）；解码失败但几何+纹理通过仍降级接受
      （贴吧等平台可要求 decoded=True）。
    """
    if not raw:
        return {"ok": False, "reason": "empty"}
    try:
        from PIL import Image

        image = Image.open(BytesIO(raw))
        image.load()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"decode:{type(exc).__name__}"}
    width, height = image.size
    if width < QR_MIN_SIDE or height < QR_MIN_SIDE:
        return {"ok": False, "reason": f"size:{width}x{height}", "width": width, "height": height}
    if max(width, height) > QR_MAX_SIDE:
        return {"ok": False, "reason": f"too_large:{width}x{height}", "width": width, "height": height}
    if width * height > QR_MAX_AREA:
        return {"ok": False, "reason": f"area:{width * height}", "width": width, "height": height}
    aspect = width / height
    if not (QR_MIN_ASPECT_RATIO <= aspect <= QR_MAX_ASPECT_RATIO):
        return {
            "ok": False,
            "reason": f"aspect:{width}x{height}({aspect:.2f})",
            "width": width,
            "height": height,
            "aspect": round(aspect, 3),
        }
    try:
        gray = image.convert("L").resize((64, 64))
        pixels = [int(value) for value in gray.getdata()]
        count = len(pixels)
        mean = sum(pixels) / count
        std = (sum((value - mean) ** 2 for value in pixels) / count) ** 0.5
        unique = len(set(pixels))
        dark_ratio = sum(1 for value in pixels if value < 128) / count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"stats:{type(exc).__name__}"}
    decoded_text = _cv2_decode_qr(raw)
    decoded = bool(decoded_text)
    heuristic_ok = std >= QR_MIN_STD and unique >= QR_MIN_UNIQUE and QR_MIN_DARK_RATIO <= dark_ratio <= 0.97
    ok = heuristic_ok or decoded
    reason = ""
    if not ok:
        reason = f"blank(std={std:.1f},unique={unique},dark={dark_ratio:.2f})"
    return {
        "ok": ok,
        "width": width,
        "height": height,
        "aspect": round(aspect, 3),
        "std": round(std, 2),
        "unique": unique,
        "dark_ratio": round(dark_ratio, 3),
        "decoded": decoded,
        "decoded_len": len(decoded_text or ""),
        "reason": reason,
    }


def validate_qr_data_uri(qr_code: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """校验 data URI/纯 base64；通过时返回规范化的 data:image/png;base64,... 。"""
    raw = _decode_base64_image(qr_code)
    info = validate_qr_bytes(raw)
    if not info.get("ok") or raw is None:
        return False, "", info
    return True, "data:image/png;base64," + base64.b64encode(raw).decode("ascii"), info


class QrCapture:
    """替换 tools.utils.show_qrcode：PIL 校验通过才保存 PNG + 发 login.qrcode 事件。"""

    def __init__(self, emitter: EventEmitter, data_dir: Path, platform: str) -> None:
        self.emitter = emitter
        self.platform = platform
        self.qr_dir = Path(data_dir) / "qr"
        self.qr_dir.mkdir(parents=True, exist_ok=True)
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._waiting_sent = False
        self.count = 0
        self.invalid_count = 0
        self.last_invalid: Optional[Dict[str, Any]] = None

    def _decode(self, qr_code: Any) -> Optional[bytes]:
        return _decode_base64_image(qr_code)

    def __call__(self, qr_code: Any) -> None:
        raw = self._decode(qr_code)
        if not raw:
            self.invalid_count += 1
            print("[engine] show_qrcode 收到空/无法解码的二维码数据，已忽略", flush=True)
            return
        valid = validate_qr_bytes(raw)
        if valid.get("ok") and self.platform == "tieba" and QR_TIEBA_STRICT_DECODE and not valid.get("decoded"):
            valid = dict(valid)
            valid["ok"] = False
            valid["reason"] = f"tieba_not_decodable({valid.get('reason') or 'no_cv2_decode'})"
        if not valid.get("ok"):
            self.invalid_count += 1
            self.last_invalid = valid
            print(
                f"[engine] show_qrcode 收到无效二维码图片（{valid.get('reason')}），不发送 login.qrcode",
                flush=True,
            )
            return
        sha = hashlib.sha256(raw).hexdigest()
        with self._lock:
            if sha in self._seen:  # 同图去重：不重复推 UI、seq 不递增
                return
            self._seen.add(sha)
            self.count += 1
        png_path = self.qr_dir / f"{self.platform}_{sha[:8]}.png"
        try:
            if not png_path.is_file():
                png_path.write_bytes(raw)
        except OSError:
            pass
        data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        self.emitter.qrcode(image=data_uri, file=str(png_path), nbytes=len(raw))
        if not self._waiting_sent:
            self._waiting_sent = True
            self.emitter.status("waiting_qr", "二维码已就绪，请用手机 App 扫码")


def install_qr_capture(mc: Dict[str, Any], emitter: EventEmitter, data_dir: Path, platform: str) -> QrCapture:
    capture = QrCapture(emitter, data_dir, platform)
    mc["utils"].show_qrcode = capture
    mc["crawler_util"].show_qrcode = capture
    return capture


# --------------------------------------------------------------------------------------
# Patch 2b：登录验证页检测 + 人工接管等待（短信验证码 / 滑块 / 拼图 / 安全验证）
# --------------------------------------------------------------------------------------
async def _safe_page_title(page: Any) -> str:
    try:
        return str(await page.title() or "")
    except Exception:
        return ""


async def _safe_page_url(page: Any) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


async def _safe_page_inner_text(page: Any, limit: int = 20000) -> str:
    """取页面可见文本：innerText 不包含 display:none 的隐藏登录表单，避免误判。"""
    if page is None:
        return ""
    try:
        text = await page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)")
        return str(text or "")[:limit]
    except Exception:
        try:
            return str(await page.content() or "")[:limit]
        except Exception:
            return ""


def _manual_message(kind: str) -> str:
    label = MANUAL_KIND_LABELS.get(kind, "安全验证")
    if kind == "sms":
        action = "输入手机收到的短信验证码"
    elif kind == "slider":
        action = "拖动滑块/完成拼图"
    else:
        action = "按页面提示完成验证"
    return (
        f"[kind={kind}] 检测到{label}：请在弹出的浏览器窗口完成验证（{action}），"
        "完成后会自动继续登录；如窗口已关闭，请重新发起登录"
    )


async def _has_visible_sms_input(page: Any) -> bool:
    """已废弃：普通登录页也可能展示可见短信验证码输入框，不能作为二次验证依据。"""
    return False


async def detect_verification(page: Any) -> Optional[Dict[str, Any]]:
    """检测当前页面是否处于平台二次验证页。返回 {kind, reason, title, url} 或 None。

    kind: sms（短信验证码）/ slider（滑块/拼图）/ captcha（其他安全验证）
    判定依据仅限：标题 + URL + 可见文本（innerText）；不使用输入框 placeholder / “验证码登录” tab。
    短信(kind=sms)是否真正触发 need_manual 还要满足“二维码已消失/登录弹窗已切换”，由上层闸门判断。
    """
    if page is None:
        return None
    title = await _safe_page_title(page)
    url = await _safe_page_url(page)
    text = await _safe_page_inner_text(page)
    blob = f"{title}\n{text}"
    low_url = url.lower()

    for phrase in SMS_PHRASES:
        if phrase in blob:
            return {"kind": "sms", "reason": phrase, "title": title[:160], "url": url[:300]}

    reason = ""
    for phrase in SLIDER_PHRASES:
        if phrase in blob:
            reason = phrase
            break
    title_hit = next((hint for hint in SLIDER_TITLE_HINTS if hint in title), "")
    url_hit = next((hint for hint in VERIFY_URL_HINTS if hint in low_url), "")
    if reason or title_hit or url_hit:
        is_slider = reason in ("拖动滑块", "拼图", "滑块验证", "按住左边", "点击按钮进行验证") or title_hit
        return {
            "kind": "slider" if is_slider else "captcha",
            "reason": reason or title_hit or f"url:{url_hit}",
            "title": title[:160],
            "url": url[:300],
        }
    return None


class ManualGate:
    """人工接管闸门：检测到验证页 → 发 login.status need_manual(kind=...)；验证结束后继续。

    同一 kind 在 20s 内不重复发 need_manual；验证结束会发一次 waiting_scan 提示“继续登录”。
    """

    def __init__(
        self,
        emitter: EventEmitter,
        platform: str,
        timeout: float = MANUAL_WAIT_SECONDS,
    ) -> None:
        self.emitter = emitter
        self.platform = platform
        self.timeout = float(timeout)
        self.active_kind: Optional[str] = None
        self._last_emit_ts = 0.0
        self._lock = threading.Lock()
        self.manual_events = 0

    def report(self, info: Optional[Dict[str, Any]]) -> bool:
        """报告一次检测结果；返回是否新发出 need_manual。"""
        emitted = False
        with self._lock:
            if info is None:
                if self.active_kind:
                    self.active_kind = None
                    self._last_emit_ts = 0.0
                    self.emitter.status("waiting_scan", "验证已完成，继续登录…")
                return False
            kind = str(info.get("kind") or "captcha")
            now = time.monotonic()
            if kind != self.active_kind or (now - self._last_emit_ts) >= 20.0:
                self.active_kind = kind
                self._last_emit_ts = now
                self.manual_events += 1
                print(
                    f"[engine] 检测到验证页（platform={self.platform}, kind={kind}, reason={info.get('reason', '')!r}）"
                    "，等待用户在浏览器窗口完成…",
                    flush=True,
                )
                self.emitter.status("need_manual", _manual_message(kind), kind=kind)
                emitted = True
        return emitted

    async def report_async(self, info: Optional[Dict[str, Any]], page: Any = None) -> bool:
        """report 的异步版：首次发 need_manual 时把登录浏览器窗口置前（用户实测需要）。"""
        emitted = self.report(info)
        if emitted and page is not None:
            try:
                if hasattr(page, "bring_to_front"):
                    await page.bring_to_front()
                    print(f"[engine] 已将登录浏览器窗口置前（platform={self.platform}），请在窗口内完成验证", flush=True)
            except Exception:
                pass
        return emitted

    async def wait_clear(
        self,
        page: Any,
        timeout: Optional[float] = None,
        poll: float = 2.0,
    ) -> bool:
        """轮询等待验证页消失；返回 True=已无验证（或从未出现），False=超时仍在验证。"""
        import asyncio

        wait_timeout = self.timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + max(0.2, wait_timeout)
        saw_verification = False
        while time.monotonic() < deadline:
            info = await detect_verification(page)
            if info:
                saw_verification = True
                await self.report_async(info, page)
                await asyncio.sleep(poll)
                continue
            if not saw_verification:
                self.report(None)
                return True
            # 已从验证页恢复：再确认一次，避免页面跳转瞬间误判
            await asyncio.sleep(min(poll, 1.0))
            if await detect_verification(page):
                continue
            self.report(None)
            return True
        print(
            f"[engine] 人工验证等待超时（{wait_timeout:.0f}s，platform={self.platform}，kind={self.active_kind}）",
            flush=True,
        )
        return False


async def wait_manual_before_begin(
    page: Any,
    emitter: EventEmitter,
    platform: str,
    manual: Optional[ManualGate],
    poll: float = 2.0,
) -> bool:
    """登录流程开始前：若页面已是验证页（dy/tieba 整页滑块、安全验证），先等用户完成。"""
    if page is None or manual is None:
        return True
    info = await detect_verification(page)
    if info is None:
        return True
    if str(info.get("kind")) == "sms":
        # 登录前不因普通手机号登录表单（含“获取验证码”）触发人工等待；短信二次验证只在出码后处理
        return True
    print(f"[engine] {platform} 登录前检测到验证页（kind={info.get('kind')}），等待人工处理后继续登录", flush=True)
    return await manual.wait_clear(page, poll=poll)


# --------------------------------------------------------------------------------------
# Patch 3：Playwright 浏览器 channel（默认系统 Edge，免装 Playwright Chromium）
# --------------------------------------------------------------------------------------
_BROWSER_PATCH: Dict[str, Any] = {}


def patch_browser_channel(channel: Optional[str] = None) -> Dict[str, Any]:
    """把 BrowserType.launch / launch_persistent_context 的 channel 强制为指定值。

    - channel 为 None 时取环境变量 BROWSER_CHANNEL，再回退 DEFAULT_CHANNEL(msedge)
    - channel 为空串 / "none"：不 patch（允许走 Playwright 默认 Chromium）
    - 对 MediaCrawler 各平台显式传入的 channel="chrome" 同样覆盖为 Edge，
      避免目标机未装 Chrome 时启动失败（可用环境变量改回 chrome）
    """
    raw = channel if channel is not None else os.environ.get("BROWSER_CHANNEL", DEFAULT_CHANNEL)
    channel = (raw or "").strip()
    if channel.lower() in ("", "none", "default"):
        return {"channel": None, "patched": False}
    if _BROWSER_PATCH.get("channel") == channel:
        return {"channel": channel, "patched": True}

    from playwright.async_api import BrowserType

    if "orig_launch_persistent_context" not in _BROWSER_PATCH:
        _BROWSER_PATCH["orig_launch_persistent_context"] = BrowserType.launch_persistent_context
        _BROWSER_PATCH["orig_launch"] = BrowserType.launch

    async def _launch_persistent_context(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["channel"] = channel
        return await _BROWSER_PATCH["orig_launch_persistent_context"](self, *args, **kwargs)

    async def _launch(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["channel"] = channel
        return await _BROWSER_PATCH["orig_launch"](self, *args, **kwargs)

    BrowserType.launch_persistent_context = _launch_persistent_context
    BrowserType.launch = _launch
    _BROWSER_PATCH["channel"] = channel
    return {"channel": channel, "patched": True}


# --------------------------------------------------------------------------------------
# Patch 3b：Page.goto 导航重试（修复微博 passport 页 ERR_CONNECTION_CLOSED/RESET 抖动）
# --------------------------------------------------------------------------------------
_PAGE_GOTO_PATCH: Dict[str, Any] = {}

GOTO_RETRYABLE_ERRORS = (
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_ABORTED",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "ERR_ABORTED",
    "ERR_FAILED",
    "ERR_NETWORK_CHANGED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_HTTP2",
    "net::ERR_",
    "Timeout",
    "timed out",
)


def install_goto_retry(emitter: Optional[EventEmitter] = None, attempts: int = 5, base_delay: float = 1.0) -> Dict[str, Any]:
    """包装 Page.goto：对网络类错误自动重试（默认 1 次初始 + 4 次重试，1~2s 退避）。

    attempts 为总尝试次数（=重试次数+1）。重试过程写原始日志（sidecar 可见）并尽量发 progress 事件。
    """
    if _PAGE_GOTO_PATCH.get("installed"):
        return {"installed": True, "attempts": _PAGE_GOTO_PATCH.get("attempts", attempts)}

    from playwright.async_api import Page

    _PAGE_GOTO_PATCH.update(installed=True, attempts=int(attempts), original=Page.goto)

    async def _goto_with_retry(self, url, *args, **kwargs):  # type: ignore[no-untyped-def]
        import asyncio

        # 单次导航超时上限（默认 15s）：避免一次慢连接吃光整个登录等待预算，尽快进入重试
        if "timeout" not in kwargs:
            try:
                kwargs["timeout"] = int(os.environ.get("ENGINE_GOTO_TIMEOUT_MS", "15000"))
            except (TypeError, ValueError):
                kwargs["timeout"] = 15000
        last_exc: Optional[BaseException] = None
        total = max(1, int(_PAGE_GOTO_PATCH.get("attempts") or attempts))
        for attempt in range(1, total + 1):
            try:
                result = await _PAGE_GOTO_PATCH["original"](self, url, *args, **kwargs)
                if attempt > 1:
                    line = f"[engine] Page.goto 第 {attempt} 次尝试成功：{str(url)[:140]}"
                    print(line, flush=True)
                    if emitter is not None:
                        emitter.progress("login", 0, 0, f"导航重试成功（第 {attempt} 次）：{str(url)[:100]}")
                return result
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                text = str(exc)
                retryable = any(key in text for key in GOTO_RETRYABLE_ERRORS)
                if attempt >= total or not retryable:
                    raise
                delay = 0.0 if base_delay <= 0 else min(2.0, max(1.0, base_delay * attempt))
                first_line = (text.splitlines() or [text])[0][:180]
                line = (
                    f"[engine] Page.goto 失败（{attempt}/{total}），{delay:.1f}s 后重试："
                    f"{str(url)[:140]} | {first_line}"
                )
                print(line, flush=True)
                if emitter is not None:
                    emitter.progress("login", 0, 0, f"导航失败，{delay:.1f}s 后重试（{attempt}/{total}）：{first_line[:100]}")
                await asyncio.sleep(delay)
        if last_exc is not None:  # pragma: no cover - 循环必然 return/raise
            raise last_exc
        return None

    Page.goto = _goto_with_retry  # type: ignore[assignment]
    return {"installed": True, "attempts": int(attempts)}


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


# --------------------------------------------------------------------------------------
# Patch 4c：快手搜索首屏网页真实 payload
# --------------------------------------------------------------------------------------
# 背景：MediaCrawler KuaishouCrawler.search() 传 pcursor=str(page)（首页 "1"），但快手网页
# 真实搜索首屏请求体是 {"keyword","page":"search","webPageArea":"","pcursor":""}；
# 服务端把无效游标 "1" 当推荐流，返回无人机/打工等无关内容。这里首屏改用网页真实 payload，
# 后续页复用响应里的 pcursor/searchSessionId。
KS_SEARCH_WEB_URI = "/rest/v/search/feed"
KS_MAX_NOTES_PER_PAGE = 20
# 关键词相关过滤：去掉这些通用后缀得到“核心词”（示例科技 -> 示例）
_KS_GENERIC_SUFFIXES = (
    "信息技术",
    "有限公司",
    "公司",
    "集团",
    "股份",
    "科技",
    "智能",
    "网络",
    "电子",
    "实业",
    "发展",
    "有限",
    "传媒",
    "文化",
)


def _ks_web_style_enabled() -> bool:
    raw = str(os.environ.get("ENGINE_KS_SEARCH_WEB_STYLE", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off", "")


def _ks_client_class() -> Any:
    from media_platform.kuaishou.client import KuaiShouClient

    return KuaiShouClient


def _ks_relevance_tokens(keyword: Any) -> List[str]:
    """关键词相关词：keyword 本身 + 去掉通用后缀的核心词 + ENGINE_KS_RELEVANCE_EXTRA 追加。"""
    tokens: List[str] = []
    lowered: set = set()

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.lower() not in lowered:
            lowered.add(text.lower())
            tokens.append(text)

    raw = str(keyword or "").strip()
    add(raw)
    core = raw
    changed = True
    while changed and core:
        changed = False
        for suffix in _KS_GENERIC_SUFFIXES:
            if len(core) > len(suffix) and core.lower().endswith(suffix.lower()):
                core = core[: -len(suffix)].strip()
                changed = True
                break
    add(core)
    extra = os.environ.get("ENGINE_KS_RELEVANCE_EXTRA", "")
    for part in re.split(r"[,，;；\s]+", str(extra)):
        add(part)
    return tokens


def _ks_feed_text(feed: Dict[str, Any]) -> str:
    """提取用于相关判定的文本：caption/title/desc + 作者昵称。"""
    if not isinstance(feed, dict):
        return ""
    photo = feed.get("photo") or {}
    author = feed.get("author") or {}
    parts = [
        photo.get("caption") if isinstance(photo, dict) else "",
        photo.get("desc") if isinstance(photo, dict) else "",
        feed.get("title"),
        feed.get("desc"),
        author.get("name") if isinstance(author, dict) else "",
        feed.get("nickname"),
    ]
    return " ".join(str(part) for part in parts if part).strip()


def _ks_feed_matches(feed: Dict[str, Any], tokens: List[str]) -> bool:
    if not tokens:
        return True
    text = _ks_feed_text(feed).lower()
    return any(token.lower() in text for token in tokens)


def _ks_filter_feeds(response: Any, keyword: Any) -> Any:
    """过滤搜索响应里的推荐补位：只保留命中相关词的 feeds（返回给 core 前过滤）。

    过滤后 core（KuaishouCrawler.search）自然只入库相关条目，评论也只抓这些。
    token 为空时（保守）不过滤。
    """
    if not isinstance(response, dict):
        return response
    feeds = response.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        return response
    tokens = _ks_relevance_tokens(keyword)
    if not tokens:
        return response
    kept = [feed for feed in feeds if _ks_feed_matches(feed, tokens)]
    dropped = len(feeds) - len(kept)
    if dropped:
        print(
            f"[engine] ks 过滤推荐补位：in={len(feeds)} out={len(kept)}"
            f"（keyword={str(keyword)!r} tokens={tokens}）",
            flush=True,
        )
    filtered = dict(response)
    filtered["feeds"] = kept
    return filtered


def patch_kuaishou_search(client_cls: Any = None) -> bool:
    """快手搜索修复（幂等）：首屏 pcursor="" + webPageArea=""，后续页复用服务端游标。

    - 仅平台 ks 时安装；ENGINE_KS_SEARCH_WEB_STYLE=0 时跳过（回退 MediaCrawler 旧行为）。
    - 请求仍走 client.request_rest_v2_signed，保持 __NS_hxfalcon 签名。
    - 返回 True 表示已安装/已安装过；False 表示环境变量关闭或安装失败。
    """
    if not _ks_web_style_enabled():
        print("[engine] ENGINE_KS_SEARCH_WEB_STYLE=0：跳过快手网页真实搜索 payload 补丁（回退旧行为）", flush=True)
        return False
    if client_cls is None:
        try:
            client_cls = _ks_client_class()
        except Exception as exc:  # noqa: BLE001
            print(f"[engine] 快手搜索结果客户端导入失败，跳过补丁：{type(exc).__name__}: {exc}", flush=True)
            return False
    if getattr(client_cls, "_engine_ks_search_patched", False):
        return True
    original_search = client_cls.search_info_by_keyword_v2

    async def _wrapped_search(self, keyword, pcursor="", search_session_id=""):
        state = getattr(self, "_engine_ks_search_state", None)
        if not isinstance(state, dict):
            state = {}
            try:
                setattr(self, "_engine_ks_search_state", state)
            except Exception:
                pass
        kw = str(keyword)
        pcursor_text = str(pcursor if pcursor is not None else "")
        try:
            page_num = int(pcursor_text.strip()) if pcursor_text.strip().isdigit() else None
        except Exception:
            page_num = None
        entry_state = state.get(kw)
        # 首屏：该 keyword 无状态 / 页码 1 / 旧逻辑首页传入 "1"
        if entry_state is None or page_num == 1 or pcursor_text.strip() == "1":
            payload = {"keyword": keyword, "page": "search", "webPageArea": "", "pcursor": ""}
            response = await self.request_rest_v2_signed(KS_SEARCH_WEB_URI, payload)
            raw_feeds = len((response or {}).get("feeds") or [])
            response = _ks_filter_feeds(response, keyword)
            next_cursor = str((response or {}).get("pcursor") or "").strip()
            session = str((response or {}).get("searchSessionId") or "").strip()
            state[kw] = {"pcursor": next_cursor, "searchSessionId": session}
            feeds = (response or {}).get("feeds") or []
            print(
                f"[engine] ks 搜索首屏使用网页真实 payload（pcursor=\"\", webPageArea=\"\"）"
                f"keyword={kw!r} feeds={raw_feeds}->{len(feeds)} next_pcursor={next_cursor[:24]!r}",
                flush=True,
            )
            return response
        cursor = str((entry_state or {}).get("pcursor") or "").strip()
        session = str((entry_state or {}).get("searchSessionId") or search_session_id or "").strip()
        if cursor:
            payload = {"keyword": keyword, "page": "search", "webPageArea": "", "pcursor": cursor}
            if session:
                payload["searchSessionId"] = session
            response = await self.request_rest_v2_signed(KS_SEARCH_WEB_URI, payload)
            raw_feeds = len((response or {}).get("feeds") or [])
            response = _ks_filter_feeds(response, keyword)
            state[kw] = {
                "pcursor": str((response or {}).get("pcursor") or "").strip(),
                "searchSessionId": str((response or {}).get("searchSessionId") or session).strip(),
            }
            print(
                f"[engine] ks 搜索后续页复用服务端游标（pcursor={cursor[:24]!r}，"
                f"searchSessionId={'有' if session else '无'}）feeds={raw_feeds}->{len((response or {}).get('feeds') or [])}",
                flush=True,
            )
            return response
        print("[engine] ks 后续页缺少服务端游标，回退 MediaCrawler 原搜索逻辑", flush=True)
        fallback_response = await original_search(self, keyword, pcursor, search_session_id)
        return _ks_filter_feeds(fallback_response, keyword)

    client_cls.search_info_by_keyword_v2 = _wrapped_search
    client_cls._engine_ks_search_patched = True
    print("[engine] 已安装快手搜索补丁：首屏 pcursor=\"\"+webPageArea=\"\"（ENGINE_KS_SEARCH_WEB_STYLE=0 可回退）", flush=True)
    return True


_WEIBO_COMMENT_EMPTY_PATCH: Dict[str, Any] = {}
WEIBO_NO_COMMENT_MARKERS = (
    "还没有人评论",
    "还没有评论",
    "暂无评论",
    "没有评论",
    "快来抢沙发",
    "评论已关闭",
    "已关闭评论",
)


def _is_weibo_no_comment_message(text: str) -> bool:
    value = str(text or "")
    return any(marker in value for marker in WEIBO_NO_COMMENT_MARKERS)


def patch_weibo_comment_empty() -> bool:
    """微博「这条微博没有评论」不再当成 ERROR/失败（幂等）。

    现象：m.weibo.cn/comments/hotflow 对无评论微博返回
    ``{"ok":0,"msg":"还没有人评论哦~快来抢沙发！"}``。MediaCrawler 把 ok=0
    一律当错误：先 ERROR 日志，再 tenacity 重试 5 次（每次 3s），最后抛
    DataFetchError，采集日志里会反复出现 ERROR，且无谓拖慢采集。

    本 patch 对评论接口做一次请求：识别无评论文案后返回空评论结构
    （max_id=0 让调用方立即结束），并抑制这条预期内的 ERROR 日志；
    其它评论接口错误仍按原逻辑抛出。
    """
    if _WEIBO_COMMENT_EMPTY_PATCH.get("installed"):
        return True
    try:
        client_mod = importlib.import_module("media_platform.weibo.client")
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 微博空评论补丁导入客户端失败，跳过：{type(exc).__name__}: {exc}", flush=True)
        return False
    client_cls = getattr(client_mod, "WeiboClient", None)
    if client_cls is None:
        return False
    if getattr(client_cls, "_engine_wb_comment_empty_patched", False):
        _WEIBO_COMMENT_EMPTY_PATCH["installed"] = True
        return True

    original_request = client_cls.request
    # tenacity 装饰器可能有多层（async_wrapped -> wrapped_f -> 原始 async 函数）；
    # 一直解到真正的协程函数，避免「补丁里又触发 5 次重试」。
    base_request = original_request
    while getattr(base_request, "__wrapped__", None) is not None:
        base_request = base_request.__wrapped__
    utils_mod = getattr(client_mod, "utils", None)
    data_fetch_error = getattr(client_mod, "DataFetchError", Exception)

    async def _request_with_empty_comment_handling(self, method, url, **kwargs):  # type: ignore[no-untyped-def]
        if "/comments/hotflow" not in str(url):
            return await original_request(self, method, url, **kwargs)
        logger = getattr(utils_mod, "logger", None) if utils_mod is not None else None
        original_error = getattr(logger, "error", None) if logger is not None else None

        def _filtered_error(message, *args, **kw):  # type: ignore[no-untyped-def]
            # 抑制「还没有人评论」这类正常业务响应，不污染采集日志
            if _is_weibo_no_comment_message(str(message)):
                return
            if original_error is not None:
                original_error(message, *args, **kw)

        if logger is not None and original_error is not None:
            logger.error = _filtered_error
        try:
            return await base_request(self, method, url, **kwargs)
        except data_fetch_error as exc:  # type: ignore[misc]
            if _is_weibo_no_comment_message(str(exc)):
                print(
                    f"[engine] 微博评论为空（{str(exc)[:80]}），按 0 条处理（不重试、不报错）",
                    flush=True,
                )
                return {"max_id": 0, "max_id_type": 0, "data": []}
            raise
        finally:
            if logger is not None and original_error is not None:
                logger.error = original_error

    client_cls.request = _request_with_empty_comment_handling  # type: ignore[assignment]
    client_cls._engine_wb_comment_empty_patched = True  # type: ignore[attr-defined]
    _WEIBO_COMMENT_EMPTY_PATCH["installed"] = True
    print("[engine] 已安装微博空评论补丁（无评论不再 ERROR/重试）", flush=True)
    return True


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
# Patch 4b：二维码查找兜底（原选择器取不到时：先人工验证，再按平台 fallback 选择器）
# --------------------------------------------------------------------------------------
_QR_FALLBACK_STATE: Dict[str, Any] = {}


async def _page_user_agent(page: Any) -> str:
    try:
        return str(await page.evaluate("() => navigator.userAgent") or "")
    except Exception:
        return ""


async def _wait_image_loaded(element: Any, timeout: float = QR_IMAGE_LOAD_WAIT_S) -> bool:
    """等待 <img> 加载完成（naturalWidth>0）。非 img / 无法评估时返回 True（交给截图判断）。"""
    import asyncio

    js = (
        "el => { if (!el || el.tagName !== 'IMG') return {img:false};"
        " return {img:true, complete: !!el.complete, nw: el.naturalWidth||0}; }"
    )
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        try:
            info = await element.evaluate(js)
        except Exception:
            return True
        if not isinstance(info, dict) or not info.get("img"):
            return True
        if info.get("complete") and int(info.get("nw") or 0) > 0:
            return True
        await asyncio.sleep(0.25)
    return False


async def _http_download_qr(src: str, page: Any) -> Optional[bytes]:
    """http(s) 二维码 src 的兜底下载（仅当元素截图无效时用；httpx 重试 3 次并 PIL 校验）。"""
    import asyncio

    import httpx

    if not src.lower().startswith(("http://", "https://")):
        return None
    headers = {
        "User-Agent": await _page_user_agent(page) or DEFAULT_QR_USER_AGENT,
        "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    }
    try:
        if getattr(page, "url", ""):
            headers["Referer"] = str(page.url)
    except Exception:
        pass
    for attempt in range(1, QR_HTTP_RETRIES + 1):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                response = await client.get(src, headers=headers)
            if response.status_code == 200 and response.content:
                return response.content
            print(
                f"[engine] 二维码 httpx 下载失败（{attempt}/{QR_HTTP_RETRIES}）：status={response.status_code}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[engine] 二维码 httpx 下载异常（{attempt}/{QR_HTTP_RETRIES}）：{type(exc).__name__}: {str(exc)[:120]}",
                flush=True,
            )
        if attempt < QR_HTTP_RETRIES:
            await asyncio.sleep(min(2.0, 0.8 * attempt))
    return None


async def _qr_candidate_from_element(
    element: Any,
    page: Any,
    require_decode: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """从元素获取有效二维码（返回 data URI + 校验信息）：

    1) data:image src -> 直接解码校验
    2) 等 <img> naturalWidth 加载完成
    3) **优先 Playwright 元素截图**（浏览器内已渲染，最可靠；http src 也走这里）
    4) 截图无效才用 httpx 下载 http(s) src（重试 3 次）
    每一步结果都必须通过 PIL 几何/纹理/宽高比校验；require_decode=True 时还要求 cv2 能解码。
    """
    if element is None:
        return "", {"ok": False, "reason": "no_element"}

    def _accept(info: Dict[str, Any]) -> bool:
        if not info.get("ok"):
            return False
        if require_decode and not info.get("decoded"):
            return False
        return True

    src = ""
    try:
        value = await element.get_property("src")
        src = str(value) if value is not None else ""
    except Exception:
        pass
    last_invalid: Dict[str, Any] = {"ok": False, "reason": "no_qr_data"}

    if src.startswith("data:image"):
        ok, uri, info = validate_qr_data_uri(src)
        if ok and _accept(info):
            return uri, info
        last_invalid = info
        print(f"[engine] data:image 二维码无效（{info.get('reason')}，decoded={info.get('decoded')}），尝试元素截图", flush=True)

    loaded = await _wait_image_loaded(element)
    if not loaded:
        print(f"[engine] <img> 未加载完成（naturalWidth=0，等待 {QR_IMAGE_LOAD_WAIT_S:.0f}s），尝试截图/下载兜底", flush=True)

    # 有头窗口可能被其他窗口遮挡/未激活，导致元素截图拿到未绘制的空白图：先置前+滚动到可视区，
    # 并给一次渲染时间；截图无效时最多重试 3 次（每轮 0.6s）。
    try:
        if hasattr(page, "bring_to_front"):
            await page.bring_to_front()
    except Exception:
        pass
    try:
        if hasattr(element, "scroll_into_view_if_needed"):
            await element.scroll_into_view_if_needed(timeout=2000)
    except Exception:
        pass
    import asyncio

    await asyncio.sleep(0.4)
    for shot_attempt in range(1, 4):
        try:
            shot = await element.screenshot(timeout=5000)
            if shot:
                info = validate_qr_bytes(shot)
                if _accept(info):
                    print(
                        f"[engine] 元素截图获取二维码成功（{info.get('width')}x{info.get('height')}, "
                        f"aspect={info.get('aspect')}, std={info.get('std')}, decoded={info.get('decoded')}, "
                        f"bytes={len(shot)}, attempt={shot_attempt}）",
                        flush=True,
                    )
                    return "data:image/png;base64," + base64.b64encode(shot).decode("ascii"), info
                last_invalid = info
                print(
                    f"[engine] 元素截图二维码无效（{info.get('reason')}，decoded={info.get('decoded')}，attempt={shot_attempt}）",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[engine] 元素截图失败（attempt={shot_attempt}）：{type(exc).__name__}: {str(exc)[:120]}", flush=True)
        if shot_attempt < 3:
            await asyncio.sleep(0.6)

    if src.lower().startswith(("http://", "https://")):
        data = await _http_download_qr(src, page)
        if data:
            info = validate_qr_bytes(data)
            if _accept(info):
                print(
                    f"[engine] httpx 下载二维码成功（{info.get('width')}x{info.get('height')}, "
                    f"aspect={info.get('aspect')}, bytes={len(data)}）",
                    flush=True,
                )
                return "data:image/png;base64," + base64.b64encode(data).decode("ascii"), info
            last_invalid = info
            print(f"[engine] httpx 下载内容非有效二维码（{info.get('reason')}，decoded={info.get('decoded')}）", flush=True)
    return "", last_invalid


async def _qr_already_visible(page: Any, platform: str) -> bool:
    """页面上是否已有“几何合法（近似正方形且足够大）”的二维码元素。

    仅 is_visible 不够：贴吧/抖音登录页可能已有横幅/占位图（例如 948x168 或 327x184），
    这里用 boundingBox 做一次几何过滤，避免因此跳过“点击扫码登录 tab”等必要动作。
    """
    if page is None:
        return False
    for selector in QR_FALLBACK_SELECTORS.get(platform) or DEFAULT_QR_FALLBACK_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(int(count), 5)):
            try:
                item = locator.nth(index)
                if not await item.is_visible():
                    continue
                try:
                    box = await item.bounding_box()
                except Exception:
                    box = None
                if _element_geometry(box).get("ok"):
                    return True
            except Exception:
                continue
    return False


async def _prepare_qr_view(page: Any, platform: str) -> bool:
    """部分平台（微博）默认显示手机号登录页，需要点击“扫码登录”标签才渲染二维码。"""
    import asyncio

    if page is None:
        return False
    selectors = QR_VIEW_ENTRY_SELECTORS.get(platform) or []
    if not selectors:
        return False
    if await _qr_already_visible(page, platform):
        return True
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if not await locator.is_visible():
                continue
            await locator.click(timeout=3000)
            print(f"[engine] {platform} 点击二维码入口成功：{selector}", flush=True)
            await asyncio.sleep(1.5)
            return True
        except Exception:
            continue
    return False


async def _find_qr_element(page: Any, selector: str, wait_ms: int = 0) -> Any:
    if page is None or not selector:
        return None
    try:
        element = await page.query_selector(selector)
    except Exception:
        element = None
    if element is not None:
        return element
    if wait_ms > 0:
        try:
            await page.wait_for_selector(selector, timeout=wait_ms)
        except Exception:
            return None
        try:
            return await page.query_selector(selector)
        except Exception:
            return None
    return None


def _element_geometry(box: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """候选元素 boundingBox 几何过滤：宽高>=80、宽高比 0.75~1.35、单边<=1200、面积适中。

    box 不可知时返回 ok=True（交给后续 PIL 校验），但已知几何的候选必须过校验，
    避免把容器/横幅（如贴吧 948x168 qrcode-card-header）当二维码截图。
    """
    if not box:
        return {"ok": True, "reason": "unknown", "aspect": 1.0}
    try:
        width = float(box.get("width") or 0)
        height = float(box.get("height") or 0)
    except Exception:
        return {"ok": True, "reason": "unknown", "aspect": 1.0}
    if width < QR_MIN_SIDE or height < QR_MIN_SIDE:
        return {"ok": False, "reason": f"box_size:{int(width)}x{int(height)}"}
    if max(width, height) > QR_MAX_SIDE:
        return {"ok": False, "reason": f"box_too_large:{int(width)}x{int(height)}"}
    if width * height > QR_MAX_AREA:
        return {"ok": False, "reason": f"box_area:{int(width * height)}"}
    aspect = width / height
    if not (QR_MIN_ASPECT_RATIO <= aspect <= QR_MAX_ASPECT_RATIO):
        return {"ok": False, "reason": f"box_aspect:{int(width)}x{int(height)}({aspect:.2f})"}
    return {"ok": True, "aspect": aspect, "width": round(width, 1), "height": round(height, 1)}


async def inspect_qr_candidates(
    page: Any,
    platform: str,
    extra_selectors: Optional[List[str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """按 原选择器 + 平台 fallback 选择器 收集候选：

    1) 元素可见 + boundingBox 几何过滤（非正方形横幅/容器直接丢弃）；
    2) 按“最接近正方形”排序，其次按选择器顺序；
    3) 元素截图优先（PIL 几何/纹理/宽高比校验，贴吧还要求 cv2 可解码），httpx 兜底。
    """
    if page is None:
        return "", {"ok": False, "reason": "no_page"}
    selectors: List[str] = []
    for selector in list(extra_selectors or []) + list(
        QR_FALLBACK_SELECTORS.get(platform) or DEFAULT_QR_FALLBACK_SELECTORS
    ):
        if selector and selector not in selectors:
            selectors.append(selector)
    last_invalid: Dict[str, Any] = {"ok": False, "reason": "no_candidate"}
    candidates: List[Dict[str, Any]] = []
    for index, selector in enumerate(selectors):
        element = await _find_qr_element(page, selector, wait_ms=2000 if selector in (extra_selectors or []) else 0)
        if element is None:
            continue
        try:
            if hasattr(element, "is_visible") and not await element.is_visible():
                continue  # 隐藏的二维码不算（如手机号登录页里隐藏的 qrcode img）
        except Exception:
            pass
        box: Optional[Dict[str, Any]] = None
        try:
            if hasattr(element, "bounding_box"):
                box = await element.bounding_box()
        except Exception:
            box = None
        geometry = _element_geometry(box)
        if not geometry.get("ok"):
            print(
                f"[engine] 跳过非二维码候选元素（platform={platform}, selector={selector}, {geometry.get('reason')}）",
                flush=True,
            )
            last_invalid = {"ok": False, "reason": str(geometry.get("reason")), "selector": selector}
            continue
        candidates.append({"element": element, "selector": selector, "index": index, "geometry": geometry})
    candidates.sort(key=lambda item: (abs(1.0 - float(item["geometry"].get("aspect") or 1.0)), item["index"]))
    require_decode = platform == "tieba" and QR_TIEBA_STRICT_DECODE
    for candidate in candidates:
        selector = candidate["selector"]
        qr, info = await _qr_candidate_from_element(candidate["element"], page, require_decode=require_decode)
        if qr:
            info = dict(info or {})
            if selector not in (extra_selectors or []):
                info["fallback_selector"] = selector
                print(f"[engine] 二维码兜底选择器命中（platform={platform}, selector={selector}）", flush=True)
            return qr, info
        last_invalid = info or last_invalid
    return "", last_invalid


async def extract_qr_fallback(page: Any, platform: str) -> str:
    """兼容旧接口：返回有效二维码 data URI，失败返回 ""。"""
    qr, _ = await inspect_qr_candidates(page, platform)
    return qr


async def _reload_login_page(page: Any) -> bool:
    """重新加载当前登录页（reload 失败时走 Page.goto 重试包装）。"""
    import asyncio

    if page is None:
        return False
    url = ""
    try:
        url = str(page.url or "")
    except Exception:
        pass
    try:
        if hasattr(page, "reload"):
            await page.reload(wait_until="load", timeout=15000)
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 登录页 reload 失败：{type(exc).__name__}: {str(exc)[:140]}，改用 goto 重试", flush=True)
    try:
        if url:
            await page.goto(url, wait_until="load")  # 通过 install_goto_retry 包装（5 次尝试）
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 登录页重新导航失败：{type(exc).__name__}: {str(exc)[:140]}", flush=True)
    await asyncio.sleep(0.5)
    return False


async def _switch_weibo_to_desktop_qr_page(page: Any) -> bool:
    """微博专修：把当前 login 页切桌面 UA（CDP 覆盖 navigator+请求头）并打开 popup 登录页。

    背景：MediaCrawler 的微博浏览器上下文用 iPhone UA，passport 页默认移动版“短信验证登录”
    布局，切换扫码 tab 后二维码 img 在部分机器上截图空白；用户实测桌面 UA + popup URL 正常。
    只在“需要扫码（pong=false）”的登录流程内对当前 page 生效，不影响已登录探测/采集 API。
    """
    import asyncio

    if page is None:
        return False
    if getattr(page, "_engine_wb_desktop_qr", False):
        return True
    print("[engine] 微博登录页切换为桌面 UA 并打开 popup 登录页（修复二维码空白）", flush=True)
    cdp_ok = False
    try:
        context = getattr(page, "context", None)
        new_cdp = getattr(context, "new_cdp_session", None)
        if new_cdp is not None:
            cdp = await new_cdp(page)
            await cdp.send(
                "Network.setUserAgentOverride",
                {
                    "userAgent": WEIBO_QR_USER_AGENT,
                    "acceptLanguage": "zh-CN,zh;q=0.9",
                    "platform": "Win32",
                },
            )
            page._engine_wb_cdp_session = cdp  # type: ignore[attr-defined]
            cdp_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 微博 CDP 桌面 UA 覆盖失败（回退 extra headers）：{type(exc).__name__}: {str(exc)[:120]}", flush=True)
    if not cdp_ok:
        try:
            if hasattr(page, "set_extra_http_headers"):
                await page.set_extra_http_headers({"User-Agent": WEIBO_QR_USER_AGENT})
        except Exception:
            pass
    try:
        await page.goto(WEIBO_QR_LOGIN_URL, wait_until="load")  # 走 Page.goto 重试包装
        page._engine_wb_desktop_qr = True  # type: ignore[attr-defined]
        await asyncio.sleep(1.5)
        await _prepare_qr_view(page, "wb")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 微博 popup 登录页导航失败：{type(exc).__name__}: {str(exc)[:140]}", flush=True)
        return False


async def _switch_tieba_to_passport_qr_page(page: Any) -> bool:
    """贴吧专修：打开百度 passport 登录页（真实二维码），避免首页扫码卡只给占位图/横幅。

    现代贴吧首页的扫码登录卡是占位图（95x95 qrcode-placeholder）+ 948x168 横幅头图，
    真实二维码在 passport.baidu.com 登录页的 img.tang-pass-qrcode-img。
    """
    import asyncio

    if page is None:
        return False
    if getattr(page, "_engine_tieba_passport_qr", False):
        return True
    print("[engine] 贴吧登录页切换到百度 passport 二维码页（修复横幅误判）", flush=True)
    try:
        await page.goto(TIEBA_QR_LOGIN_URL, wait_until="load")  # 走 Page.goto 重试包装
        page._engine_tieba_passport_qr = True  # type: ignore[attr-defined]
        await asyncio.sleep(1.5)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 贴吧 passport 登录页导航失败：{type(exc).__name__}: {str(exc)[:140]}", flush=True)
        return False


async def _acquire_qr_once(
    page: Any,
    platform: str,
    selectors: List[str],
    manual: Optional[ManualGate],
) -> Tuple[str, Dict[str, Any]]:
    """单轮获取：切二维码页/tab -> 验证页等待 -> 选择器取码（内部已做几何/截图/http/PIL 校验）。"""
    if page is None:
        return "", {"ok": False, "reason": "no_page"}
    if platform == "wb":
        await _switch_weibo_to_desktop_qr_page(page)
    elif platform == "tieba":
        await _switch_tieba_to_passport_qr_page(page)
    if await _prepare_qr_view(page, platform):
        print(f"[engine] {platform} 二维码视图已就绪（扫码登录标签）", flush=True)
    info = await detect_verification(page)
    if info is not None and str(info.get("kind")) != "sms":
        print(f"[engine] 取二维码前检测到验证页（platform={platform}, kind={info.get('kind')}），等待人工完成…", flush=True)
        if manual is not None:
            await manual.wait_clear(page)
        else:
            import asyncio

            await asyncio.sleep(1)
    result = await inspect_qr_candidates(page, platform, extra_selectors=selectors)
    if result[0]:
        return result
    # 二次尝试：页面可能还停在手机号登录 tab（dy login-full-panel 等）→ 再点扫码入口后重取
    if await _prepare_qr_view(page, platform):
        await asyncio.sleep(0.8)
        result2 = await inspect_qr_candidates(page, platform, extra_selectors=selectors)
        if result2[0]:
            return result2
    return result


async def _run_qr_rounds(
    page: Any,
    platform: str,
    selectors: List[str],
    emitter: Optional[EventEmitter],
    manual: Optional[ManualGate],
    keepalive: bool = False,
) -> str:
    """有头登录：取码失败后保留浏览器窗口，改为等用户在浏览器里扫码。

    keepalive=False（无头登录/自测）保持原行为：3 轮重载后抛 QrLoadFailed；
    keepalive=True（登录模式，有可见 Edge 窗口）：只尝试一次，失败不重载页面
    （重载会让用户刚扫的二维码失效），返回占位图让 MediaCrawler 继续轮询登录态。
    """
    import asyncio

    last_invalid: Dict[str, Any] = {}
    rounds = 1 if keepalive else QR_MAX_ROUNDS
    for round_index in range(1, rounds + 1):
        qr, info = await _acquire_qr_once(page, platform, selectors, manual)
        if qr:
            if info.get("fallback_selector") and emitter is not None:
                emitter.progress(
                    "login",
                    0,
                    0,
                    f"原生二维码选择器未命中，已使用兜底选择器捕获二维码（{info.get('fallback_selector')}）",
                )
            if round_index > 1:
                print(f"[engine] 第 {round_index}/{QR_MAX_ROUNDS} 轮重试后拿到有效二维码", flush=True)
                if emitter is not None:
                    emitter.progress("login", 0, 0, f"第 {round_index} 轮重载后成功获取二维码")
            return qr
        last_invalid = info or {"ok": False, "reason": "invalid"}
        print(
            f"[engine] 第 {round_index}/{rounds} 轮二维码无效：{last_invalid.get('reason')}",
            flush=True,
        )
        if round_index >= rounds:
            break
        if emitter is not None:
            emitter.progress(
                "login",
                0,
                0,
                f"二维码图片未加载/无效，重载登录页重试（第 {round_index + 1}/{QR_MAX_ROUNDS} 轮）",
            )
        reloaded = await _reload_login_page(page)
        if not reloaded:
            print("[engine] 登录页重载失败，继续下一轮尝试", flush=True)
        await asyncio.sleep(1.2)
        if platform == "wb":
            # 重载后重新确认桌面 UA + 重新点“扫码登录”tab（popup 页默认已显示二维码）
            cdp = getattr(page, "_engine_wb_cdp_session", None)
            if cdp is not None:
                try:
                    await cdp.send(
                        "Network.setUserAgentOverride",
                        {
                            "userAgent": WEIBO_QR_USER_AGENT,
                            "acceptLanguage": "zh-CN,zh;q=0.9",
                            "platform": "Win32",
                        },
                    )
                except Exception:
                    pass
            await _prepare_qr_view(page, platform)
        elif platform == "tieba":
            # 重载后重新打开 passport 登录页，拿一张新二维码
            page._engine_tieba_passport_qr = False  # type: ignore[attr-defined]
            await _switch_tieba_to_passport_qr_page(page)
    if keepalive:
        message = "未能在应用内读取二维码，请直接在已打开的浏览器窗口扫码；登录成功后会自动继续"
        print(f"[engine] {message}（原因：{last_invalid.get('reason')}）", flush=True)
        if emitter is not None:
            emitter.status("waiting_scan", message)
        return QR_KEEPALIVE_PLACEHOLDER
    message = QR_FAIL_MESSAGE_TIEBA if platform == "tieba" else QR_FAIL_MESSAGE
    print(f"[engine] {message}（最后一轮原因：{last_invalid.get('reason')}）", flush=True)
    if emitter is not None:
        emitter.status("failed", message)
    raise QrLoadFailed(message)


async def _short_wait_original_find(original: Any, page: Any, selector: str, timeout_ms: int = QR_SHORT_WAIT_MS) -> str:
    """先短等待选择器出现（避免原生 find 默认 30s 卡死），出现后再调用原生逻辑。"""
    if page is None or not selector:
        return ""
    try:
        await page.wait_for_selector(selector, timeout=timeout_ms)
    except Exception:
        print(f"[engine] 原生二维码选择器未在 {timeout_ms}ms 内出现：{selector[:140]}", flush=True)
        return ""
    try:
        result = await original(page, selector)
        result = str(result or "")
        if not result:
            print(f"[engine] 原生二维码提取为空（selector={selector[:140]}），尝试兜底选择器", flush=True)
        return result
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 原生二维码提取异常：{type(exc).__name__}: {str(exc)[:160]}", flush=True)
        return ""


def install_qr_fallback(
    mc: Dict[str, Any],
    emitter: Optional[EventEmitter],
    platform: str,
    manual: Optional[ManualGate] = None,
    keepalive: bool = False,
) -> Dict[str, Any]:
    """包装 find_login_qrcode / find_qrcode_img_from_canvas：验证页等待 + 原选择器 + 平台兜底。"""
    utils = mc["utils"]
    crawler_util = mc["crawler_util"]
    if getattr(utils, "_engine_qr_fallback_installed", False):
        return {"installed": True}

    original_find = utils.find_login_qrcode
    original_canvas = utils.find_qrcode_img_from_canvas

    async def _find_with_fallback(page: Any, selector: Any = None, **kwargs) -> str:
        sel = selector if selector is not None else kwargs.get("selector")
        sel = str(sel or "")
        print(f"[engine] find_login_qrcode 包装器进入（platform={platform}, selector={sel[:140]}）", flush=True)
        state_keepalive = bool(_QR_FALLBACK_STATE.get("keepalive", keepalive))
        return await _run_qr_rounds(
            page, platform, [sel] if sel else [], emitter, manual, keepalive=state_keepalive
        )

    async def _find_canvas_with_fallback(page: Any, canvas_selector: Any = None, **kwargs) -> str:
        sel = canvas_selector if canvas_selector is not None else kwargs.get("canvas_selector")
        sel = str(sel or "")
        print(f"[engine] find_qrcode_img_from_canvas 包装器进入（platform={platform}, selector={sel[:140]}）", flush=True)
        state_keepalive = bool(_QR_FALLBACK_STATE.get("keepalive", keepalive))
        return await _run_qr_rounds(
            page, platform, [sel] if sel else [], emitter, manual, keepalive=state_keepalive
        )

    utils.find_login_qrcode = _find_with_fallback
    utils.find_qrcode_img_from_canvas = _find_canvas_with_fallback
    crawler_util.find_login_qrcode = _find_with_fallback
    crawler_util.find_qrcode_img_from_canvas = _find_canvas_with_fallback
    utils._engine_qr_fallback_installed = True
    _QR_FALLBACK_STATE["platform"] = platform
    _QR_FALLBACK_STATE["keepalive"] = bool(keepalive)
    return {"installed": True, "selectors": QR_FALLBACK_SELECTORS.get(platform, DEFAULT_QR_FALLBACK_SELECTORS)}


# --------------------------------------------------------------------------------------
# Patch 5：login 模式登录成功即停 / crawl 模式未登录快速失败
# --------------------------------------------------------------------------------------
def patch_login_only(crawler: Any, state: Optional[Dict[str, Any]] = None) -> None:
    """把实例的 search 替换为中止异常（login 模式不采集）。

    - state["login_attempted"] 为 True（确实进入过 login.begin 扫码）→ LoginOnlyDone
      （扫码成功由 instrument_login_class 的 check_login_state 包装发 confirmed）
    - 否则说明 crawler.start() 的 pong 已判定登录态有效（跳过 login.begin）→ AlreadyLoggedIn
    这样"已登录，无需扫码"与"扫码成功"两条路径可区分，UI 不会误报“二维码截图失败”。
    """
    login_attempted = bool(state and state.get("login_attempted"))

    async def _search_stop(*_args, **_kwargs):
        if login_attempted:
            raise LoginOnlyDone()
        raise AlreadyLoggedIn()

    crawler.search = _search_stop  # 实例属性，调用时不绑定 self


def _login_class(platform: str):
    module_name, class_name = LOGIN_CLASSES[platform]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


_NETWORK_TRANSIENT_TOKENS = (
    "connecttimeout",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "connecterror",
    "remoteprotocolerror",
    "networkerror",
    "retryerror",
    "timeout",
    "timed out",
    "connection reset",
    "connection closed",
    "connection refused",
    "econnreset",
    "econnrefused",
    "err_connection",
    "err_name_not_resolved",
    "network is unreachable",
    "proxy",
)
_NETWORK_TRANSIENT_STATE: Dict[str, Any] = {"ts": 0.0, "message": ""}
_NETWORK_HOOK_STATE: Dict[str, Any] = {"installed": False}


def _is_network_exception(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(token in text for token in _NETWORK_TRANSIENT_TOKENS)


def _record_network_exception(exc: BaseException) -> None:
    """记录最近一次网络异常；登录被拒时据此区分“网络抖动”和“真未登录”。"""
    _NETWORK_TRANSIENT_STATE["ts"] = time.monotonic()
    _NETWORK_TRANSIENT_STATE["message"] = f"{type(exc).__name__}: {str(exc)[:240]}"


def _recent_network_exception(window_seconds: float = 90.0) -> str:
    age = time.monotonic() - float(_NETWORK_TRANSIENT_STATE.get("ts") or 0.0)
    if _NETWORK_TRANSIENT_STATE.get("message") and age <= window_seconds:
        return str(_NETWORK_TRANSIENT_STATE["message"])
    return ""


def clear_network_transient() -> None:
    """清空网络异常记录（例如忽略页面首页加载失败后，单独判断 pong 结果）。"""
    _NETWORK_TRANSIENT_STATE["ts"] = 0.0
    _NETWORK_TRANSIENT_STATE["message"] = ""


def install_network_error_hook() -> Dict[str, Any]:
    """抓取所有平台 HTTP 请求的网络异常，供 crawl 模式统一识别。

    很多平台 pong() 内部会吞掉异常并返回 False，随后 core 调用 login.begin()；
    crawl 模式又把 login.begin() 拒绝成 NotLoggedIn，导致任何平台的网络抖动
    都被误报成“未登录”。这里记录最近网络异常，让 patch_login_blocked 能改抛
    NetworkTransient，由 sidecar 自动重试。
    """
    if _NETWORK_HOOK_STATE.get("installed"):
        return {"installed": True}
    try:
        import httpx
    except Exception:  # noqa: BLE001
        return {"installed": False}

    original_async_request = httpx.AsyncClient.request

    async def async_request_with_network_capture(self, *args, **kwargs):
        try:
            return await original_async_request(self, *args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if _is_network_exception(exc):
                _record_network_exception(exc)
            raise

    httpx.AsyncClient.request = async_request_with_network_capture

    try:
        original_sync_request = httpx.Client.request

        def sync_request_with_network_capture(self, *args, **kwargs):
            try:
                return original_sync_request(self, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if _is_network_exception(exc):
                    _record_network_exception(exc)
                raise

        httpx.Client.request = sync_request_with_network_capture
    except Exception:  # noqa: BLE001
        pass

    _NETWORK_HOOK_STATE["installed"] = True
    return {"installed": True}


def patch_login_blocked(mc: Dict[str, Any], platform: str) -> None:
    """crawl 模式：需要登录时直接抛 NotLoggedIn，绝不拉起扫码。

    若刚刚发生过网络异常（平台 pong 超时等），改抛 NetworkTransient，
    避免把网络抖动误报成“未登录”，让 sidecar 按网络错误自动重试。
    """

    cls = _login_class(platform)
    if getattr(cls, "_engine_login_blocked", False):
        return

    async def _blocked_begin(self, *_args, **_kwargs):
        network_error = _recent_network_exception()
        if network_error:
            raise NetworkTransient(f"平台网络异常，自动重试：{network_error}")
        raise NotLoggedIn()

    cls.begin = _blocked_begin
    cls._engine_login_blocked = True


# --------------------------------------------------------------------------------------
# Patch 5b：抖音登录按钮点击稳健化（避免被 login-full-panel 浮层拦截卡 30s）
# --------------------------------------------------------------------------------------
_DY_LOGIN_PANEL_SELECTORS = [
    "div#login-panel-new",
    "div[id^='login-full-panel']",
    "div[id*='login-full-panel']",
    "div#animate_qrcode_container",
    "div[class*='login-panel']",
]
_DY_LOGIN_BUTTON_SELECTORS = ["//p[text()='登录']", "text=登录"]


async def _any_visible(page: Any, selectors: List[str]) -> bool:
    if page is None:
        return False
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _wait_any_visible(page: Any, selectors: List[str], timeout_ms: int) -> bool:
    import asyncio

    deadline = time.monotonic() + max(0.1, timeout_ms / 1000.0)
    while time.monotonic() < deadline:
        if await _any_visible(page, selectors):
            return True
        await asyncio.sleep(0.3)
    return False


async def _robust_douyin_popup_login_dialog(self: Any, initial_wait_ms: int = 6000) -> None:
    """替代 DouYinLogin.popup_login_dialog：

    - 已有登录面板（login-panel-new / login-full-panel）→ 直接返回，不重复点击；
    - 未弹出 → 短超时(3s/2s)+force 点击“登录”，失败时 ESC 关遮罩后重试；
    - 仍失败不抛 30s TimeoutError，快速返回让后续二维码兜底/重载重试处理。
    """
    import asyncio

    page = getattr(self, "context_page", None)
    if page is None:
        return
    if await _wait_any_visible(page, _DY_LOGIN_PANEL_SELECTORS, initial_wait_ms):
        print("[engine] dy 登录面板已打开，跳过点击登录", flush=True)
        return
    for attempt in range(1, 4):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        await asyncio.sleep(0.3)
        clicked = False
        for selector in _DY_LOGIN_BUTTON_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() == 0:
                    continue
                try:
                    await locator.click(timeout=3000)
                    clicked = True
                except Exception:
                    try:
                        await locator.click(timeout=2000, force=True)
                        clicked = True
                    except Exception:
                        try:
                            await page.evaluate(
                                """(sel) => {
                                  const els = [...document.querySelectorAll('p,div,span,button')];
                                  const el = els.find(e => (e.innerText || '').trim() === '登录');
                                  if (el) { el.click(); return true; }
                                  return false;
                                }""",
                                selector,
                            )
                            clicked = True
                        except Exception:
                            pass
            except Exception:
                continue
            if clicked:
                break
        print(f"[engine] dy 尝试打开登录面板（第 {attempt}/3 次，clicked={clicked}）", flush=True)
        if await _wait_any_visible(page, _DY_LOGIN_PANEL_SELECTORS, 3000):
            return
        await asyncio.sleep(0.5)
    print("[engine] dy 登录面板仍未弹出（短超时+force+ESC 兜底后），继续尝试二维码兜底", flush=True)


def patch_douyin_login_dialog(mc: Dict[str, Any], emitter: Optional[EventEmitter] = None) -> None:
    """安装抖音登录面板稳健点击补丁（幂等）。"""
    cls = _login_class("dy")
    if getattr(cls, "_engine_dy_popup_patched", False):
        return
    cls.popup_login_dialog = _robust_douyin_popup_login_dialog  # type: ignore[assignment]
    cls._engine_dy_popup_patched = True
    print("[engine] 已安装抖音登录按钮稳健点击补丁（短超时+force+ESC，不再卡 30s）", flush=True)


def instrument_login_class(
    mc: Dict[str, Any],
    emitter: EventEmitter,
    platform: str,
    state: Optional[Dict[str, Any]] = None,
    manual: Optional[ManualGate] = None,
) -> None:
    """login 模式仪器化登录类：

    - 包装 begin()：先等待整页验证（dy 验证码中间页 / tieba 百度安全验证）由用户完成 → 再走原登录流程；
      并标记 state["login_attempted"]=True（区分“需要扫码”与“已登录跳过登录”）
    - 包装 check_login_state()：扫码登录成功时发 login.status confirmed，并标记 state["confirmed"]
    """
    cls = _login_class(platform)
    if getattr(cls, "_engine_instrumented", False):
        return
    original_check = cls.check_login_state
    original_begin = cls.begin

    async def _wrapped_check(self, *args, **kwargs):
        ok = await original_check(self, *args, **kwargs)
        if ok:
            if state is not None:
                state["confirmed"] = True
            emitter.status("confirmed", "登录成功")
        return ok

    async def _wrapped_begin(self, *args, **kwargs):
        import asyncio

        if state is not None:
            state["login_attempted"] = True
        page = getattr(self, "context_page", None)
        cleared = await wait_manual_before_begin(page, emitter, platform, manual)
        if not cleared:
            print(f"[engine] {platform} 人工验证未完成，继续尝试原登录流程（大概率失败）", flush=True)
        await asyncio.sleep(0.5)  # 等页面跳转稳定
        return await original_begin(self, *args, **kwargs)

    cls.check_login_state = _wrapped_check
    cls.begin = _wrapped_begin
    cls._engine_instrumented = True


# --------------------------------------------------------------------------------------
# Patch 6：日志 -> progress 事件
# --------------------------------------------------------------------------------------
_RE_KEYWORD = re.compile(r"Current (?:search )?keyword[:：]\s*(.+?)\s*$")
_RE_PAGE = re.compile(r"search .*?keyword[:：]\s*(.+?),\s*page[:：]\s*(\d+)")
_RE_DETAIL = re.compile(r"(get_note_detail_async_task|get_(?:video|aweme|note)_(?:detail|info)_task)")
_RE_COMMENTS = re.compile(r"(get_comments|batch_get_.*comments|get_note_all_comments|get_note_comments)")
_RE_STORE = re.compile(r"\[store\.[a-z]+\.update_")
_RE_FINISHED = re.compile(r"Crawler finished")


class ProgressLogHandler(logging.Handler):
    """把 MediaCrawler 的关键日志行转换为 progress 事件。"""

    def __init__(self, emitter: EventEmitter) -> None:
        super().__init__(level=logging.INFO)
        self.emitter = emitter
        self.stage = "boot"
        self.page = 0
        self.saved = 0

    def emit(self, record: logging.LogRecord) -> None:  # noqa: A003 - logging API
        try:
            message = record.getMessage()
        except Exception:
            return
        try:
            if "comment mode is not enabled" in message:
                return  # 关闭评论时的提示，不应当作 comments 阶段事件
            if _RE_STORE.search(message):
                self.saved += 1
                if self.saved == 1 or self.saved % 5 == 0:
                    self.emitter.progress(self.stage or "search", self.page, self.saved, f"已保存 {self.saved} 条")
                return
            match = _RE_KEYWORD.search(message)
            if match:
                self.stage = "search"
                self.page = 0
                self.emitter.progress("search", 0, self.saved, f"开始搜索：{match.group(1)}")
                return
            match = _RE_PAGE.search(message)
            if match:
                self.stage = "search"
                self.page = int(match.group(2))
                self.emitter.progress("search", self.page, self.saved, f"搜索 {match.group(1)} 第 {self.page} 页")
                return
            if _RE_DETAIL.search(message):
                self.stage = "detail"
                self.emitter.progress("detail", self.page, self.saved, "获取详情")
                return
            if _RE_COMMENTS.search(message):
                self.stage = "comments"
                self.emitter.progress("comments", self.page, self.saved, "获取评论")
                return
            if _RE_FINISHED.search(message):
                self.stage = "done"
                self.emitter.progress("done", self.page, self.saved, "采集流程结束")
        except Exception:
            return  # 日志 Handler 绝不能影响主流程


def install_progress_handler(mc: Dict[str, Any], emitter: EventEmitter) -> ProgressLogHandler:
    logger = mc["utils"].logger
    handler = ProgressLogHandler(emitter)
    logger.addHandler(handler)
    return handler


# --------------------------------------------------------------------------------------
# 清理 & 登录页观察（login.status: waiting_scan / need_manual）
# --------------------------------------------------------------------------------------
async def close_crawler(crawler: Any, timeout: float = 15.0) -> None:
    import asyncio

    context = getattr(crawler, "browser_context", None)
    if context is None:
        return
    try:
        await asyncio.wait_for(context.close(), timeout=timeout)
    except Exception:
        pass


async def _qr_visible_for_platform(page: Any, platform: str) -> bool:
    """当前页面是否仍可见二维码元素（用于区分“正常登录页”与“扫码后二次验证”）。"""
    try:
        return await _qr_already_visible(page, platform)
    except Exception:
        return False


async def should_trigger_sms(page: Any, platform: str, qr_count: int) -> bool:
    """短信二次验证触发条件（严格）：

    - 必须已经出过二维码（qr_count > 0，说明用户已进入扫码流程）
    - 页面出现短信验证专属文案
    - 且二维码元素已不可见（二维码消失/登录弹窗已切换）
    仅仅页面上有短信验证码输入框/「验证码登录」tab 一律不算。
    """
    if qr_count <= 0:
        return False
    info = await detect_verification(page)
    if not info or str(info.get("kind")) != "sms":
        return False
    if await _qr_visible_for_platform(page, platform):
        return False
    return True


async def watch_login_page(
    crawler: Any,
    emitter: EventEmitter,
    interval: float = 2.0,
    manual: Optional[ManualGate] = None,
    capture: Optional[Any] = None,
    platform: str = "",
) -> None:
    """轮询登录页：验证页 → need_manual（并把浏览器窗口置前）；出码后周期性 waiting_scan。

    - 短信二次验证必须同时满足“已出码 + 二维码消失 + 专属文案”（见 should_trigger_sms），
      普通登录页的手机号/验证码 tab 不触发 need_manual。
    - 不再凭页面文字猜测“已扫码”；只发周期性的“等待手机扫码/确认”，避免未扫码误报。
    """
    import asyncio

    last_tick = 0.0
    while True:
        await asyncio.sleep(interval)
        page = getattr(crawler, "context_page", None)
        if page is None:
            continue
        qr_count = int(getattr(capture, "count", 0) or 0) if capture is not None else 0
        info = await detect_verification(page)
        if info is not None and str(info.get("kind")) == "sms":
            if not await should_trigger_sms(page, platform, qr_count):
                info = None
        if manual is not None:
            await manual.report_async(info, page)
        elif info is not None:
            emitter.status("need_manual", _manual_message(str(info.get("kind") or "captcha")), kind=str(info.get("kind") or "captcha"))
            try:
                if hasattr(page, "bring_to_front"):
                    await page.bring_to_front()
            except Exception:
                pass
        if info is not None:
            continue
        if qr_count <= 0:
            continue
        now = time.monotonic()
        if now - last_tick >= 15.0:
            last_tick = now
            emitter.status("waiting_scan", "等待手机扫码/确认…（二维码已展示，请在手机 App 上操作）")


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
                    except Exception:
                        pass  # 页面加载失败也让 pong 决定（cookie 可能仍有效）
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
                except Exception:
                    pass

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

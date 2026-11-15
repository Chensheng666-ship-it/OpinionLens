# -*- coding: utf-8 -*-
"""二维码适配器：有效性校验 / 截获 / 兜底提取（纯函数可离线单测，不依赖浏览器）。

M1.6 从 ``adapters/login.py`` 纯搬迁（函数体一行未改）：
* 纯函数：``validate_qr_bytes`` / ``validate_qr_data_uri``（PIL + cv2 校验）
* ``QrCapture``：替换 ``tools.utils.show_qrcode``，落盘 PNG + 发 ``login.qrcode``
* 页面级兜底：``inspect_qr_candidates`` / ``_run_qr_rounds`` 等（只在真跑浏览器时用到）

依赖方向：``qr -> manual``（取验证页检测）与 ``qr -> base/session``；不回指 login。
运行期状态全部走 :func:`adapters.session.current_session`，无模块级全局。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

from adapters.base import QrLoadFailed, emit_patch_warning, require_patch
from adapters.manual import detect_verification
from adapters.session import current_session

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查（避免与 login 循环导入）
    from patches import EventEmitter
    from adapters.manual import ManualGate

log = logging.getLogger("engine.adapters.qr")

def _warn_once(kind: str, detail: str, platform: str = "") -> None:
    """可选增强缺失只上报一次（去重状态在 PatchSession，reset_session 后可再次发出）。"""
    session = current_session()
    if not session.should_warn_once(f"{platform}:{kind}"):
        return
    emit_patch_warning(platform or "-", kind, detail)


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

# --------------------------------------------------------------------------------------
# Patch 2：二维码截获 -> PNG + login.qrcode 事件
# --------------------------------------------------------------------------------------
def _decode_base64_image(qr_code: Any) -> Optional[bytes]:
    try:
        text = str(qr_code)
        if "," in text:
            text = text.split(",", 1)[1]
        return base64.b64decode(text)
    except Exception as exc:  # noqa: BLE001 - 输入级容错：坏 base64 不是补丁能力丢失
        log.debug("show_qrcode 数据无法 base64 解码：%s: %s", type(exc).__name__, exc)
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
    except Exception as exc:  # noqa: BLE001 - cv2 属可选增强，缺失时降级为几何/纹理校验
        _warn_once("cv2_decode", f"缺少 cv2/numpy，二维码解码校验降级：{type(exc).__name__}: {exc}")
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
    except Exception as exc:  # noqa: BLE001 - 解码失败回退到几何/纹理判定
        log.debug("cv2 二维码解码异常（回退启发式校验）：%s: %s", type(exc).__name__, exc)
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
    except Exception as exc:  # noqa: BLE001 - 坏图（非图片字节）按无效处理，调用方会重试
        log.debug("二维码图片无法解码：%s: %s", type(exc).__name__, exc)
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
    except Exception as exc:  # noqa: BLE001 - 像素统计失败按无效图处理
        log.debug("二维码像素统计失败：%s: %s", type(exc).__name__, exc)
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
        except OSError as exc:  # noqa: BLE001 - 可选增强：落盘失败不影响扫码（data URI 仍会推给 UI）
            emit_patch_warning(self.platform, "qrcode_file_write", f"二维码 PNG 落盘失败：{type(exc).__name__}: {exc}")
        data_uri = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
        self.emitter.qrcode(image=data_uri, file=str(png_path), nbytes=len(raw))
        if not self._waiting_sent:
            self._waiting_sent = True
            self.emitter.status("waiting_qr", "二维码已就绪，请用手机 App 扫码")


def install_qr_capture(mc: Dict[str, Any], emitter: EventEmitter, data_dir: Path, platform: str) -> QrCapture:
    """替换 show_qrcode：登录/二维码属必需能力，上游钩子缺失即 EnginePatchError。"""
    capture = QrCapture(emitter, data_dir, platform)
    try:
        mc["utils"].show_qrcode = capture
        mc["crawler_util"].show_qrcode = capture
    except Exception as exc:  # noqa: BLE001 - 二维码截获是必需能力
        require_patch(platform, "install_qr_capture", exc, emitter=emitter)
    return capture



# --------------------------------------------------------------------------------------
# Patch 4b：二维码查找兜底（原选择器取不到时：先人工验证，再按平台 fallback 选择器）
# 运行期状态（keepalive / platform）挂在 PatchSession 上，不再用模块级字典。
# --------------------------------------------------------------------------------------


async def _page_user_agent(page: Any) -> str:
    try:
        return str(await page.evaluate("() => navigator.userAgent") or "")
    except Exception as exc:  # noqa: BLE001 - 取不到 UA 时退回默认 UA
        log.debug("读取页面 UA 失败：%s: %s", type(exc).__name__, exc)
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
        except Exception as exc:  # noqa: BLE001 - 无法评估时交给后续截图/PIL 校验
            log.debug("等待 <img> 加载时评估失败：%s: %s", type(exc).__name__, exc)
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
    except Exception as exc:  # noqa: BLE001 - 缺 Referer 只影响部分 CDN 防盗链
        log.debug("设置二维码下载 Referer 失败：%s: %s", type(exc).__name__, exc)
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
    except Exception as exc:  # noqa: BLE001 - 取不到 src 时走元素截图兜底
        log.debug("读取二维码元素 src 失败：%s: %s", type(exc).__name__, exc)
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
    except Exception as exc:  # noqa: BLE001 - 置前/滚动都是截图前的尽力而为动作
        log.debug("二维码截图前置前失败：%s: %s", type(exc).__name__, exc)
    try:
        if hasattr(element, "scroll_into_view_if_needed"):
            await element.scroll_into_view_if_needed(timeout=2000)
    except Exception as exc:  # noqa: BLE001 - 滚动失败仍尝试直接截图
        log.debug("二维码元素滚动到可视区失败：%s: %s", type(exc).__name__, exc)
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
        except Exception as exc:  # noqa: BLE001 - 选择器不可用：试下一个
            log.debug("二维码可见性检查 selecter 失败（%s）：%s: %s", selector[:60], type(exc).__name__, exc)
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
        except Exception as exc:  # noqa: BLE001 - 该入口不可点：试下一个选择器
            log.debug("%s 点击二维码入口失败（%s）：%s: %s", platform, selector[:60], type(exc).__name__, exc)
            continue
    return False


async def _find_qr_element(page: Any, selector: str, wait_ms: int = 0) -> Any:
    if page is None or not selector:
        return None
    try:
        element = await page.query_selector(selector)
    except Exception as exc:  # noqa: BLE001 - 选择器语法/页面状态问题：按未找到处理
        log.debug("query_selector 失败（%s）：%s: %s", selector[:60], type(exc).__name__, exc)
        element = None
    if element is not None:
        return element
    if wait_ms > 0:
        try:
            await page.wait_for_selector(selector, timeout=wait_ms)
        except Exception as exc:  # noqa: BLE001 - 等待超时=候选不存在，交给下一个选择器
            log.debug("等待二维码选择器超时（%s）：%s", selector[:60], exc)
            return None
        try:
            return await page.query_selector(selector)
        except Exception as exc:  # noqa: BLE001 - 同上
            log.debug("等待后再次 query_selector 失败（%s）：%s", selector[:60], exc)
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
    except Exception as exc:  # noqa: BLE001 - boundingBox 结构异常：交给后续 PIL 校验
        log.debug("boundingBox 解析失败：%s: %s", type(exc).__name__, exc)
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
        except Exception as exc:  # noqa: BLE001 - 可见性查询失败：继续尝试截图校验
            log.debug("二维码候选可见性查询失败（%s）：%s: %s", selector[:60], type(exc).__name__, exc)
        box: Optional[Dict[str, Any]] = None
        try:
            if hasattr(element, "bounding_box"):
                box = await element.bounding_box()
        except Exception as exc:  # noqa: BLE001 - 拿不到几何信息时交给 PIL 校验
            log.debug("二维码候选 bounding_box 失败（%s）：%s: %s", selector[:60], type(exc).__name__, exc)
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
    except Exception as exc:  # noqa: BLE001 - 取不到 URL 时只用 reload
        log.debug("读取当前登录页 URL 失败：%s: %s", type(exc).__name__, exc)
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
            page._engine_wb_cdp_session = cdp
            cdp_ok = True
    except Exception as exc:  # noqa: BLE001
        print(f"[engine] 微博 CDP 桌面 UA 覆盖失败（回退 extra headers）：{type(exc).__name__}: {str(exc)[:120]}", flush=True)
    if not cdp_ok:
        try:
            if hasattr(page, "set_extra_http_headers"):
                await page.set_extra_http_headers({"User-Agent": WEIBO_QR_USER_AGENT})
        except Exception as exc:  # noqa: BLE001 - CDP 已失败时的二级兜底，失败也不阻断
            log.debug("设置微博桌面 UA 请求头失败：%s: %s", type(exc).__name__, exc)
    try:
        await page.goto(WEIBO_QR_LOGIN_URL, wait_until="load")  # 走 Page.goto 重试包装
        page._engine_wb_desktop_qr = True
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
        page._engine_tieba_passport_qr = True
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
                except Exception as exc:  # noqa: BLE001 - 重载后重设 UA 失败：仍继续取码
                    log.debug("重载后重设微博桌面 UA 失败：%s: %s", type(exc).__name__, exc)
            await _prepare_qr_view(page, platform)
        elif platform == "tieba":
            # 重载后重新打开 passport 登录页，拿一张新二维码
            page._engine_tieba_passport_qr = False
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


async def _qr_visible_for_platform(page: Any, platform: str) -> bool:
    """当前页面是否仍可见二维码元素（用于区分“正常登录页”与“扫码后二次验证”）。"""
    try:
        return await _qr_already_visible(page, platform)
    except Exception as exc:  # noqa: BLE001 - 页面不可查询时按"看不到二维码"保守处理
        log.debug("查询二维码可见性失败：%s: %s", type(exc).__name__, exc)
        return False


__all__ = [
    # 常量
    "QR_MAX_ROUNDS",
    "QR_MIN_SIDE",
    "QR_MIN_ASPECT_RATIO",
    "QR_MAX_ASPECT_RATIO",
    "QR_MAX_SIDE",
    "QR_MIN_AREA",
    "QR_MAX_AREA",
    "QR_MIN_STD",
    "QR_MIN_UNIQUE",
    "QR_MIN_DARK_RATIO",
    "QR_IMAGE_LOAD_WAIT_S",
    "QR_HTTP_RETRIES",
    "QR_FAIL_MESSAGE",
    "QR_FAIL_MESSAGE_TIEBA",
    "QR_KEEPALIVE_PLACEHOLDER",
    "QR_TIEBA_STRICT_DECODE",
    "QR_SHORT_WAIT_MS",
    "DEFAULT_QR_USER_AGENT",
    "WEIBO_QR_USER_AGENT",
    "WEIBO_QR_LOGIN_URL",
    "TIEBA_QR_LOGIN_URL",
    "QR_FALLBACK_SELECTORS",
    "DEFAULT_QR_FALLBACK_SELECTORS",
    "QR_VIEW_ENTRY_SELECTORS",
    # 纯函数（可离线单测）
    "validate_qr_bytes",
    "validate_qr_data_uri",
    # 二维码截获
    "QrCapture",
    "install_qr_capture",
    # 页面级兜底提取
    "inspect_qr_candidates",
    "extract_qr_fallback",
    "_qr_visible_for_platform",
    "_warn_once",
    "_decode_base64_image",
    "_cv2_decode_qr",
    "_page_user_agent",
    "_wait_image_loaded",
    "_http_download_qr",
    "_qr_candidate_from_element",
    "_qr_already_visible",
    "_prepare_qr_view",
    "_find_qr_element",
    "_element_geometry",
    "_reload_login_page",
    "_switch_weibo_to_desktop_qr_page",
    "_switch_tieba_to_passport_qr_page",
    "_acquire_qr_once",
    "_run_qr_rounds",
    "_short_wait_original_find",
]

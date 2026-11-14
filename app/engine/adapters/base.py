# -*- coding: utf-8 -*-
"""Engine 适配器基础设施：上游契约探测、显式错误、补丁事件与计数。

对应 ``docs/代码审查与重构路线.md`` P0-1：把「靠 monkeypatch 打空枪也不报错」
换成「上游不兼容立刻明确失败」。

三条约定
--------
1. **契约可探测**：每个适配器声明 ``UPSTREAM_CONTRACT``（依赖的上游文件 + 符号），
   :func:`detect_upstream_contract` 真实 ``importlib`` + ``getattr`` 链探测；
   缺失即 :class:`EngineIncompatible`（``code = "ENGINE_INCOMPATIBLE"``），
   由 engine 入口 ``entry.main`` 在 ``run_mode`` 之前转成事件 + 非零退出。
2. **必需补丁失败要炸**：登录 / 二维码 / 数据解析等必需能力失败 -> :func:`require_patch`
   -> :class:`EnginePatchError`（出口层统一转 ``engine.finished ok:false`` + message）。
3. **可选增强失败要可见**：:func:`emit_patch_warning` 发 ``progress``（stage=boot，
   message 以 ``[warning]`` 开头）+ 计数，绝不允许「吞掉后什么都不做」。

本模块只依赖标准库，不 import ``patches``（避免循环导入），可离线自测。
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple

log = logging.getLogger("engine.adapters")

PLATFORMS: Tuple[str, ...] = ("xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu")

# platform -> (login 模块, Login 类名)
LOGIN_CLASSES: Dict[str, Tuple[str, str]] = {
    "xhs": ("media_platform.xhs.login", "XiaoHongShuLogin"),
    "dy": ("media_platform.douyin.login", "DouYinLogin"),
    "ks": ("media_platform.kuaishou.login", "KuaishouLogin"),
    "bili": ("media_platform.bilibili.login", "BilibiliLogin"),
    "wb": ("media_platform.weibo.login", "WeiboLogin"),
    "tieba": ("media_platform.tieba.login", "BaiduTieBaLogin"),
    "zhihu": ("media_platform.zhihu.login", "ZhiHuLogin"),
}

INSTALL_HINT = (
    "上游 MediaCrawler 版本不匹配，请按 scripts/setup_mediacrawler.ps1 的固定 commit 重装"
)


# --------------------------------------------------------------------------------------
# 异常：显式失败（不再静默）
# --------------------------------------------------------------------------------------
class EngineIncompatible(RuntimeError):
    """上游 MediaCrawler 与适配器契约不兼容（符号缺失 / 模块导入失败）。"""

    code = "ENGINE_INCOMPATIBLE"

    def __init__(self, missing: Sequence["MissingSymbol"]) -> None:
        self.missing: List["MissingSymbol"] = list(missing)
        items = [item.describe() for item in self.missing]
        self.details = "；".join(items)
        shown = "；".join(items[:5])
        if len(items) > 5:
            shown += f"；……等 {len(items)} 项（完整清单见引擎日志）"
        if not shown:
            shown = "（未提供缺失明细）"
        self.summary = (
            f"{self.code}: 上游 MediaCrawler 不兼容，缺失 {len(self.missing)} 个必需符号 —— "
            f"{shown}。{INSTALL_HINT}"
        )
        super().__init__(self.summary)
        log.error("%s 缺失明细（%d 项）：%s", self.code, len(items), self.details)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.summary,
            "missing": [item.as_dict() for item in self.missing],
        }


class EnginePatchError(RuntimeError):
    """必需补丁安装/执行失败（登录、二维码、数据解析等能力不可用）。"""

    code = "ENGINE_PATCH_ERROR"

    def __init__(self, platform: str, patch_name: str, detail: str = "") -> None:
        self.platform = platform
        self.patch_name = patch_name
        self.detail = detail
        super().__init__(f"{self.code}: 补丁 {patch_name}（platform={platform}）失败：{detail or '未知原因'}")


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
# 上游契约
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class UpstreamSymbol:
    """适配器依赖的一个上游符号（文件路径 + 相对模块的属性链）。"""

    owner: str
    path: str
    symbol: str
    required: bool = True
    platforms: Tuple[str, ...] = ("all",)
    modes: Tuple[str, ...] = ("all",)

    @property
    def module(self) -> str:
        """``media_platform/xhs/client.py`` -> ``media_platform.xhs.client``。"""
        path = self.path
        if path.endswith(".py"):
            path = path[:-3]
        return path.strip("/").replace("/", ".")

    def describe(self) -> str:
        return f"{self.path}:{self.symbol}"


@dataclass(frozen=True)
class MissingSymbol:
    """探测到的缺失符号（用于 EngineIncompatible 的 message）。"""

    owner: str
    path: str
    symbol: str
    reason: str

    def describe(self) -> str:
        return f"{self.path}:{self.symbol}（{self.reason}）"

    def as_dict(self) -> Dict[str, str]:
        return {
            "owner": self.owner,
            "path": self.path,
            "symbol": self.symbol,
            "reason": self.reason,
        }


_CONTRACTS: Dict[str, Dict[str, Any]] = {}


def register_contract(
    owner: str, description: str, symbols: Sequence[UpstreamSymbol]
) -> Sequence[UpstreamSymbol]:
    """适配器导入时登记自己的上游契约（幂等，重复登记去重）。"""
    item = _CONTRACTS.setdefault(owner, {"description": "", "symbols": []})
    if description:
        item["description"] = description
    seen = {(symbol.path, symbol.symbol) for symbol in item["symbols"]}
    for symbol in symbols:
        key = (symbol.path, symbol.symbol)
        if key in seen:
            continue
        item["symbols"].append(symbol)
        seen.add(key)
    return symbols


def _matches(values: Sequence[str], value: Optional[str]) -> bool:
    if not value:
        return True
    return "all" in values or value in values


def iter_contracts(
    platform: Optional[str] = None,
    mode: Optional[str] = None,
    owner: Optional[str] = None,
    required_only: bool = False,
) -> List[UpstreamSymbol]:
    """按平台/模式筛选契约项（``all`` 表示适用于全部）。"""
    result: List[UpstreamSymbol] = []
    for key, item in _CONTRACTS.items():
        if owner and key != owner:
            continue
        for symbol in item["symbols"]:
            if required_only and not symbol.required:
                continue
            if not _matches(symbol.platforms, platform):
                continue
            if not _matches(symbol.modes, mode):
                continue
            result.append(symbol)
    return result


def adapter_registry() -> Dict[str, Dict[str, Any]]:
    """适配器注册表（自测断言每个适配器都有必需契约项）。"""
    return {
        key: {
            "description": item["description"],
            "symbols": list(item["symbols"]),
            "required": [symbol for symbol in item["symbols"] if symbol.required],
        }
        for key, item in _CONTRACTS.items()
    }


# --------------------------------------------------------------------------------------
# 恒等函数（产品默认：真实数据不脱敏）
# --------------------------------------------------------------------------------------
def identity_user_id(user_id: Any) -> str:
    if user_id is None:
        return ""
    return str(user_id)


def identity_nickname(name: Any) -> str:
    if name is None:
        return ""
    return str(name)


def _login_class(platform: str) -> Any:
    """按平台取上游 Login 类（导入失败直接抛，由调用方转 EnginePatchError）。"""
    module_name, class_name = LOGIN_CLASSES[platform]
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


# --------------------------------------------------------------------------------------
# 补丁事件：warning 计数 / error 事件 / 必需补丁失败
# --------------------------------------------------------------------------------------
_PATCH_WARNING_COUNTS: Dict[str, int] = {}
_PATCH_WARNING_TOTAL = 0
_PATCH_ERROR_TOTAL = 0
_EMITTER: Optional[Any] = None
_EMITTER_LOCK = threading.Lock()


def set_warning_emitter(emitter: Optional[Any]) -> None:
    """登记补丁事件出口（``EventEmitter`` 构造时自动登记最新实例）。"""
    global _EMITTER
    with _EMITTER_LOCK:
        _EMITTER = emitter


def get_warning_emitter() -> Optional[Any]:
    with _EMITTER_LOCK:
        return _EMITTER


def reset_patch_warning_counts() -> None:
    """清零计数（自测用）。"""
    global _PATCH_WARNING_TOTAL, _PATCH_ERROR_TOTAL
    with _EMITTER_LOCK:
        _PATCH_WARNING_COUNTS.clear()
        _PATCH_WARNING_TOTAL = 0
        _PATCH_ERROR_TOTAL = 0


def patch_warning_counts() -> Dict[str, Any]:
    """当前补丁 warning 统计：``{"total": n, "items": {"platform:patch": n}}``。"""
    with _EMITTER_LOCK:
        return {"total": _PATCH_WARNING_TOTAL, "items": dict(_PATCH_WARNING_COUNTS)}


def patch_error_counts() -> int:
    with _EMITTER_LOCK:
        return _PATCH_ERROR_TOTAL


def _emit_patch_event(
    level: str,
    platform: str,
    patch_name: str,
    detail: str,
    emitter: Optional[Any] = None,
    stage: str = "boot",
) -> Dict[str, Any]:
    global _PATCH_WARNING_TOTAL, _PATCH_ERROR_TOTAL

    key = f"{platform}:{patch_name}"
    with _EMITTER_LOCK:
        if level == "warning":
            _PATCH_WARNING_TOTAL += 1
            count = _PATCH_WARNING_COUNTS[key] = _PATCH_WARNING_COUNTS.get(key, 0) + 1
        else:
            _PATCH_ERROR_TOTAL += 1
            count = _PATCH_ERROR_TOTAL

    text = f"[{level}] 补丁 {patch_name}（platform={platform}）：{detail}"
    if level == "warning":
        log.warning("%s", text)
    else:
        log.error("%s", text)

    target = emitter if emitter is not None else get_warning_emitter()
    event: Optional[dict] = None
    if target is not None:
        try:
            # 复用契约事件 progress（字段不增不减，level 写在 message 前缀里）
            event = target.progress(stage, 0, 0, text)
        except Exception:  # noqa: BLE001 - 事件出口异常不能盖住真正的补丁错误
            log.exception("发送补丁 %s 事件失败", level)
    return {
        "level": level,
        "platform": platform,
        "patch": patch_name,
        "detail": detail,
        "count": count,
        "event": event,
    }


def emit_patch_warning(
    platform: str, patch_name: str, detail: str, emitter: Optional[Any] = None
) -> Dict[str, Any]:
    """可选增强失败：warning 事件 + 计数（返回含 level/count/event 的报告，便于自测断言）。"""
    return _emit_patch_event("warning", platform, patch_name, detail, emitter=emitter)


def emit_patch_error(
    platform: str, patch_name: str, detail: str, emitter: Optional[Any] = None
) -> Dict[str, Any]:
    """必需能力失败：error 事件（出口层随后发 engine.finished ok:false）。"""
    return _emit_patch_event("error", platform, patch_name, detail, emitter=emitter)


def require_patch(
    platform: str,
    patch_name: str,
    exc: Any,
    emitter: Optional[Any] = None,
) -> NoReturn:
    """必需补丁失败：发 error 事件 + 抛 EnginePatchError（由出口层统一收口）。"""
    detail = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    cause = exc if isinstance(exc, BaseException) else None
    emit_patch_error(platform, patch_name, detail, emitter=emitter)
    raise EnginePatchError(platform, patch_name, detail) from cause


# --------------------------------------------------------------------------------------
# 契约探测
# --------------------------------------------------------------------------------------
def install_import_path(mc_root: Any) -> Path:
    """把 MediaCrawler 根目录加入 sys.path，并把 cwd 固定到 MediaCrawler 根。

    上游部分模块在 import 期就按 **cwd** 读相对路径（例如 ``media_platform/douyin/core.py``
    读 ``libs/douyin.js``），所以探测前必须切好 cwd，否则会误报「模块导入失败」。
    非 MediaCrawler 目录（fake 上游自测）不切换 cwd。
    """
    root = Path(mc_root).expanduser().resolve()
    sys.dont_write_bytecode = True  # 不往 MediaCrawler/ 写 __pycache__（只读约束）
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    if (root / "main.py").is_file():
        try:
            os.chdir(str(root))
        except OSError as exc:
            log.debug("切换 cwd 到 MediaCrawler 根失败：%s: %s", type(exc).__name__, exc)
    return root


def _prepare_user_hash_patch() -> None:
    """探测前先做「不脱敏」前置 patch，保证导入 store 时绑定的就是恒等函数。"""
    try:
        importlib.import_module("tools.utils")
        user_hash_module = importlib.import_module("tools.user_hash")
        importlib.import_module("tools.crawler_util")
    except Exception as exc:  # noqa: BLE001 - 逐符号探测会把缺失报得更具体
        log.debug("前置导入 tools 失败（由符号探测报告明细）：%s: %s", type(exc).__name__, exc)
        return
    try:
        user_hash_module.anonymize_user_id = identity_user_id
        user_hash_module.mask_nickname = identity_nickname
    except Exception as exc:  # noqa: BLE001 - 不是本次探测目标，留给 patch_user_hash 报错
        log.debug("前置 user_hash patch 失败：%s: %s", type(exc).__name__, exc)


def _resolve_symbol(module: Any, symbol: str) -> Tuple[Any, Optional[str]]:
    """沿 ``A.b.c`` 取属性；返回 (值, 失败原因)。"""
    node = module
    for part in symbol.split("."):
        try:
            node = getattr(node, part)
        except AttributeError:
            return None, f"属性缺失：{part}"
        except Exception as exc:  # noqa: BLE001 - 属性访问抛别的异常也算不可用
            return None, f"{type(exc).__name__}: {exc}"
    return node, None


def detect_upstream_contract(
    mc_root: Any,
    platform: Optional[str] = None,
    mode: Optional[str] = None,
    symbols: Optional[Sequence[UpstreamSymbol]] = None,
    import_path: bool = True,
) -> List[MissingSymbol]:
    """真实 import + getattr 链探测适配器依赖的上游符号，返回缺失项（不抛裸异常）。

    - ``symbols`` 缺省时用注册表里按 ``platform`` / ``mode`` 过滤后的契约；
    - 只把 ``required=True`` 的缺失项计入返回值（可选增强缺失由补丁自己发 warning）；
    - 模块导入失败按「该模块下所有必需符号缺失」处理，reason 带异常类型。
    """
    if import_path:
        install_import_path(mc_root)
    targets = list(symbols) if symbols is not None else iter_contracts(platform=platform, mode=mode)
    if not targets:
        return []
    _prepare_user_hash_patch()

    modules: Dict[str, Tuple[Any, Optional[BaseException]]] = {}
    missing: List[MissingSymbol] = []
    for symbol in targets:
        name = symbol.module
        if name not in modules:
            try:
                modules[name] = (importlib.import_module(name), None)
            except BaseException as exc:  # noqa: BLE001 - 导入失败=上游不可用
                modules[name] = (None, exc)
        module, error = modules[name]
        if error is not None:
            if symbol.required:
                missing.append(
                    MissingSymbol(
                        symbol.owner,
                        symbol.path,
                        symbol.symbol,
                        f"模块导入失败 {type(error).__name__}: {str(error)[:160]}",
                    )
                )
            continue
        _value, reason = _resolve_symbol(module, symbol.symbol)
        if reason is not None and symbol.required:
            missing.append(MissingSymbol(symbol.owner, symbol.path, symbol.symbol, reason))
    return missing

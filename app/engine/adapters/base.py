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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, NoReturn, Optional, Sequence, Tuple

from adapters.session import (
    PatchSession,
    current_session,
    remember_originals,
    reset_session,
    restore_originals,
    session_lock,
)

log = logging.getLogger("engine.adapters")

PLATFORMS: Tuple[str, ...] = ("xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu")

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
        # 构造函数只组装信息，不写日志（副作用会让「构造异常」变成隐式 IO）；
        # 由抛出点（entry.main / 调用方）记录一次完整明细。
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

    def log_once(self, logger: Optional[logging.Logger] = None) -> None:
        """在抛出点记录一次完整缺失明细（构造阶段保持无副作用）。"""
        (logger or log).error("%s 缺失明细（%d 项）：%s", self.code, len(self.missing), self.details)

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
_CONTRACT_VERSION = 0  # 每次登记新契约 +1：让探测缓存自动失效


def register_contract(
    owner: str, description: str, symbols: Sequence[UpstreamSymbol]
) -> Sequence[UpstreamSymbol]:
    """适配器导入时登记自己的上游契约（幂等，重复登记去重）。"""
    global _CONTRACT_VERSION
    item = _CONTRACTS.setdefault(owner, {"description": "", "symbols": []})
    before = len(item["symbols"])
    if description:
        item["description"] = description
    seen = {(symbol.path, symbol.symbol) for symbol in item["symbols"]}
    for symbol in symbols:
        key = (symbol.path, symbol.symbol)
        if key in seen:
            continue
        item["symbols"].append(symbol)
        seen.add(key)
    if len(item["symbols"]) != before:
        _CONTRACT_VERSION += 1
        clear_contract_cache()
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


# --------------------------------------------------------------------------------------
# 补丁事件：warning 计数 / error 事件 / 必需补丁失败（状态全部挂在 PatchSession 上）
# --------------------------------------------------------------------------------------
def set_warning_emitter(emitter: Optional[Any]) -> None:
    """绑定补丁事件出口（``EventEmitter`` 构造时自动绑定，等价于开始一次新会话）。"""
    current_session().emitter = emitter


def get_warning_emitter() -> Optional[Any]:
    return current_session().emitter


def reset_patch_warning_counts() -> None:
    """兼容壳：等价于 :func:`adapters.session.reset_session`（清计数 + warned_once + installed）。"""
    reset_session()


def patch_warning_counts() -> Dict[str, Any]:
    """当前会话的补丁 warning 统计：``{"total": n, "items": {"platform:patch": n}}``。"""
    session = current_session()
    with session_lock():
        return {"total": session.warning_total, "items": dict(session.warning_counts)}


def patch_error_counts() -> int:
    with session_lock():
        return current_session().error_total


def _emit_patch_event(
    level: str,
    platform: str,
    patch_name: str,
    detail: str,
    emitter: Optional[Any] = None,
    stage: str = "boot",
) -> Dict[str, Any]:
    session = current_session()
    key = f"{platform}:{patch_name}"
    with session_lock():
        if level == "warning":
            session.warning_total += 1
            count = session.warning_counts[key] = session.warning_counts.get(key, 0) + 1
        else:
            session.error_total += 1
            count = session.error_total

    text = f"[{level}] 补丁 {patch_name}（platform={platform}）：{detail}"
    if level == "warning":
        log.warning("%s", text)
    else:
        log.error("%s", text)

    target = emitter if emitter is not None else session.emitter
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
        install_heavy_import_stubs()
    return root


# --------------------------------------------------------------------------------------
# 冷启动加速：上游 store 层在 import 期无条件导入两个重模块，而本应用永远用不到
#   * tools.words            -> matplotlib.pyplot + wordcloud + jieba（约 1.0s）；apply_config 固定
#                               ENABLE_GET_WORDCLOUD=False，AsyncFileWriter 不会实例化它
#   * database.mongodb_store_base -> motor/pymongo（约 0.5s）；SAVE_DATA_OPTION 固定 sqlite
# 在 import 之前放入 sys.modules 占位：只提供被 import 的类名，真被实例化时明确抛错，
# 不会静默失效。设 ENGINE_STUB_HEAVY_IMPORTS=0 可关闭（排查上游问题时用）。
# --------------------------------------------------------------------------------------
HEAVY_IMPORT_STUBS: Dict[str, Tuple[str, ...]] = {
    "tools.words": ("AsyncWordCloudGenerator",),
    "database.mongodb_store_base": ("MongoDBStoreBase", "MongoDBConnection"),
}


def _make_disabled_class(module_name: str, class_name: str) -> type:
    def _init(self: Any, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError(
            f"{module_name}.{class_name} 已被舆见引擎禁用（词云 / MongoDB 不在本应用支持范围）；"
            "如确需启用请设置 ENGINE_STUB_HEAVY_IMPORTS=0"
        )

    return type(class_name, (), {"__init__": _init, "__module__": module_name})


def install_heavy_import_stubs() -> List[str]:
    """在上游模块 import 前放入占位模块；已真实导入的不覆盖。返回实际放入的模块名。"""
    if str(os.environ.get("ENGINE_STUB_HEAVY_IMPORTS", "1")).strip().lower() in ("0", "false", "no", "off"):
        return []
    import types

    installed: List[str] = []
    for module_name, class_names in HEAVY_IMPORT_STUBS.items():
        if module_name in sys.modules:
            continue
        stub = types.ModuleType(module_name)
        stub.__doc__ = "舆见引擎占位模块（冷启动加速，见 adapters/base.py HEAVY_IMPORT_STUBS）"
        stub.__engine_stub__ = True  # type: ignore[attr-defined]  # 原因：给占位模块挂识别标记（ModuleType 无该属性声明）
        for class_name in class_names:
            setattr(stub, class_name, _make_disabled_class(module_name, class_name))
        sys.modules[module_name] = stub
        installed.append(module_name)
    return installed


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
        # 运行时替换上游 tools.user_hash 的函数（ModuleType 无静态属性声明）
        user_hash_module.anonymize_user_id = identity_user_id  # type: ignore[attr-defined]  # 原因：运行时 patch 上游 tools.user_hash 的函数（ModuleType 无静态属性声明）
        user_hash_module.mask_nickname = identity_nickname  # type: ignore[attr-defined]  # 原因：运行时 patch 上游 tools.user_hash 的函数（ModuleType 无静态属性声明）
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


# --------------------------------------------------------------------------------------
# 探测缓存（M2 同进程多次跑：第二次探测接近 0 成本）
# --------------------------------------------------------------------------------------
# 缓存键 = (解析后的 mc_root, platform, mode, 契约版本)；显式传 symbols 的探测不缓存
# （fake 上游自测会临时替换 sys.modules，不能污染真实结果）。
_CONTRACT_CACHE: Dict[Tuple[str, Optional[str], Optional[str], int], List[MissingSymbol]] = {}
_CACHE_HITS = 0
_CACHE_MISSES = 0


def clear_contract_cache() -> None:
    """清空探测缓存（改契约 / 自测用）。"""
    global _CACHE_HITS, _CACHE_MISSES
    with session_lock():
        _CONTRACT_CACHE.clear()
        _CACHE_HITS = 0
        _CACHE_MISSES = 0


def contract_cache_info() -> Dict[str, int]:
    """探测缓存统计（自测断言命中；size=当前缓存条目数）。"""
    with session_lock():
        return {"hits": _CACHE_HITS, "misses": _CACHE_MISSES, "size": len(_CONTRACT_CACHE)}


def detect_upstream_contract(
    mc_root: Any,
    platform: Optional[str] = None,
    mode: Optional[str] = None,
    symbols: Optional[Sequence[UpstreamSymbol]] = None,
    import_path: bool = True,
) -> List[MissingSymbol]:
    """真实 import + getattr 链探测适配器依赖的上游符号，返回缺失项（不抛裸异常）。

    - ``symbols`` 缺省时用注册表里按 ``platform`` / ``mode`` 过滤后的契约，并**命中缓存**；
    - 显式传 ``symbols``（fake 上游自测）时不缓存，避免污染真实结果；
    - 只把 ``required=True`` 的缺失项计入返回值（可选增强缺失由补丁自己发 warning）；
    - 模块导入失败按「该模块下所有必需符号缺失」处理，reason 带异常类型。
    """
    root = install_import_path(mc_root) if import_path else Path(mc_root).expanduser().resolve()
    explicit = symbols is not None
    key: Tuple[str, Optional[str], Optional[str], int] = (str(root), platform, mode, _CONTRACT_VERSION)
    global _CACHE_HITS, _CACHE_MISSES
    if not explicit:
        with session_lock():
            cached = _CONTRACT_CACHE.get(key)
            if cached is not None:
                _CACHE_HITS += 1
                return list(cached)

    targets = list(symbols) if symbols is not None else iter_contracts(platform=platform, mode=mode)
    if not targets:
        missing: List[MissingSymbol] = []
    else:
        _prepare_user_hash_patch()
        modules: Dict[str, Tuple[Any, Optional[BaseException]]] = {}
        missing = []
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

    if not explicit:
        with session_lock():
            _CACHE_MISSES += 1
            _CONTRACT_CACHE[key] = list(missing)
    return missing

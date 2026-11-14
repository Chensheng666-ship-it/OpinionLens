# -*- coding: utf-8 -*-
"""单次运行的补丁会话（``PatchSession``）：替代模块级全局，供 M2 同进程多次跑。

为什么需要它（M1 review 结论）
------------------------------
* 模块级 ``_WARNED_ONCE`` 这类全局集合让「只提醒一次」的 warning 跨运行失效，
  自测之间也会互相污染；
* 补丁闭包捕获 emitter / manual 后，同进程下一次运行仍在用旧对象（事件丢失/串台）；
* 「已安装」幂等标记若只放模块全局，新一次运行无法重新包装（旧 emitter 继续被用）；
  完全不设标记，重复调用又会把包装器套成多层 ——直接导致事件翻倍。

设计
----
* :class:`PatchSession` 持有 ``emitter / state / manual / warned_once / installed /
  warning_counts``；
* :func:`current_session` 取当前会话（懒创建），补丁只读写它；
* :func:`start_session` 由 ``EventEmitter`` 构造时调用（一次运行一个会话）；
* :func:`reset_session` 显式清空（同进程重跑 / 自测），旧名 ``reset_patch_warning_counts`` 保留为壳；
* :func:`remember_originals` / :func:`restore_originals`：按 owner 记住上游原始实现，
  重新包装前回到基线，避免「包装器套包装器」。

本模块只依赖标准库，不 import ``base`` / ``patches``（避免循环导入）。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

_LOCK = threading.RLock()

# 上游原始实现：优先挂在 owner 自身的 ``_engine_originals`` 属性上（模块/类都支持），
# 不可写对象退化为 id 键字典（极少见）。
_ORIGINALS_FALLBACK: Dict[Tuple[int, str], Any] = {}
_ORIGINALS_KEEP: Dict[int, Any] = {}  # 防止 owner 被回收后 id 复用导致串台


@dataclass
class PatchSession:
    """一次运行内的补丁上下文（emitter / 状态 / 幂等标记 / warning 计数）。"""

    emitter: Optional[Any] = None
    state: Dict[str, Any] = field(default_factory=dict)
    manual: Optional[Any] = None
    warned_once: Set[str] = field(default_factory=set)
    installed: Set[str] = field(default_factory=set)
    warning_counts: Dict[str, int] = field(default_factory=dict)
    warning_total: int = 0
    error_total: int = 0

    # ---- 幂等标记 ----------------------------------------------------------
    def mark_installed(self, key: str) -> bool:
        """标记一次安装；返回 ``False`` 表示本会话已装过（调用方应直接返回）。"""
        with _LOCK:
            if key in self.installed:
                return False
            self.installed.add(key)
            return True

    def is_installed(self, key: str) -> bool:
        with _LOCK:
            return key in self.installed

    # ---- 只提醒一次的 warning ----------------------------------------------
    def should_warn_once(self, key: str) -> bool:
        """``True`` = 本会话首次（应当发 warning）；``False`` = 已提醒过。"""
        with _LOCK:
            if key in self.warned_once:
                return False
            self.warned_once.add(key)
            return True


_SESSION: PatchSession = PatchSession()


# --------------------------------------------------------------------------------------
# 会话生命周期
# --------------------------------------------------------------------------------------
def session_lock() -> threading.RLock:
    """跨线程保护会话状态（补丁可能从 Playwright 回调线程发事件）。"""
    return _LOCK


def current_session() -> PatchSession:
    return _SESSION


def start_session(
    emitter: Optional[Any] = None,
    state: Optional[Dict[str, Any]] = None,
    manual: Optional[Any] = None,
) -> PatchSession:
    """开始一次新的补丁会话（每次运行一个）：清空 warned_once / installed / 计数。"""
    global _SESSION
    with _LOCK:
        _SESSION = PatchSession(emitter=emitter, state=dict(state or {}), manual=manual)
        return _SESSION


def reset_session() -> PatchSession:
    """清空当前会话（同进程重跑 / 自测）：warning 可再次发出、补丁可重新包装。"""
    return start_session()


def ensure_session(session: Optional[PatchSession] = None) -> PatchSession:
    """拿到要用的会话：显式传入优先，否则用当前会话。"""
    return session if session is not None else _SESSION


# --------------------------------------------------------------------------------------
# 上游原始实现（重新包装的基线）
# --------------------------------------------------------------------------------------
def remember_originals(owner: Any, attrs: Iterable[str]) -> Dict[str, Any]:
    """记住 ``owner``（模块/类）上的上游原始实现（只在第一次记住），返回 name -> 原实现。"""
    attrs = tuple(attrs)
    with _LOCK:
        slot = getattr(owner, "_engine_originals", None)
        if not isinstance(slot, dict):
            slot = {}
            try:
                setattr(owner, "_engine_originals", slot)
            except Exception:  # noqa: BLE001 - 不可写对象（少见）退化为 id 键字典
                slot = None
        result: Dict[str, Any] = {}
        for attr in attrs:
            if slot is not None:
                if attr not in slot:
                    slot[attr] = getattr(owner, attr)
                result[attr] = slot[attr]
                continue
            key = (id(owner), attr)
            if key not in _ORIGINALS_FALLBACK:
                _ORIGINALS_KEEP[id(owner)] = owner  # 保活，防止 id 复用
                _ORIGINALS_FALLBACK[key] = getattr(owner, attr)
            result[attr] = _ORIGINALS_FALLBACK[key]
        return result


def restore_originals(owner: Any, attrs: Iterable[str]) -> None:
    """把 ``owner`` 的属性恢复到 :func:`remember_originals` 记录的基线（避免套娃）。"""
    attrs = tuple(attrs)
    with _LOCK:
        for attr in attrs:
            slot = getattr(owner, "_engine_originals", None)
            if isinstance(slot, dict) and attr in slot:
                setattr(owner, attr, slot[attr])
                continue
            key = (id(owner), attr)
            if key in _ORIGINALS_FALLBACK:
                setattr(owner, attr, _ORIGINALS_FALLBACK[key])
                continue
            raise KeyError(f"remember_originals 未记录：{getattr(owner, '__name__', owner)!r}.{attr}")


def originals_of(owner: Any, attr: str) -> Any:
    """读取已记住的上游原始实现（未记录返回 ``None``）。"""
    slot = getattr(owner, "_engine_originals", None)
    if isinstance(slot, dict) and attr in slot:
        return slot[attr]
    return _ORIGINALS_FALLBACK.get((id(owner), attr))

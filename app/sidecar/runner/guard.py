# -*- coding: utf-8 -*-
"""并发闸门：login/test/run 互斥与账号级独占（M2 从 engine_runner.py 纯搬迁）。"""

from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional

log = logging.getLogger("sidecar.engine")  # 沿用旧 logger 名：日志输出与拆分前逐字节一致


BUSY_GLOBAL_MESSAGE = "当前有登录/采集任务进行中，请等待结束或先取消"
BUSY_PLATFORM_TEST_MESSAGE = "该平台正在检测，请稍候"
BUSY_PLATFORM_LOGIN_MESSAGE = "该平台正在登录，请稍候"
BUSY_ACCOUNT_MESSAGE = "该账号正在被登录/检测/采集任务使用，请稍候"
# 兼容旧引用（登录/采集全局互斥的默认文案）
_BUSY_MESSAGE = BUSY_GLOBAL_MESSAGE

class ActivityGuard:
    """并发闸门：login 全局唯一；login/test 按平台/账号互斥；run 全局保守；账号级独占。

    账号化（契约 §6.2）：
    * ``acquire_account(account_id, ref)`` 保证同一账号 profile 同一时间只被一个引擎进程使用；
    * test 槽按 ``platform`` 保留旧语义（account_id 为空），指定账号时按 ``platform@account_id``；
    * login 仍全局唯一（一次只弹一个 Edge 登录窗）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 全局互斥槽（login / run 二选一）；login 时记录平台，用于按平台解耦
        self._exclusive_kind: Optional[str] = None
        self._exclusive_ref: Optional[str] = None
        self._exclusive_platform: Optional[str] = None
        # 按平台的 test 槽：旧语义 key=platform；账号级 key="platform@account_id"
        self._tests: Dict[str, str] = {}
        # 账号独占槽：account_id -> ref（一账号一引擎进程）
        self._accounts: Dict[str, str] = {}

    # -- login / run ----------------------------------------------------
    def acquire_exclusive(
        self,
        kind: str,
        ref: Optional[str] = None,
        platform: Optional[str] = None,
    ) -> Optional[str]:
        """申请 login/run 全局槽；成功返回 None，失败返回 BUSY message。

        - 已有 login/run → BUSY_GLOBAL_MESSAGE（同一时间只允许一个 login/run）
        - kind=login 且该平台正在 test → BUSY_PLATFORM_TEST_MESSAGE
        - kind=login 且其它平台在 test → 允许（不同 browser_data profile 可并行）
        - kind=run → 保守：有任意 test 则 BUSY_GLOBAL_MESSAGE
        """
        with self._lock:
            if self._exclusive_kind is not None:
                return BUSY_GLOBAL_MESSAGE
            if kind == "login":
                if platform is not None and self._platform_has_test(platform):
                    return BUSY_PLATFORM_TEST_MESSAGE
                # 其它平台的 test 允许与登录并行
            else:  # run：全局保守，任何 test 都拒绝
                if self._tests:
                    return BUSY_GLOBAL_MESSAGE
            self._exclusive_kind = kind
            self._exclusive_ref = ref
            self._exclusive_platform = platform if kind == "login" else None
            return None

    def release_exclusive(self, ref: Optional[str] = None) -> None:
        with self._lock:
            if ref is not None and self._exclusive_ref is not None and ref != self._exclusive_ref:
                return
            self._exclusive_kind = None
            self._exclusive_ref = None
            self._exclusive_platform = None

    # -- test：按平台/账号并行 -------------------------------------------
    @staticmethod
    def _test_key(platform: str, account_id: str = "") -> str:
        account_id = str(account_id or "").strip()
        return f"{platform}@{account_id}" if account_id else str(platform)

    def _platform_has_test(self, platform: str) -> bool:
        """调用方需持锁：某平台是否有任意 test 槽（含账号级）。"""
        for key in self._tests:
            if key == platform or key.startswith(f"{platform}@"):
                return True
        return False

    def acquire_test(
        self,
        platform: str,
        ref: Optional[str] = None,
        account_id: str = "",
    ) -> Optional[str]:
        """申请 test 槽；成功返回 None。

        - 同平台同账号已有 test → BUSY_PLATFORM_TEST_MESSAGE（旧接口 account_id 空=平台级）
        - 有 run 进行中 → BUSY_GLOBAL_MESSAGE
        - 有 login：同平台 → BUSY_PLATFORM_LOGIN_MESSAGE；其它平台 → 允许
        - 账号已被占用（run/login 持有账号锁）→ BUSY_ACCOUNT_MESSAGE
        """
        key = self._test_key(platform, account_id)
        with self._lock:
            if self._exclusive_kind == "run":
                return BUSY_GLOBAL_MESSAGE
            if key in self._tests:
                return BUSY_PLATFORM_TEST_MESSAGE
            if self._exclusive_kind == "login" and self._exclusive_platform == platform:
                return BUSY_PLATFORM_LOGIN_MESSAGE
            account_id = str(account_id or "").strip()
            if account_id and account_id in self._accounts:
                return BUSY_ACCOUNT_MESSAGE
            self._tests[key] = ref or ""
            return None

    def release_test(
        self,
        platform: str,
        ref: Optional[str] = None,
        account_id: str = "",
    ) -> None:
        key = self._test_key(platform, account_id)
        with self._lock:
            current = self._tests.get(key)
            if current is None:
                return
            if ref is not None and current and current != ref:
                return
            self._tests.pop(key, None)

    # -- 账号独占（一账号一 profile 一进程） ------------------------------
    def acquire_account(self, account_id: str, ref: Optional[str] = None) -> Optional[str]:
        """申请账号独占锁；成功返回 None，被占用返回 BUSY_ACCOUNT_MESSAGE。"""
        account_id = str(account_id or "").strip()
        if not account_id:
            return None
        with self._lock:
            if account_id in self._accounts:
                return BUSY_ACCOUNT_MESSAGE
            self._accounts[account_id] = ref or ""
            return None

    def release_account(self, account_id: str, ref: Optional[str] = None) -> None:
        account_id = str(account_id or "").strip()
        if not account_id:
            return
        with self._lock:
            current = self._accounts.get(account_id)
            if current is None:
                return
            if ref is not None and current and current != ref:
                return
            self._accounts.pop(account_id, None)

    def is_account_busy(self, account_id: str) -> bool:
        account_id = str(account_id or "").strip()
        if not account_id:
            return False
        with self._lock:
            return account_id in self._accounts

    def account_ref(self, account_id: str) -> str:
        with self._lock:
            return self._accounts.get(str(account_id or "").strip(), "")

    # -- 查询 -------------------------------------------------------------
    def is_exclusive_busy(self) -> bool:
        with self._lock:
            return self._exclusive_kind is not None

    def is_test_busy(self, platform: Optional[str] = None) -> bool:
        with self._lock:
            if platform is None:
                return bool(self._tests)
            return self._platform_has_test(platform)

    def is_busy(self) -> bool:
        """是否有任意 login/run/test 在跑（delete_account 等场景用）。"""
        with self._lock:
            return self._exclusive_kind is not None or bool(self._tests)

    def describe(self) -> str:
        with self._lock:
            parts: List[str] = []
            if self._exclusive_kind is not None:
                label = self._exclusive_kind
                if self._exclusive_platform:
                    label += f"@{self._exclusive_platform}"
                parts.append(f"{label}:{self._exclusive_ref or ''}")
            if self._tests:
                parts.append("tests=" + ",".join(sorted(self._tests)))
            if self._accounts:
                parts.append("accounts=" + ",".join(sorted(self._accounts)))
            return ";".join(parts) if parts else "idle"

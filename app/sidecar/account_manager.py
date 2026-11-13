# -*- coding: utf-8 -*-
"""多账号号池管理（Phase C3）：账号 CRUD / 选号 / 配额 / 冷却 / 健康度 / 旧数据迁移。

设计要点（契约 IMPLEMENTATION_CONTRACT §2.3.1 / §6，勘察 R2）：

* **真源是 ``accounts`` 表**；``account_state`` 保留双写，旧 API/旧 UI 行为不变。
* **legacy 默认账号懒创建**：``ensure_legacy_default(platform)`` 在该平台没有默认账号时
  建 ``acc_legacy_<platform>``（``profile_layout="legacy"``、``profile_dir`` 指向旧目录
  ``browser_data/<platform>_user_data_dir``），旧单账号路径与 CLI 行为完全保留。
* **一进程一账号**：选号后由 ``ActivityGuard.acquire_account`` 保证同一账号 profile
  不会被两个引擎进程同时打开（RunManager / LoginManager / test / delete 都走同一把锁）。
* **硬过滤 + 可解释打分**：``pick()`` 返回 ``{account, score, reasons, candidates}``；
  没有可用账号抛 ``ServiceError("NO_ACCOUNT", ..., 409)``（不静默失败）。
* **失败分类**：登录失效 / 风控 → 标记账号并发布事件（换号由 RunManager 决策）；
  网络类错误由 RunManager 同号重试（本模块只更新健康度/退避）。
* **旧 profile 迁移**：只在空闲时做；rename 优先，失败回退 copy；失败保留旧目录并回滚 DB。

注意：本模块不允许弹真实浏览器；所有进程操作只做「按 profile marker 查杀残留 Edge」。
"""
from __future__ import annotations

import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from events import bus
from engine_runner import (
    PLATFORM_LABELS,
    PLATFORM_IDS,
    ActivityGuard,
    ServiceError,
    SidecarConfig,
    _to_int,
    kill_browser_processes_by_marker,
    log,
)
from state import StateStore, now_ts

# 选号策略（manual 由调用方显式指定 account_id）
DEFAULT_STRATEGY = "lru"
STRATEGIES = ("lru", "best_health", "round_robin", "manual")

# 账号状态机（契约 §3.2 参考）
ACCOUNT_STATUSES = ("active", "cooldown", "need_login", "disabled", "risk", "archived")
# 可被自动选号的状态（login_state=ok 也是硬条件）
SELECTABLE_STATUSES = ("active", "cooldown")

# 默认配额/限流（可被 app_settings: pool.limits / pool.platform_min_interval_s 覆盖）
DEFAULT_LIMITS: Dict[str, int] = {"daily_runs": 20, "daily_saved_items": 2000}
DEFAULT_PLATFORM_MIN_INTERVAL_S = 30

# 连续失败退避：30s → 60s → 120s → 240s → 480s → 960s → 1920s → 3600s(上限)
COOLDOWN_BASE_S = 30
COOLDOWN_MAX_S = 3600

# 失败分类关键词（与 engine_runner._is_retryable_crawl_error 的不可重试名单保持口径）
RISK_TOKENS = (
    "封禁",
    "账号异常",
    "风控",
    "风险",
    "操作频繁",
    "请求频繁",
    "安全验证",
    "restricted",
    "forbidden",
    "banned",
    "risk control",
)
LOGIN_TOKENS = (
    "未登录",
    "登录失效",
    "登录失败",
    "需要登录",
    "请重新登录",
    "重新扫码",
    "扫码",
    "二维码",
    "验证码",
    "滑块",
    "拼图",
    "not logged",
    "login required",
    "login failed",
    "login expired",
    "captcha",
    "slider",
)
NETWORK_TOKENS = (
    "err_connection",
    "err_timed_out",
    "err_name_not_resolved",
    "err_network_changed",
    "net::err_",
    "page.goto",
    "retryerror",
    "datafetcherror",
    "timeouterror",
    "timeout",
    "timed out",
    "超时",
    "connection",
    "目标页关闭",
    "browser has been closed",
)


def _today(day: Optional[str] = None) -> str:
    """本地日（YYYY-MM-DD）；``day`` 显式传入时原样返回（便于测试）。"""
    if day:
        return str(day)
    return datetime.now().strftime("%Y-%m-%d")


def _today_from_ts(ts: int) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return _today()


def cooldown_delay(consecutive_fail: int) -> int:
    """指数退避：30s → 60s → … → 上限 1h（``consecutive_fail<=0`` 返回 0）。"""
    count = max(0, int(consecutive_fail or 0))
    if count <= 0:
        return 0
    return min(COOLDOWN_MAX_S, COOLDOWN_BASE_S * (2 ** (count - 1)))


def classify_failure(message: str) -> str:
    """把采集/测试失败消息分类：``login_invalid | risk_blocked | network | other``。"""
    text = str(message or "").strip()
    lowered = text.lower()
    if not text:
        return "other"
    for token in RISK_TOKENS:
        if token in lowered or token in text:
            return "risk_blocked"
    for token in LOGIN_TOKENS:
        if token in lowered or token in text:
            return "login_invalid"
    for token in NETWORK_TOKENS:
        if token in lowered or token in text:
            return "network"
    return "other"


def normalize_strategy(strategy: Optional[str]) -> str:
    value = str(strategy or "").strip().lower()
    if value in STRATEGIES:
        return value
    return DEFAULT_STRATEGY


class AccountManager:
    """账号池门面：CRUD + 选号 + 记账 + 事件 + 迁移。

    构造参数 ``guard`` 可为 None（单元测试只测 DB 逻辑时不传），
    生产环境由 ``EngineService`` 注入同一个 :class:`ActivityGuard`。
    """

    def __init__(
        self,
        cfg: SidecarConfig,
        state: StateStore,
        guard: Optional[ActivityGuard] = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.guard = guard
        self._lock = threading.RLock()
        self._strategy_cache: Optional[str] = None

    # ------------------------------------------------------------------
    # 基础行操作
    # ------------------------------------------------------------------
    def _rows(self, platform: Optional[str] = None, include_archived: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM accounts"
        params: List[Any] = []
        clauses: List[str] = []
        if platform:
            clauses.append("platform = ?")
            params.append(str(platform))
        if not include_archived:
            clauses.append("status != 'archived'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, account_id ASC"
        return self.state.query(sql, tuple(params))

    def _row(self, account_id: str) -> Dict[str, Any]:
        rows = self.state.query(
            "SELECT * FROM accounts WHERE account_id = ?", (str(account_id or ""),)
        )
        if not rows:
            raise ServiceError(
                "ACCOUNT_NOT_FOUND", f"账号不存在：{account_id}", 404
            )
        return rows[0]

    def _update_row(self, account_id: str, **fields: Any) -> None:
        clean = {key: value for key, value in fields.items() if value is not None or key == "cooldown_until"}
        if not clean:
            return
        clean["updated_at"] = now_ts()
        sets = ", ".join("%s = ?" % key for key in clean)
        params = list(clean.values()) + [str(account_id)]
        affected = self.state.execute(
            "UPDATE accounts SET %s WHERE account_id = ?" % sets, tuple(params)
        )
        if not affected:
            raise ServiceError("ACCOUNT_NOT_FOUND", f"账号不存在：{account_id}", 404)

    def _insert_row(self, **fields: Any) -> None:
        params = dict(fields)
        params.setdefault("created_at", now_ts())
        params.setdefault("updated_at", now_ts())
        columns = ", ".join(params)
        placeholders = ", ".join("?" for _ in params)
        self.state.execute(
            "INSERT INTO accounts(%s) VALUES (%s)" % (columns, placeholders),
            tuple(params.values()),
        )

    # ------------------------------------------------------------------
    # 路径 / 展示
    # ------------------------------------------------------------------
    @staticmethod
    def _profile_rel(platform: str, account_id: str, layout: str = "v2") -> str:
        """账号 profile 相对路径（存库口径，统一 / 分隔）。"""
        if layout == "legacy":
            return "browser_data/%s_user_data_dir" % platform
        return "browser_data/%s/%s/user_data_dir" % (platform, account_id)

    def profile_path(self, account: Dict[str, Any]) -> Path:
        """账号 profile 目录的绝对 Path（相对库值按 data-dir 解析）。"""
        raw = str(account.get("profile_dir") or "").strip()
        if raw:
            path = Path(raw)
            return path if path.is_absolute() else (self.cfg.data_dir / path)
        return self.cfg.data_dir / self._profile_rel(
            account["platform"], account["account_id"], account.get("profile_layout") or "v2"
        )

    def profile_exists(self, account: Dict[str, Any]) -> bool:
        try:
            path = self.profile_path(account)
            return path.is_dir() and any(path.iterdir())
        except OSError:
            return False

    def profile_template(self, account: Dict[str, Any]) -> str:
        """传给 engine 的 ``--profile-template``（含恰好一个 ``%s`` 的绝对模板）。

        legacy 账号返回旧模板 ``browser_data/%s_user_data_dir``（迁移前保持旧目录）；
        v2 账号返回 ``browser_data/%s/<account_id>/user_data_dir``。
        """
        if (account.get("profile_layout") or "v2") == "legacy":
            return str(self.cfg.browser_data_dir / "%s_user_data_dir")
        return self.cfg.platform_profile_template(account["platform"], account["account_id"])

    def engine_account_args(self, account: Optional[Dict[str, Any]]) -> Dict[str, str]:
        """engine 子进程账号参数。

        旧单账号（legacy 默认账号 / 无账号）**不追加任何参数**：profile 走旧路径，
        引擎事件字段与历史版本逐字节一致（selftest 既有断言不受影响）。
        v2 账号才显式传 ``--account-id`` + ``--profile-template``。
        """
        if not account:
            return {"account_id": "", "profile_template": ""}
        if (account.get("profile_layout") or "v2") == "legacy":
            return {"account_id": "", "profile_template": ""}
        return {
            "account_id": str(account["account_id"]),
            "profile_template": self.profile_template(account),
        }

    def _display_alias(self, account: Dict[str, Any]) -> str:
        alias = str(account.get("alias") or "").strip()
        if alias:
            return alias
        label = PLATFORM_LABELS.get(str(account.get("platform") or ""), "")
        short = str(account.get("account_id") or "")[-6:]
        return f"{label}账号{short}" if label else f"账号{short}"

    def _quota_api(self, account_id: str) -> Dict[str, Any]:
        return self.quota_remaining(account_id)

    def _to_api(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """账号 API 对象（列表/详情/创建/更新统一形状）。"""
        row = dict(account)
        account_id = str(row.get("account_id") or "")
        quota = self._quota_api(account_id) if account_id else {}
        cooldown_until = row.get("cooldown_until")
        now = now_ts()
        remaining = max(0, int(cooldown_until) - now) if cooldown_until else 0
        result = {
            "account_id": account_id,
            "platform": row.get("platform") or "",
            "platform_label": PLATFORM_LABELS.get(str(row.get("platform") or ""), ""),
            "alias": row.get("alias") or "",
            "display_alias": self._display_alias(row),
            "status": row.get("status") or "need_login",
            "login_state": row.get("login_state") or "unknown",
            "profile_dir": row.get("profile_dir") or "",
            "profile_layout": row.get("profile_layout") or "v2",
            "profile_exists": self.profile_exists(row),
            "profile_path": str(self.profile_path(row)),
            "health_score": _to_int(row.get("health_score"), 100),
            "success_count": _to_int(row.get("success_count"), 0),
            "fail_count": _to_int(row.get("fail_count"), 0),
            "consecutive_fail": _to_int(row.get("consecutive_fail"), 0),
            "last_login_at": row.get("last_login_at"),
            "last_checked_at": row.get("last_checked_at"),
            "last_used_at": row.get("last_used_at"),
            "cooldown_until": cooldown_until,
            "cooldown_remaining_s": remaining,
            "risk_level": row.get("risk_level") or "normal",
            "risk_note": row.get("risk_note") or "",
            "proxy_configured": bool(row.get("proxy_enc")),
            "is_default": bool(row.get("is_default")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "quota": quota,
        }
        return result

    def aliases(self) -> Dict[str, str]:
        """account_id -> 展示别名（run 详情回填用）。"""
        result: Dict[str, str] = {}
        for row in self.state.query("SELECT * FROM accounts"):
            result[str(row["account_id"])] = self._display_alias(row)
        return result

    def alias_of(self, account_id: str) -> str:
        if not account_id:
            return ""
        try:
            return self._display_alias(self._row(account_id))
        except ServiceError:
            return ""

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def list(self, platform: Optional[str] = None, include_archived: bool = False) -> List[Dict[str, Any]]:
        """列出账号（默认不含 archived）；平台顺序固定便于 UI 分组。"""
        rows = self._rows(platform=platform, include_archived=include_archived)
        order = {pid: index for index, pid in enumerate(PLATFORM_IDS)}
        rows.sort(key=lambda row: (order.get(str(row.get("platform") or ""), 99), row.get("created_at") or 0))
        return [self._to_api(row) for row in rows]

    def get(self, account_id: str) -> Dict[str, Any]:
        return self._to_api(self._row(account_id))

    def _set_default_locked(self, account_id: str, platform: str) -> None:
        """把 account_id 设为平台唯一默认账号（调用方持 self._lock）。"""
        self.state.execute(
            "UPDATE accounts SET is_default = 0, updated_at = ? "
            "WHERE platform = ? AND account_id != ?",
            (now_ts(), str(platform), str(account_id)),
        )
        self.state.execute(
            "UPDATE accounts SET is_default = 1, updated_at = ? WHERE account_id = ?",
            (now_ts(), str(account_id)),
        )

    def _default_row(self, platform: str, include_archived: bool = False) -> Optional[Dict[str, Any]]:
        rows = self._rows(platform=platform, include_archived=include_archived)
        defaults = [row for row in rows if int(row.get("is_default") or 0)]
        if defaults:
            return defaults[0]
        return None

    def create(
        self,
        platform: str,
        alias: str = "",
        proxy_url: Optional[str] = None,
        *,
        account_id: str = "",
        profile_layout: str = "v2",
        status: str = "need_login",
        login_state: str = "unknown",
        is_default: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """新建账号（默认 v2 独立 profile；``account_id`` 仅供迁移/测试注入）。"""
        platform = str(platform or "").strip()
        if platform not in PLATFORM_LABELS:
            raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
        profile_layout = "legacy" if profile_layout == "legacy" else "v2"
        with self._lock:
            new_id = str(account_id or "").strip() or ("acc_" + uuid.uuid4().hex[:12])
            existing = self.state.query(
                "SELECT account_id FROM accounts WHERE account_id = ?", (new_id,)
            )
            if existing:
                raise ServiceError("ACCOUNT_EXISTS", f"账号已存在：{new_id}", 409)
            current_default = self._default_row(platform)
            default_flag = bool(is_default) if is_default is not None else current_default is None
            if default_flag:
                self.state.execute(
                    "UPDATE accounts SET is_default = 0, updated_at = ? WHERE platform = ?",
                    (now_ts(), platform),
                )
            proxy_enc = self._protect_proxy(proxy_url)
            ts = now_ts()
            self._insert_row(
                account_id=new_id,
                platform=platform,
                alias=str(alias or "").strip(),
                status=str(status or "need_login"),
                login_state=str(login_state or "unknown"),
                profile_dir=self._profile_rel(platform, new_id, profile_layout),
                profile_layout=profile_layout,
                health_score=100,
                success_count=0,
                fail_count=0,
                consecutive_fail=0,
                last_login_at=None,
                last_checked_at=None,
                last_used_at=None,
                cooldown_until=None,
                risk_level="normal",
                risk_note="",
                proxy_enc=proxy_enc,
                is_default=1 if default_flag else 0,
                created_at=ts,
                updated_at=ts,
            )
            return self._to_api(self._row(new_id))

    @staticmethod
    def _protect_proxy(proxy_url: Optional[str]) -> Optional[str]:
        text = str(proxy_url or "").strip()
        if not text:
            return None
        try:
            from secret_store import protect
        except Exception as exc:  # noqa: BLE001 - 密钥模块缺失时不落明文
            raise ServiceError(
                "SECRET_STORE_UNAVAILABLE", f"代理密钥加密组件不可用：{exc}", 500
            ) from exc
        try:
            return protect(text)
        except Exception as exc:  # noqa: BLE001
            raise ServiceError("SECRET_PROTECT_FAILED", f"代理密钥加密失败：{exc}", 500) from exc

    def update(self, account_id: str, **fields: Any) -> Dict[str, Any]:
        """更新账号属性（alias/status/enabled/is_default/risk_note/proxy_url/...）。"""
        allowed = {
            "alias",
            "status",
            "login_state",
            "risk_level",
            "risk_note",
            "is_default",
            "health_score",
            "cooldown_until",
            "enabled",
            "proxy_url",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ServiceError(
                "INVALID_PARAMS", "不支持的字段：" + ",".join(sorted(unknown)), 400
            )
        with self._lock:
            row = self._row(account_id)
            updates: Dict[str, Any] = {}
            if "alias" in fields:
                updates["alias"] = str(fields["alias"] or "").strip()
            if "risk_note" in fields:
                updates["risk_note"] = str(fields["risk_note"] or "")
            if "risk_level" in fields:
                level = str(fields["risk_level"] or "normal").strip().lower()
                if level not in ("normal", "watch", "high"):
                    raise ServiceError("INVALID_PARAMS", f"非法 risk_level：{level}", 400)
                updates["risk_level"] = level
            if "login_state" in fields:
                state_value = str(fields["login_state"] or "").strip().lower()
                if state_value not in ("unknown", "ok", "expired"):
                    raise ServiceError("INVALID_PARAMS", f"非法 login_state：{state_value}", 400)
                updates["login_state"] = state_value
            if "health_score" in fields:
                updates["health_score"] = max(0, min(100, _to_int(fields["health_score"], 100)))
            if "cooldown_until" in fields:
                raw = fields["cooldown_until"]
                updates["cooldown_until"] = None if raw in (None, "", 0) else _to_int(raw, 0) or None
            status_value: Optional[str] = None
            if "enabled" in fields:
                enabled = bool(fields["enabled"])
                status_value = "active" if enabled else "disabled"
            if "status" in fields:
                raw_status = str(fields["status"] or "").strip().lower()
                mapping = {
                    "enable": "active",
                    "enabled": "active",
                    "active": "active",
                    "disable": "disabled",
                    "disabled": "disabled",
                    "need_login": "need_login",
                    "cooldown": "cooldown",
                    "risk": "risk",
                    "archived": "archived",
                }
                if raw_status not in mapping:
                    raise ServiceError("INVALID_PARAMS", f"非法 status：{raw_status}", 400)
                status_value = mapping[raw_status]
            if status_value is not None:
                if status_value != "archived" and row.get("status") == "archived":
                    # 允许从归档恢复（软删可逆），显式状态优先
                    pass
                updates["status"] = status_value
                if status_value == "active" and row.get("login_state") != "ok":
                    # 仅手动启用不解锁登录态，避免未登录账号被自动选中
                    pass
            if "proxy_url" in fields:
                updates["proxy_enc"] = self._protect_proxy(fields["proxy_url"])
            if fields.get("is_default"):
                updates["is_default"] = 1
                self.state.execute(
                    "UPDATE accounts SET is_default = 0, updated_at = ? "
                    "WHERE platform = ? AND account_id != ?",
                    (now_ts(), str(row["platform"]), str(account_id)),
                )
            elif "is_default" in fields and not fields.get("is_default"):
                updates["is_default"] = 0
            if updates:
                self._update_row(account_id, **updates)
            return self._to_api(self._row(account_id))

    def set_default(self, account_id: str) -> Dict[str, Any]:
        return self.update(account_id, is_default=True)

    def archive(self, account_id: str, purge_profile: bool = False) -> Dict[str, Any]:
        """软删账号（archived，保留 run 历史）；``purge_profile=True`` 时连 profile 目录删除。"""
        with self._lock:
            row = self._row(account_id)
            if purge_profile:
                if self._account_busy(account_id):
                    raise ServiceError("ACCOUNT_BUSY", "该账号正在登录/检测/采集中，无法删除 profile", 409)
                self._remove_profile(row)
            self._update_row(
                account_id,
                status="archived",
                is_default=0,
                login_state="unknown",
                cooldown_until=None,
            )
            return {
                "ok": True,
                "account_id": str(account_id),
                "purged": bool(purge_profile),
                "account": self._to_api(self._row(account_id)),
            }

    # ------------------------------------------------------------------
    # 旧账号懒适配
    # ------------------------------------------------------------------
    def _legacy_profile_dir(self, platform: str) -> Path:
        return self.cfg.browser_data_dir / f"{platform}_user_data_dir"

    def _old_profile_exists(self, platform: str) -> bool:
        try:
            path = self._legacy_profile_dir(platform)
            return path.is_dir() and any(path.iterdir())
        except OSError:
            return False

    def ensure_legacy_default(self, platform: str) -> Dict[str, Any]:
        """确保平台存在默认账号（旧库懒适配）：无默认账号时建 legacy 默认账号。

        幂等：重复调用返回同一账号；已存在手工账号（v2）但无默认时，选最早的
        legacy/v2 账号作为默认，不重复建 legacy 行。
        """
        platform = str(platform or "").strip()
        if platform not in PLATFORM_LABELS:
            raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
        with self._lock:
            default = self._default_row(platform, include_archived=True)
            if default is not None and default.get("status") != "archived":
                return self._to_api(default)
            rows = self._rows(platform=platform, include_archived=True)
            alive = [row for row in rows if row.get("status") != "archived"]
            if alive:
                # 已有账号但没默认：优先 legacy 布局，其次最早创建
                legacy = [row for row in alive if (row.get("profile_layout") or "v2") == "legacy"]
                chosen = (legacy or alive)[0]
                self._set_default_locked(str(chosen["account_id"]), platform)
                return self._to_api(self._row(str(chosen["account_id"])))
            # 全新平台：建 legacy 默认账号，登录态回填自旧 account_state
            states = self.state.get_account_states()
            old = states.get(platform) or {}
            logged_in = old.get("logged_in")
            profile_exists = self._old_profile_exists(platform) or bool(old.get("profile_exists"))
            if logged_in is True:
                status, login_state = "active", "ok"
                message = "已登录"
            elif logged_in is False:
                status, login_state = "need_login", "expired"
                message = old.get("message") or "未登录"
            else:
                status, login_state = "need_login", "unknown"
                message = old.get("message") or ""
            ts = now_ts()
            account_id = f"acc_legacy_{platform}"
            self.state.execute(
                "INSERT OR IGNORE INTO accounts(account_id, platform, alias, status, login_state, profile_dir, "
                "profile_layout, health_score, is_default, risk_level, risk_note, created_at, updated_at, "
                "last_checked_at) VALUES (?, ?, '默认账号', ?, ?, ?, 'legacy', 100, 1, 'normal', ?, ?, ?, ?)",
                (
                    account_id,
                    platform,
                    status,
                    login_state,
                    self._profile_rel(platform, account_id, "legacy"),
                    message,
                    ts,
                    ts,
                    old.get("last_checked_at"),
                ),
            )
            log.info(
                "账号池：为平台 %s 创建 legacy 默认账号 %s（profile_layout=legacy, profile_exists=%s）",
                platform,
                account_id,
                profile_exists,
            )
            return self._to_api(self._row(account_id))

    def ensure_all_legacy_defaults(self) -> List[Dict[str, Any]]:
        return [self.ensure_legacy_default(platform) for platform in PLATFORM_IDS]

    def resolve(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        """解析平台默认账号（account_id 空）或校验指定账号归属。"""
        platform = str(platform or "").strip()
        if platform not in PLATFORM_LABELS:
            raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
        if not account_id:
            return self.ensure_legacy_default(platform)
        row = self._row(account_id)
        if str(row.get("platform")) != platform:
            raise ServiceError(
                "ACCOUNT_NOT_FOUND", f"账号 {account_id} 不属于平台 {platform}", 404
            )
        return self._to_api(row)

    # ------------------------------------------------------------------
    # 配额 / 限流设置
    # ------------------------------------------------------------------
    def limits(self) -> Dict[str, int]:
        raw = self.state.get_setting("pool.limits", None)
        result = dict(DEFAULT_LIMITS)
        if isinstance(raw, dict):
            for key in DEFAULT_LIMITS:
                if key in raw:
                    result[key] = max(0, _to_int(raw.get(key), DEFAULT_LIMITS[key]))
        return result

    def platform_min_interval_s(self) -> int:
        raw = self.state.get_setting("pool.platform_min_interval_s", DEFAULT_PLATFORM_MIN_INTERVAL_S)
        return max(0, _to_int(raw, DEFAULT_PLATFORM_MIN_INTERVAL_S))

    def default_strategy(self) -> str:
        raw = self.state.get_setting("pool.default_strategy", DEFAULT_STRATEGY)
        return normalize_strategy(raw)

    def settings(self) -> Dict[str, Any]:
        return {
            "default_strategy": self.default_strategy(),
            "limits": self.limits(),
            "platform_min_interval_s": self.platform_min_interval_s(),
            "strategies": list(STRATEGIES),
        }

    def update_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(payload or {})
        if "default_strategy" in payload:
            strategy = str(payload["default_strategy"] or "").strip().lower()
            if strategy not in STRATEGIES:
                raise ServiceError("INVALID_PARAMS", f"非法策略：{strategy}", 400)
            self.state.set_setting("pool.default_strategy", strategy)
        if "limits" in payload and isinstance(payload["limits"], dict):
            limits = self.limits()
            for key in DEFAULT_LIMITS:
                if key in payload["limits"]:
                    limits[key] = max(0, _to_int(payload["limits"][key], limits[key]))
            self.state.set_setting("pool.limits", limits)
        if "platform_min_interval_s" in payload:
            value = max(0, _to_int(payload["platform_min_interval_s"], DEFAULT_PLATFORM_MIN_INTERVAL_S))
            self.state.set_setting("pool.platform_min_interval_s", value)
        return self.settings()

    def _refund_quota(self, account_id: str, runs: int = 0, platform_runs: int = 0) -> None:
        """回滚一次「没有真正打到平台」的尝试占用的日配额（例如未登录直接失败）。

        与 :meth:`_bump_quota` 不同，这里做减法并在 SQL 层夹到 0，避免出现负数配额。
        """
        account_id = str(account_id or "")
        if not account_id:
            return
        self.state.execute(
            "UPDATE account_quota SET runs = MAX(0, runs - ?), platform_runs = MAX(0, platform_runs - ?), "
            "updated_at = ? WHERE account_id = ? AND day = ?",
            (
                max(0, int(runs)),
                max(0, int(platform_runs)),
                now_ts(),
                account_id,
                _today(),
            ),
        )

    def quota_row(self, account_id: str, day: Optional[str] = None) -> Dict[str, Any]:
        day_str = _today(day)
        rows = self.state.query(
            "SELECT * FROM account_quota WHERE account_id = ? AND day = ?",
            (str(account_id), day_str),
        )
        if not rows:
            return {
                "account_id": str(account_id),
                "day": day_str,
                "runs": 0,
                "platform_runs": 0,
                "saved_items": 0,
                "updated_at": None,
            }
        return dict(rows[0])

    def quota_remaining(self, account_id: str, day: Optional[str] = None) -> Dict[str, Any]:
        limits = self.limits()
        row = self.quota_row(account_id, day)
        used_runs = _to_int(row.get("runs"), 0)
        used_items = _to_int(row.get("saved_items"), 0)
        runs_limit = int(limits["daily_runs"])
        items_limit = int(limits["daily_saved_items"])
        reasons: List[str] = []
        if runs_limit > 0 and used_runs >= runs_limit:
            reasons.append(f"日采集次数已达上限（{used_runs}/{runs_limit}）")
        if items_limit > 0 and used_items >= items_limit:
            reasons.append(f"日入库条数已达上限（{used_items}/{items_limit}）")
        return {
            "account_id": str(account_id),
            "day": row["day"],
            "runs": used_runs,
            "platform_runs": _to_int(row.get("platform_runs"), 0),
            "saved_items": used_items,
            "day_runs_limit": runs_limit,
            "day_items_limit": items_limit,
            "runs_remaining": max(0, runs_limit - used_runs) if runs_limit > 0 else None,
            "items_remaining": max(0, items_limit - used_items) if items_limit > 0 else None,
            "exceeded": bool(reasons),
            "exceeded_reasons": reasons,
        }

    def _bump_quota(self, account_id: str, runs: int = 0, platform_runs: int = 0, saved_items: int = 0) -> None:
        day = _today()
        ts = now_ts()
        self.state.execute(
            "INSERT INTO account_quota(account_id, day, runs, platform_runs, saved_items, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, day) DO UPDATE SET "
            "runs = account_quota.runs + excluded.runs, "
            "platform_runs = account_quota.platform_runs + excluded.platform_runs, "
            "saved_items = account_quota.saved_items + excluded.saved_items, "
            "updated_at = excluded.updated_at",
            (
                str(account_id),
                day,
                max(0, int(runs)),
                max(0, int(platform_runs)),
                max(0, int(saved_items)),
                ts,
            ),
        )

    # ------------------------------------------------------------------
    # 选号（硬过滤 + 打分）
    # ------------------------------------------------------------------
    def _account_busy(self, account_id: str) -> bool:
        if self.guard is None:
            return False
        try:
            return bool(self.guard.is_account_busy(account_id))
        except Exception:  # noqa: BLE001 - 闸门异常不阻塞选号
            return False

    def _hard_filters(self, row: Dict[str, Any], now: int, exclude: Sequence[str]) -> List[str]:
        """返回硬过滤原因列表；空列表=可选。"""
        blocked: List[str] = []
        account_id = str(row.get("account_id") or "")
        status = str(row.get("status") or "")
        if account_id in set(exclude):
            blocked.append("本轮已尝试过该账号")
        if status == "archived":
            blocked.append("账号已删除（archived）")
        elif status == "disabled":
            blocked.append("账号已被禁用")
        elif status == "risk":
            blocked.append("账号处于风控状态")
        elif status == "need_login":
            blocked.append("账号需要重新登录")
        elif status not in SELECTABLE_STATUSES:
            blocked.append(f"账号状态不可选（{status}）")
        cooldown_until = _to_int(row.get("cooldown_until"), 0)
        if cooldown_until and cooldown_until > now:
            blocked.append(f"冷却中（剩余 {cooldown_until - now} 秒）")
        if str(row.get("login_state") or "") != "ok":
            blocked.append("登录态未确认（需登录/测试通过）")
        quota = self.quota_remaining(account_id)
        if quota["exceeded"]:
            blocked.extend(quota["exceeded_reasons"])
        if self._account_busy(account_id):
            blocked.append("账号正在被其它任务使用")
        return blocked

    def _score(self, row: Dict[str, Any], strategy: str, now: int) -> tuple:
        """返回 ``(score, reasons)``；分数越高越优先（可解释）。"""
        health = _to_int(row.get("health_score"), 100)
        last_used = _to_int(row.get("last_used_at"), 0)
        idle_seconds = max(0, now - last_used) if last_used else 7 * 86400
        idle_days = min(idle_seconds, 7 * 86400) / 86400.0
        consecutive = min(_to_int(row.get("consecutive_fail"), 0), 3)
        default_bonus = 0.3 if int(row.get("is_default") or 0) else 0.0
        reasons: List[str] = []

        if strategy == "best_health":
            score = 4.0 * (health / 100.0) + 0.5 * (idle_days / 7.0) + default_bonus
            reasons.append(f"健康度 {health} → +{4.0 * health / 100.0:.2f}")
            reasons.append(f"空闲 {idle_days:.2f} 天 → +{0.5 * idle_days / 7.0:.2f}")
        elif strategy == "round_robin":
            score = 2.0 * (idle_days / 7.0) + 1.0 * (health / 100.0) + default_bonus
            reasons.append(f"空闲 {idle_days:.2f} 天（轮询权重最高）→ +{2.0 * idle_days / 7.0:.2f}")
            reasons.append(f"健康度 {health} → +{1.0 * health / 100.0:.2f}")
        elif strategy == "manual":
            score = 1.0 + default_bonus
            reasons.append("手动策略：优先平台默认账号")
        else:  # lru
            score = (
                3.0 * (health / 100.0)
                + 1.0 * (idle_days / 7.0)
                + default_bonus
                - 1.0 * (consecutive / 3.0)
            )
            reasons.append(f"健康度 {health} → +{3.0 * health / 100.0:.2f}")
            reasons.append(f"空闲 {idle_days:.2f} 天（LRU）→ +{idle_days / 7.0:.2f}")
        if default_bonus:
            reasons.append(f"默认账号 → +{default_bonus:.2f}")
        if consecutive:
            penalty = 1.0 * (consecutive / 3.0)
            reasons.append(f"连续失败 {_to_int(row.get('consecutive_fail'), 0)} 次 → -{penalty:.2f}")
            if strategy != "lru":
                score -= penalty
        return round(score, 6), reasons

    def _evaluate(
        self,
        platform: str,
        strategy: Optional[str],
        exclude: Sequence[str] = (),
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        strategy = normalize_strategy(strategy or self.default_strategy())
        now_int = int(now if now is not None else now_ts())
        rows = self._rows(platform=platform, include_archived=True)
        candidates: List[Dict[str, Any]] = []
        eligible: List[tuple] = []
        manual_row = self._default_row(platform) if strategy == "manual" else None
        for row in rows:
            reasons: List[str] = []
            blocked = self._hard_filters(row, now_int, exclude)
            if strategy == "manual" and manual_row is not None:
                if str(row.get("account_id")) != str(manual_row.get("account_id")):
                    blocked.append("手动策略：仅平台默认账号可选")
            score = 0.0
            if not blocked:
                score, reasons = self._score(row, strategy, now_int)
                eligible.append((score, row, reasons))
            candidates.append(
                {
                    "account_id": str(row.get("account_id") or ""),
                    "alias": self._display_alias(row),
                    "platform": str(row.get("platform") or ""),
                    "status": str(row.get("status") or ""),
                    "login_state": str(row.get("login_state") or ""),
                    "health_score": _to_int(row.get("health_score"), 100),
                    "last_used_at": row.get("last_used_at"),
                    "is_default": bool(int(row.get("is_default") or 0)),
                    "eligible": not blocked,
                    "blocked_reasons": blocked,
                    "score": round(score, 6),
                    "reasons": reasons if not blocked else [],
                }
            )
        selected: Optional[Dict[str, Any]] = None
        if eligible:
            score, row, reasons = max(
                eligible, key=lambda item: (item[0], str(item[1].get("account_id") or ""))
            )
            selected = {
                "account": self._to_api(row),
                "account_id": str(row.get("account_id") or ""),
                "platform": platform,
                "alias": self._display_alias(row),
                "score": round(score, 6),
                "reasons": reasons,
                "strategy": strategy,
            }
        candidates.sort(key=lambda item: (not item["eligible"], -item["score"]))
        return {
            "platform": platform,
            "strategy": strategy,
            "selected": selected,
            "candidates": candidates,
        }

    def pick(
        self,
        platform: str,
        strategy: Optional[str] = None,
        exclude: Iterable[str] = (),
        now: Optional[int] = None,
    ) -> Dict[str, Any]:
        """选一个可用账号；无可选账号抛 ``NO_ACCOUNT``（409）。

        返回可解释 dict：``{account, account_id, platform, alias, score, reasons, strategy, candidates}``。

        **旧单账号兼容**：平台只有 legacy 默认账号（尚未迁移的旧 profile）时，
        即使登录态未确认/冷却中，也返回该账号并标记 ``fallback=True``
        （``strategy="legacy_fallback"``）——旧版 sidecar 本来就把账号交给 engine
        由 engine 判定登录失效，账号池不得改变这一行为。
        """
        platform = str(platform or "").strip()
        if platform not in PLATFORM_LABELS:
            raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
        self.ensure_legacy_default(platform)
        exclude_tuple = tuple(exclude or ())
        evaluation = self._evaluate(platform, strategy, exclude=exclude_tuple, now=now)
        selected = evaluation.get("selected")
        if selected:
            selected["candidates"] = evaluation["candidates"]
            return selected
        if normalize_strategy(strategy or self.default_strategy()) != "manual":
            fallback = self.legacy_fallback(platform, exclude=exclude_tuple)
            if fallback is not None:
                fallback["candidates"] = evaluation["candidates"]
                fallback["fallback"] = True
                return fallback
        label = PLATFORM_LABELS.get(platform, platform)
        detail = "；".join(
            "%s: %s" % (item["alias"], "、".join(item["blocked_reasons"]))
            for item in evaluation["candidates"][:3]
            if item.get("blocked_reasons")
        )
        raise ServiceError(
            "NO_ACCOUNT",
            f"平台 {label} 没有可用账号（请登录/解禁/等待冷却/调整配额）"
            + (f"：{detail}" if detail else ""),
            409,
        )

    def pick_manual(self, platform: str, account_id: str) -> Dict[str, Any]:
        """手动指定账号：逐项校验并给出明确错误码。"""
        row = self._row(account_id)
        if str(row.get("platform")) != str(platform):
            raise ServiceError(
                "ACCOUNT_NOT_FOUND", f"账号 {account_id} 不属于平台 {platform}", 404
            )
        blocked = self._hard_filters(row, now_ts(), ())
        if blocked:
            reasons = "；".join(blocked)
            if any("正在被其它任务使用" in item for item in blocked):
                raise ServiceError("ACCOUNT_BUSY", f"账号 {account_id} 正在被占用：{reasons}", 409)
            if any("禁用" in item for item in blocked):
                raise ServiceError("ACCOUNT_DISABLED", f"账号已被禁用：{reasons}", 409)
            raise ServiceError("NO_ACCOUNT", f"账号不可用：{reasons}", 409)
        score, reasons = self._score(row, normalize_strategy(self.default_strategy()), now_ts())
        return {
            "account": self._to_api(row),
            "account_id": str(account_id),
            "platform": str(row.get("platform")),
            "alias": self._display_alias(row),
            "score": score,
            "reasons": reasons,
            "strategy": "manual",
            "candidates": [],
        }

    def preview(self, platform: Optional[str] = None, strategy: Optional[str] = None) -> Dict[str, Any]:
        """干跑选号（不写库、不占锁），供 ``GET /api/accounts/schedule/preview``。"""
        if platform:
            platform = str(platform).strip()
            if platform not in PLATFORM_LABELS:
                raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
            self.ensure_legacy_default(platform)
            result = self._evaluate(platform, strategy)
            result["default_strategy"] = self.default_strategy()
            return result
        self.ensure_all_legacy_defaults()
        return {
            "platforms": [self._evaluate(pid, strategy) for pid in PLATFORM_IDS],
            "default_strategy": self.default_strategy(),
            "strategy": normalize_strategy(strategy or self.default_strategy()),
        }

    def legacy_fallback(
        self, platform: str, exclude: Iterable[str] = ()
    ) -> Optional[Dict[str, Any]]:
        """旧单账号兼容兜底：legacy 默认账号即使登录态未确认/冷却中也允许沿用旧行为。

        仅当账号 ``profile_layout=legacy``（尚未迁移的旧单账号）且无其它硬阻塞
        （archived/disabled/配额/占用/本平台本轮已尝试）时返回；多账号 v2 池仍严格按
        ``pick`` 过滤。这样旧用户「未先登录直接采集 / 失败后立刻续采」的行为与旧版本
        一致（由引擎自己报未登录或失败），不会因为账号池的登录态/冷却过滤而提前
        ``NO_ACCOUNT`` 失败——旧版 sidecar 本来就没有这些闸门。
        """
        try:
            account = self.ensure_legacy_default(platform)
        except ServiceError:
            return None
        if (account.get("profile_layout") or "v2") != "legacy":
            return None
        row = self._row(account["account_id"])
        ignorable = ("登录态未确认", "账号需要重新登录", "冷却中")
        blocked = [
            item
            for item in self._hard_filters(row, now_ts(), tuple(exclude or ()))
            if not item.startswith(ignorable)
        ]
        if blocked:
            return None
        return {
            "account": account,
            "account_id": account["account_id"],
            "platform": platform,
            "alias": account.get("display_alias") or account.get("alias") or "",
            "score": 0.0,
            "reasons": ["旧单账号兼容：legacy 默认账号沿用旧行为（未强制要求登录态确认）"],
            "strategy": "legacy_fallback",
            "fallback": True,
            "candidates": [],
        }

    # ------------------------------------------------------------------
    # 采集记账
    # ------------------------------------------------------------------
    def mark_attempt(
        self,
        run_id: str,
        platform: str,
        attempt: int,
        account_id: str,
        reason: str = "auto",
    ) -> None:
        """记录一次账号尝试（run_accounts）+ 更新 run_platforms 账号列 + 日配额计数。"""
        attempt_int = max(1, _to_int(attempt, 1))
        account_id = str(account_id or "")
        ts = now_ts()
        self.state.execute(
            "INSERT INTO run_accounts(run_id, platform, attempt, account_id, reason, status, message, "
            "started_at, finished_at) VALUES (?, ?, ?, ?, ?, 'running', '', ?, NULL) "
            "ON CONFLICT(run_id, platform, attempt) DO UPDATE SET "
            "account_id = excluded.account_id, reason = excluded.reason, status = 'running', "
            "message = '', started_at = excluded.started_at, finished_at = NULL",
            (str(run_id), str(platform), attempt_int, account_id, str(reason or "auto"), ts),
        )
        self.state.update_platform(
            run_id,
            platform,
            account_id=account_id,
            account_attempts=attempt_int,
        )
        if account_id:
            self._bump_quota(account_id, runs=1, platform_runs=1)
            self._update_row(account_id, last_used_at=ts)

    def finish_attempt(
        self,
        run_id: str,
        platform: str,
        attempt: int,
        status: str,
        message: str = "",
        account_id: str = "",
    ) -> None:
        attempt_int = max(1, _to_int(attempt, 1))
        status_str = str(status or "failed")
        # 先取当前行：既拿到账号（调用方可能不传），也用来判断这次是不是「running → login_invalid」的首次跃迁，
        # 避免重复 finish 造成配额被多次回滚。
        prev_rows = self.state.query(
            "SELECT status, account_id FROM run_accounts WHERE run_id = ? AND platform = ? AND attempt = ?",
            (str(run_id), str(platform), attempt_int),
        )
        prev = dict(prev_rows[0]) if prev_rows else {}
        sets = "status = ?, message = ?, finished_at = ?"
        params: List[Any] = [status_str, str(message or ""), now_ts()]
        where = "run_id = ? AND platform = ? AND attempt = ?"
        args = [str(run_id), str(platform), attempt_int]
        if account_id:
            sets += ", account_id = ?"
            params.append(str(account_id))
        self.state.execute(
            "UPDATE run_accounts SET %s WHERE %s" % (sets, where), tuple(params + args)
        )
        # 未登录直接失败：这次尝试根本没打到平台的内容接口，不该白吃一次「日采集次数」。
        # mark_attempt() 在尝试开始时已 +1，这里在终态确认后回滚（幂等：只对 running→login_invalid 生效）。
        if status_str == "login_invalid" and str(prev.get("status") or "") == "running":
            target_account = str(account_id or prev.get("account_id") or "")
            self._refund_quota(target_account, runs=1, platform_runs=1)

    def max_attempt(self, run_id: str, platform: str) -> int:
        """该 run 该平台已有最大尝试序号（续采时 attempt 从其后继续，避免主键覆盖历史）。"""
        rows = self.state.query(
            "SELECT MAX(attempt) AS max_attempt FROM run_accounts WHERE run_id = ? AND platform = ?",
            (str(run_id), str(platform)),
        )
        if not rows or rows[0].get("max_attempt") is None:
            return 0
        return _to_int(rows[0].get("max_attempt"), 0)

    def last_attempt_accounts(self, run_id: str) -> Dict[str, str]:
        """每个平台最近一次尝试使用的账号（续采优先沿用）。"""
        rows = self.state.query(
            "SELECT platform, account_id, MAX(attempt) AS max_attempt FROM run_accounts "
            "WHERE run_id = ? GROUP BY platform",
            (str(run_id),),
        )
        return {
            str(row["platform"]): str(row["account_id"])
            for row in rows
            if row.get("account_id")
        }

    def run_accounts(self, run_id: str) -> List[Dict[str, Any]]:
        """run 的账号尝试明细（``GET /api/runs/{id}/accounts``）。"""
        rows = self.state.query(
            "SELECT * FROM run_accounts WHERE run_id = ? ORDER BY platform ASC, attempt ASC",
            (str(run_id),),
        )
        aliases = self.aliases()
        result: List[Dict[str, Any]] = []
        for row in rows:
            account_id = str(row.get("account_id") or "")
            result.append(
                {
                    "run_id": str(row.get("run_id") or ""),
                    "platform": str(row.get("platform") or ""),
                    "attempt": _to_int(row.get("attempt"), 1),
                    "account_id": account_id,
                    "account_alias": aliases.get(account_id, ""),
                    "reason": str(row.get("reason") or ""),
                    "status": str(row.get("status") or ""),
                    "message": str(row.get("message") or ""),
                    "started_at": row.get("started_at"),
                    "finished_at": row.get("finished_at"),
                }
            )
        return result

    # ------------------------------------------------------------------
    # 状态回调（登录/测试/采集结果）
    # ------------------------------------------------------------------
    def on_login_success(self, account_id: str) -> Dict[str, Any]:
        """登录 confirmed：登录态 ok、恢复 active、健康度小幅回升。"""
        with self._lock:
            row = self._row(account_id)
            health = min(100, _to_int(row.get("health_score"), 100) + 5)
            ts = now_ts()
            self._update_row(
                account_id,
                status="active",
                login_state="ok",
                health_score=health,
                consecutive_fail=0,
                last_login_at=ts,
                last_checked_at=ts,
                cooldown_until=None,
                risk_note="",
            )
            return self._to_api(self._row(account_id))

    def on_login_failed(self, account_id: str, reason: str = "") -> Dict[str, Any]:
        """登录失败（非用户取消）：登录态置 expired、状态 need_login（不直接归档）。

        注意：登录失败/二维码过期是人工操作结果，不加冷却（登录态 expired 本身
        已阻止自动选号），避免用户「重新扫码→立即采集」被冷却误伤。
        """
        with self._lock:
            row = self._row(account_id)
            consecutive = _to_int(row.get("consecutive_fail"), 0) + 1
            health = max(0, _to_int(row.get("health_score"), 100) - 20)
            ts = now_ts()
            self._update_row(
                account_id,
                status="need_login",
                login_state="expired",
                health_score=health,
                fail_count=_to_int(row.get("fail_count"), 0) + 1,
                consecutive_fail=consecutive,
                last_checked_at=ts,
                risk_note=str(reason or "")[:200],
            )
            return self._to_api(self._row(account_id))

    def on_test_result(
        self,
        account_id: str,
        ok: bool,
        message: str = "",
        network_error: bool = False,
    ) -> Dict[str, Any]:
        """测试连接结果：网络异常只更新时间/备注，不误标未登录。"""
        ts = now_ts()
        with self._lock:
            row = self._row(account_id)
            if network_error:
                self._update_row(account_id, last_checked_at=ts, risk_note=str(message or "")[:200])
                return self._to_api(self._row(account_id))
            if ok:
                return self.on_login_success(account_id)
            self._update_row(
                account_id,
                status="need_login",
                login_state="expired",
                last_checked_at=ts,
                risk_note=str(message or "未登录")[:200],
            )
            self._publish(
                "account.login_invalid",
                "error",
                "account:%s:login_invalid:%s" % (account_id, _today_from_ts(ts)),
                {
                    "account_id": str(account_id),
                    "account_alias": self._display_alias(row),
                    "platform": str(row.get("platform") or ""),
                    "status": "need_login",
                    "message": str(message or "登录失效"),
                },
            )
            return self._to_api(self._row(account_id))

    def on_crawl_result(
        self,
        account_id: str,
        ok: bool,
        message: str = "",
        saved_items: int = 0,
        platform: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        """采集结果回调：健康度/连续失败/冷却/配额 + 账号事件。

        返回 ``{"kind","account","cooldown_until"}``；``kind`` 供 RunManager 决定是否换号：
        ``login_invalid`` / ``risk_blocked`` 换号，``network`` / ``other`` 不换号（由调用方重试）。
        """
        ts = now_ts()
        with self._lock:
            row = self._row(account_id)
            platform = platform or str(row.get("platform") or "")
            if ok:
                health = min(100, _to_int(row.get("health_score"), 100) + 5)
                self._update_row(
                    account_id,
                    status="active",
                    login_state="ok",
                    health_score=health,
                    success_count=_to_int(row.get("success_count"), 0) + 1,
                    consecutive_fail=0,
                    last_used_at=ts,
                    last_checked_at=ts,
                    cooldown_until=None,
                )
                self._bump_quota(account_id, saved_items=max(0, _to_int(saved_items, 0)))
                return {
                    "kind": "ok",
                    "account": self._to_api(self._row(account_id)),
                    "cooldown_until": None,
                }

            kind = classify_failure(message)
            consecutive = _to_int(row.get("consecutive_fail"), 0) + 1
            health_loss = {"login_invalid": 20, "risk_blocked": 30, "network": 5}.get(kind, 10)
            health = max(0, _to_int(row.get("health_score"), 100) - health_loss)
            fields: Dict[str, Any] = {
                "health_score": health,
                "fail_count": _to_int(row.get("fail_count"), 0) + 1,
                "consecutive_fail": consecutive,
                "last_used_at": ts,
                "last_checked_at": ts,
                "risk_note": str(message or "")[:200],
            }
            cooldown_until: Optional[int] = None
            if kind == "login_invalid":
                fields.update(status="need_login", login_state="expired")
                cooldown_until = ts + cooldown_delay(consecutive)
            elif kind == "risk_blocked":
                fields.update(status="risk", risk_level="watch")
                cooldown_until = max(ts + 3600, ts + cooldown_delay(consecutive))
            else:  # network / other：保持 active（可继续同号重试），仅退避
                fields.update(status="active")
                cooldown_until = ts + cooldown_delay(consecutive)
            fields["cooldown_until"] = cooldown_until
            self._update_row(account_id, **fields)

            event_type = ""
            dedupe_key = ""
            if kind == "login_invalid":
                event_type = "account.login_invalid"
                dedupe_key = "account:%s:login_invalid:%s" % (account_id, _today_from_ts(ts))
            elif kind == "risk_blocked":
                event_type = "account.risk_blocked"
                dedupe_key = "account:%s:risk:%s" % (account_id, _today_from_ts(ts))
            if event_type:
                self._publish(
                    event_type,
                    "error",
                    dedupe_key,
                    {
                        "account_id": str(account_id),
                        "account_alias": self._display_alias(row),
                        "platform": str(platform),
                        "status": str(fields.get("status") or ""),
                        "message": str(message or ""),
                        "run_id": str(run_id or ""),
                    },
                )
            return {
                "kind": kind,
                "account": self._to_api(self._row(account_id)),
                "cooldown_until": cooldown_until,
            }

    # ------------------------------------------------------------------
    # 事件（契约 §2.1）
    # ------------------------------------------------------------------
    @staticmethod
    def _publish(event_type: str, severity: str, dedupe_key: str, data: Dict[str, Any]) -> None:
        event = {
            "type": event_type,
            "severity": severity,
            "dedupe_key": dedupe_key,
            "ts": now_ts(),
            "data": dict(data),
        }
        try:
            bus.publish(event)
        except Exception:  # noqa: BLE001 - 通知失败不能影响账号状态
            log.exception("发布账号事件失败：%s", event_type)

    def publish_login_need_manual(
        self,
        login_id: str,
        platform: str,
        account_id: str,
        kind: str = "",
        message: str = "",
    ) -> None:
        """登录需要人工接管（滑块/拼图/短信）：由 LoginManager 在首次/换 kind 时调用。"""
        alias = self.alias_of(account_id)
        self._publish(
            "login.need_manual",
            "warn",
            "login:%s:need_manual" % str(login_id),
            {
                "login_id": str(login_id),
                "platform": str(platform),
                "account_id": str(account_id or ""),
                "account_alias": alias,
                "kind": str(kind or ""),
                "message": str(message or ""),
            },
        )

    # ------------------------------------------------------------------
    # profile 删除 / 旧目录迁移
    # ------------------------------------------------------------------
    def _kill_profile_browsers(self, account: Dict[str, Any]) -> List[int]:
        """按 profile marker 杀掉残留 Edge/Chrome（删目录/迁移前）。"""
        try:
            marker = str(self.profile_path(account))
        except Exception:  # noqa: BLE001
            return []
        try:
            return kill_browser_processes_by_marker(marker)
        except Exception:  # noqa: BLE001 - 查杀失败不阻塞流程
            log.exception("按 profile marker 查杀浏览器失败：%s", marker)
            return []

    def _remove_profile(self, account: Dict[str, Any]) -> None:
        """删除账号 profile 目录（先杀残留浏览器，Windows 下重试）。"""
        path = self.profile_path(account)
        killed = self._kill_profile_browsers(account)
        if killed:
            log.info("账号池：删除 profile 前清理残留浏览器 %s 个 account=%s", len(killed), account.get("account_id"))
            time.sleep(0.3)
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                if path.exists():
                    shutil.rmtree(path, ignore_errors=False)
                last_error = None
                break
            except OSError as exc:
                last_error = exc
                time.sleep(0.3 * (attempt + 1))
        if last_error is not None or path.exists():
            detail = str(last_error) if last_error is not None else "目录仍存在"
            raise ServiceError("DELETE_FAILED", f"删除账号 profile 失败：{detail}", 500)

    def adopt_legacy(self, platform: str = "", account_id: str = "") -> Dict[str, Any]:
        """把旧单账号 profile 迁移到 v2 目录（幂等；仅空闲；rename 优先，失败回滚）。

        - ``account_id`` 指定时迁移该账号（必须属于对应平台）；空则迁移平台默认账号；
        - 旧目录不存在 → 直接把账号切换到 v2 布局（无需搬数据）；
        - 目标目录已存在且非空 → ``PROFILE_LAYOUT_CONFLICT``(409)；
        - 搬移失败 → 回滚 DB、保留旧目录，抛 ``PROFILE_MIGRATE_FAILED``(500)。
        """
        with self._lock:
            if self.guard is not None and self.guard.is_busy():
                raise ServiceError("BUSY", "当前有登录/检测/采集任务进行中，暂不能迁移账号目录", 409)
            account = (
                self._row(account_id) if account_id else self.ensure_legacy_default(platform)
            )
            platform = str(account.get("platform") or platform or "").strip()
            if platform not in PLATFORM_LABELS:
                raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
            if str(account.get("profile_layout") or "v2") == "v2":
                # 已是 v2：幂等返回（若目标目录缺失也不回退，账号仍按 v2 使用）
                return {
                    "ok": True,
                    "migrated": False,
                    "already_v2": True,
                    "account_id": str(account["account_id"]),
                    "platform": platform,
                    "profile_dir": account.get("profile_dir") or "",
                    "message": "账号已是 v2 独立 profile 布局，无需迁移",
                }
            if self._account_busy(str(account["account_id"])):
                raise ServiceError("ACCOUNT_BUSY", "该账号正在使用中，暂不能迁移目录", 409)
            source = self.profile_path(account)
            target_rel = self._profile_rel(platform, str(account["account_id"]), "v2")
            target = self.cfg.data_dir / target_rel
            if target.exists() and any(target.iterdir()):
                raise ServiceError(
                    "PROFILE_LAYOUT_CONFLICT",
                    f"目标 profile 目录已存在且非空，无法迁移：{target}",
                    409,
                )
            ts = now_ts()
            if not source.exists() or not any(source.iterdir()):
                # 无旧数据：直接切布局（首次扫码会创建新目录）
                self._update_row(
                    str(account["account_id"]),
                    profile_dir=target_rel,
                    profile_layout="v2",
                )
                log.info("账号池：旧 profile 不存在，账号 %s 直接切换为 v2 布局", account["account_id"])
                return {
                    "ok": True,
                    "migrated": True,
                    "already_v2": False,
                    "account_id": str(account["account_id"]),
                    "platform": platform,
                    "from": str(source),
                    "to": str(target),
                    "message": "旧 profile 目录不存在，已切换为新布局（首次登录时会创建）",
                }

            killed = self._kill_profile_browsers(account)
            if killed:
                log.info("账号池：迁移前清理残留浏览器 %s 个 account=%s", len(killed), account["account_id"])
                time.sleep(0.3)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ServiceError(
                    "PROFILE_MIGRATE_FAILED",
                    f"迁移账号 profile 失败（无法创建目标父目录：{exc}），旧目录已保留：{source}",
                    500,
                ) from exc
            moved = False
            rename_error: Optional[Exception] = None
            try:
                source.rename(target)
                moved = True
            except OSError as exc:  # 跨盘/权限：回退 copy
                rename_error = exc
                try:
                    if target.exists():
                        shutil.rmtree(target, ignore_errors=True)
                    shutil.copytree(source, target)
                    moved = True
                except Exception as copy_error:  # noqa: BLE001
                    # 回滚：清理半成品目标目录，保留旧目录
                    try:
                        if target.exists():
                            shutil.rmtree(target, ignore_errors=True)
                    except Exception:  # noqa: BLE001
                        pass
                    raise ServiceError(
                        "PROFILE_MIGRATE_FAILED",
                        f"迁移账号 profile 失败（rename: {rename_error}; copy: {copy_error}），"
                        f"旧目录已保留：{source}",
                        500,
                    ) from copy_error
            if not moved:  # pragma: no cover - 理论不可达
                raise ServiceError("PROFILE_MIGRATE_FAILED", "迁移账号 profile 失败", 500)
            # copy 分支：旧目录已复制成功，尽量清理（失败仅告警，不丢数据）
            if rename_error is not None:
                try:
                    shutil.rmtree(source, ignore_errors=True)
                except Exception:  # noqa: BLE001
                    log.warning("迁移后清理旧目录失败（数据已复制到 v2，旧目录保留）：%s", source)
            self._update_row(
                str(account["account_id"]),
                profile_dir=target_rel,
                profile_layout="v2",
                last_checked_at=ts,
            )
            log.info(
                "账号池：账号 %s 旧 profile 迁移完成 %s → %s（rename=%s）",
                account["account_id"],
                source,
                target,
                rename_error is None,
            )
            return {
                "ok": True,
                "migrated": True,
                "already_v2": False,
                "account_id": str(account["account_id"]),
                "platform": platform,
                "from": str(source),
                "to": str(target),
                "message": "旧 profile 已迁移到 v2 独立目录",
            }

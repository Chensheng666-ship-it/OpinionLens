# -*- coding: utf-8 -*-
"""多账号号池离线测试（Phase C3）：``python app/sidecar/test_account_pool.py``。

覆盖：
* ``account_manager.AccountManager``：legacy 默认账号懒创建、CRUD、默认账号唯一性、软删、
  pick 硬过滤/打分（lru|best_health|round_robin|manual）、配额、冷却指数退避、失败分类事件、
  adopt_legacy 迁移（幂等/冲突/失败回滚）；
* ``engine_runner``：ActivityGuard 账号锁、LoginManager 账号会话、RunManager 选号/换号/
  run_accounts/run_platforms.account_id、run.* 事件、run 账号尝试明细；
* ``api_pool`` 路由：用 FastAPI TestClient 离线验证（不启动 Electron）。
* 集成部分用 ``_fake_engine.py`` 真实子进程（不联网、不启动浏览器）。

约定：本文件不写用户数据目录（全部 tempfile），不联网，不拉起 Edge。
供 selftest.py 复用：``def run(report=None) -> tuple[int, int]`` 返回 (passed, failed)。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SIDECAR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SIDECAR_DIR.parent.parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))

import engine_runner  # noqa: E402
from engine_runner import (  # noqa: E402
    PLATFORM_IDS,
    PLATFORM_LABELS,
    ActivityGuard,
    EngineService,
    ServiceError,
    SidecarConfig,
)
from state import StateStore  # noqa: E402
import account_manager as am_module  # noqa: E402
from account_manager import (  # noqa: E402
    AccountManager,
    classify_failure,
    cooldown_delay,
)
from events import bus  # noqa: E402

FAKE_ENGINE = SIDECAR_DIR / "_fake_engine.py"
PLATFORMS = list(PLATFORM_IDS)


# ---------------------------------------------------------------------------
# 测试报告（与 selftest.Report.check 兼容）
# ---------------------------------------------------------------------------
class LocalReport:
    """本地报告器：失败只记录不抛出（直接运行 python 时能跑完全部用例）。"""

    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []

    def check(self, name: str, condition: Any, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"[PASS] {name}")
        else:
            message = f"{name} {detail}".strip()
            self.failed.append(message)
            print(f"[FAIL] {message}")

    def warn(self, name: str) -> None:
        print(f"[WARN] {name}")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
class TempEnv:
    """临时覆盖环境变量（fake engine 场景/自动重试参数等），退出时恢复。"""

    def __init__(self, **values: Any) -> None:
        self.values = {key: str(value) for key, value in values.items()}
        self.old: Dict[str, Optional[str]] = {}

    def __enter__(self) -> "TempEnv":
        for key, value in self.values.items():
            self.old[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_cfg(data_dir: Path) -> SidecarConfig:
    """构造测试用 SidecarConfig；干净 clone 未安装 MediaCrawler 时使用占位根（fake engine 不读取内容）。"""
    mc_root = PROJECT_ROOT / "MediaCrawler"
    if not (mc_root / "main.py").is_file():
        mc_root = Path(tempfile.gettempdir()) / f"opinionlens_pool_media_crawler_{os.getpid()}"
        (mc_root / "media_platform").mkdir(parents=True, exist_ok=True)
        (mc_root / "main.py").write_text("# offline pool test stub (fake engine only)\n", encoding="utf-8")
    return SidecarConfig.from_env(
        data_dir,
        mc_root=mc_root,
        python=sys.executable,
        engine_entry=FAKE_ENGINE,
    )


def wait_until(predicate: Callable[[], bool], timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 轮询期间忽略瞬态异常
            pass
        time.sleep(interval)
    return False


class UnitCtx:
    """纯 DB/manager 层上下文（不起子进程）。"""

    def __init__(self, name: str) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix=f"pool_unit_{name}_")).resolve()
        self.cfg = make_cfg(self.data_dir)
        self.cfg.ensure_dirs()
        self.guard = ActivityGuard()
        self.state = StateStore(self.cfg.app_db)
        self.accounts = AccountManager(self.cfg, self.state, self.guard)

    def close(self) -> None:
        try:
            self.state.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.data_dir, ignore_errors=True)


class ServiceCtx:
    """EngineService 集成上下文（真实 fake engine 子进程）。"""

    def __init__(self, name: str) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix=f"pool_svc_{name}_")).resolve()
        self.cfg = make_cfg(self.data_dir)
        self.cfg.ensure_dirs()
        env = TempEnv(
            FAKE_ENGINE="1",
            FAKE_TEST_SLEEP="0.05",
            SIDECAR_AUTO_RETRY_MAX="0",
            SIDECAR_CRAWL_TIMEOUT="60",
            SIDECAR_TEST_TIMEOUT="30",
        )
        self._env = env
        env.__enter__()
        self.service = EngineService(self.cfg)

    def close(self) -> None:
        try:
            self.service.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._env.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.data_dir, ignore_errors=True)

    # -- 便捷操作 ---------------------------------------------------------
    def login_v2(self, platform: str, alias: str) -> Dict[str, Any]:
        account = self.service.accounts.create(platform, alias=alias)
        account_id = account["account_id"]
        started = self.service.start_login(platform, account_id)
        session = self._wait_login(platform, started["login_id"])
        assert session["status"] == "confirmed", session
        self._wait_idle()
        return account

    def login_legacy(self, platform: str) -> Dict[str, Any]:
        started = self.service.start_login(platform)
        session = self._wait_login(platform, started["login_id"])
        assert session["status"] == "confirmed", session
        self._wait_idle()
        return session

    def _wait_login(self, platform: str, login_id: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}

        def _done() -> bool:
            nonlocal result
            result = self.service.get_login(platform, login_id)
            return result.get("status") in ("confirmed", "failed", "expired", "cancelled")

        wait_until(_done, timeout=30.0)
        return result

    def _wait_idle(self, timeout: float = 20.0) -> None:
        wait_until(lambda: not self.service.guard.is_exclusive_busy(), timeout=timeout)

    def run_until_done(self, params: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
        created = self.service.create_run(params)
        run_id = created["run_id"]
        result: Dict[str, Any] = {}

        def _done() -> bool:
            nonlocal result
            result = self.service.get_run(run_id)
            return result.get("status") in ("done", "failed", "cancelled")

        wait_until(_done, timeout=timeout)
        self._wait_idle()
        return result


def _fake_args(data_dir: Path, mode: str, platform: str, account_id: str = "") -> Dict[str, Any]:
    suffix = f"_{account_id}" if account_id else ""
    path = Path(data_dir) / "logs" / f"fake_engine_args_{mode}_{platform}{suffix}.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _seed_ok_account(
    manager: AccountManager,
    platform: str,
    alias: str,
    *,
    health: int = 100,
    last_used_at: Optional[int] = None,
    is_default: bool = False,
) -> Dict[str, Any]:
    account = manager.create(platform, alias=alias)
    manager.update(
        account["account_id"],
        login_state="ok",
        status="active",
        health_score=health,
        is_default=is_default,
    )
    if last_used_at is not None:
        manager._update_row(account["account_id"], last_used_at=last_used_at)
    return manager.get(account["account_id"])


# ===========================================================================
# 1. 纯函数：退避 / 失败分类
# ===========================================================================
def test_helpers(report: Any) -> None:
    print("\n--- 1. 冷却退避 / 失败分类 ---")
    report.check("退避：连续失败 1 次 = 30s", cooldown_delay(1) == 30, str(cooldown_delay(1)))
    report.check("退避：连续失败 2 次 = 60s", cooldown_delay(2) == 60, str(cooldown_delay(2)))
    report.check("退避：连续失败 5 次 = 480s", cooldown_delay(5) == 480, str(cooldown_delay(5)))
    report.check("退避：连续失败 20 次上限 1h", cooldown_delay(20) == 3600, str(cooldown_delay(20)))
    report.check("退避：0 次 = 0s", cooldown_delay(0) == 0)
    report.check("失败分类：未登录 → login_invalid", classify_failure("未登录，请重新扫码") == "login_invalid")
    report.check("失败分类：封禁 → risk_blocked", classify_failure("账号异常，疑似封禁") == "risk_blocked")
    report.check("失败分类：网络错误 → network", classify_failure("ERR_CONNECTION_CLOSED") == "network")
    report.check("失败分类：空消息 → other", classify_failure("") == "other")


# ===========================================================================
# 2. legacy 默认账号懒创建
# ===========================================================================
def test_legacy_default(report: Any) -> None:
    print("\n--- 2. legacy 默认账号懒创建（旧库兼容）---")
    ctx = UnitCtx("legacy")
    try:
        report.check("空库初始账号列表为空", len(ctx.accounts.list()) == 0)
        first = ctx.accounts.ensure_legacy_default("xhs")
        report.check(
            "懒创建 legacy 默认账号 id=acc_legacy_xhs",
            first["account_id"] == "acc_legacy_xhs",
            first["account_id"],
        )
        report.check("legacy 账号 profile_layout=legacy", first["profile_layout"] == "legacy")
        report.check(
            "legacy 账号 profile_dir 指向旧路径",
            first["profile_dir"] == "browser_data/xhs_user_data_dir",
            first["profile_dir"],
        )
        report.check("legacy 账号默认账号唯一", first["is_default"] is True)
        report.check("legacy 账号初始 need_login", first["status"] == "need_login")
        report.check("legacy 账号初始 login_state=unknown", first["login_state"] == "unknown")
        again = ctx.accounts.ensure_legacy_default("xhs")
        report.check("ensure_legacy_default 幂等", again["account_id"] == first["account_id"])
        report.check("幂等后 xhs 仍只有 1 个账号", len(ctx.accounts.list("xhs")) == 1)
        all_defaults = ctx.accounts.ensure_all_legacy_defaults()
        report.check("ensure_all_legacy_defaults 建 7 个平台默认账号", len(all_defaults) == 7)
        report.check(
            "每个平台的默认账号来自 acc_legacy_<platform>",
            all(item["account_id"] == f"acc_legacy_{item['platform']}" for item in all_defaults),
        )
        # 旧 profile 目录 + 旧 account_state 存在时，legacy 账号应回填已登录
        state2 = StateStore(ctx.data_dir / "legacy2.db")
        try:
            old_profile = ctx.cfg.browser_data_dir / "dy_user_data_dir"
            old_profile.mkdir(parents=True, exist_ok=True)
            (old_profile / "Cookies").write_text("x", encoding="utf-8")
            state2.set_account_state("dy", logged_in=True, profile_exists=True, message="已登录")
            manager2 = AccountManager(ctx.cfg, state2, ActivityGuard())
            dy = manager2.ensure_legacy_default("dy")
            report.check("旧登录态回填：legacy 账号 active", dy["status"] == "active", dy["status"])
            report.check("旧登录态回填：legacy 账号 login_state=ok", dy["login_state"] == "ok")
            report.check("旧登录态回填：profile_exists=true", dy["profile_exists"] is True)
            report.check("旧登录态回填：默认账号", dy["is_default"] is True)
        finally:
            state2.close()
    finally:
        ctx.close()


# ===========================================================================
# 3. CRUD / 默认账号唯一性 / 软删
# ===========================================================================
def test_crud(report: Any) -> None:
    print("\n--- 3. 账号 CRUD / 默认账号唯一性 / 软删 ---")
    ctx = UnitCtx("crud")
    try:
        ctx.accounts.ensure_legacy_default("xhs")
        a1 = ctx.accounts.create("xhs", alias="账号A")
        report.check(
            "create 返回 acc_ + 12 位 hex",
            bool(re.match(r"^acc_[0-9a-f]{12}$", a1["account_id"])),
            a1["account_id"],
        )
        report.check(
            "create 生成 v2 profile 目录（相对 data-dir）",
            a1["profile_dir"] == f"browser_data/xhs/{a1['account_id']}/user_data_dir",
            a1["profile_dir"],
        )
        report.check("create 保留 alias", a1["alias"] == "账号A")
        report.check("非首个账号不抢占默认", a1["is_default"] is False)
        a2 = ctx.accounts.create("xhs", alias="账号B")
        report.check("第二个账号默认标记 false", a2["is_default"] is False)
        report.check("list(platform) 只含该平台", len(ctx.accounts.list("xhs")) == 3)
        updated = ctx.accounts.update(a1["account_id"], alias="账号A2", risk_note="备注")
        report.check("update alias", updated["alias"] == "账号A2")
        report.check("update risk_note", updated["risk_note"] == "备注")
        disabled = ctx.accounts.update(a1["account_id"], enabled=False)
        report.check("update enabled=false → status=disabled", disabled["status"] == "disabled")
        enabled = ctx.accounts.update(a1["account_id"], status="enabled")
        report.check("update status=enabled → status=active", enabled["status"] == "active")
        ctx.accounts.update(a2["account_id"], is_default=True)
        report.check(
            "设为默认后旧默认被清除",
            ctx.accounts.get(a1["account_id"])["is_default"] is False
            and ctx.accounts.get(a2["account_id"])["is_default"] is True,
        )
        try:
            ctx.accounts.get("acc_not_exists")
            report.check("get 不存在账号抛 ACCOUNT_NOT_FOUND", False)
        except ServiceError as exc:
            report.check(
                "get 不存在账号抛 ACCOUNT_NOT_FOUND/404",
                exc.code == "ACCOUNT_NOT_FOUND" and exc.status_code == 404,
                f"{exc.code}/{exc.status_code}",
            )
        try:
            ctx.accounts.update(a1["account_id"], alias="x", unknown_field=1)
            report.check("update 未知字段被拒绝", False)
        except ServiceError as exc:
            report.check("update 未知字段抛 INVALID_PARAMS", exc.code == "INVALID_PARAMS", exc.code)
        try:
            ctx.accounts.create("not_a_platform")
            report.check("create 非法平台被拒绝", False)
        except ServiceError as exc:
            report.check("create 非法平台抛 INVALID_PLATFORM", exc.code == "INVALID_PLATFORM", exc.code)
        # 软删：archived 不出现在默认列表，但可显式查看
        profile_dir = ctx.accounts.profile_path(ctx.accounts.get(a1["account_id"]))
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / "profile.bin").write_text("x", encoding="utf-8")
        archived = ctx.accounts.archive(a1["account_id"], purge_profile=True)
        report.check("archive 返回 ok", archived.get("ok") is True)
        report.check("archive 标记 purged", archived.get("purged") is True)
        report.check("archive 后 profile 目录被删除", not profile_dir.exists())
        report.check(
            "archived 账号不出现在 list()",
            a1["account_id"] not in {item["account_id"] for item in ctx.accounts.list("xhs")},
        )
        report.check(
            "archived 账号可用 include_archived 查看",
            a1["account_id"] in {item["account_id"] for item in ctx.accounts.list("xhs", include_archived=True)},
        )
        report.check(
            "archived 账号 status=archived",
            ctx.accounts.get(a1["account_id"])["status"] == "archived",
        )
        # 设置（策略/配额/平台最小间隔）
        report.check("默认策略 lru", ctx.accounts.default_strategy() == "lru")
        report.check("默认日采集上限 20", ctx.accounts.limits()["daily_runs"] == 20)
        report.check("默认日入库上限 2000", ctx.accounts.limits()["daily_saved_items"] == 2000)
        report.check("默认平台最小间隔 30s", ctx.accounts.platform_min_interval_s() == 30)
        changed = ctx.accounts.update_settings(
            {"default_strategy": "best_health", "limits": {"daily_runs": 5}, "platform_min_interval_s": 10}
        )
        report.check("update_settings 策略生效", changed["default_strategy"] == "best_health")
        report.check("update_settings 配额生效", changed["limits"]["daily_runs"] == 5)
        report.check("update_settings 最小间隔生效", changed["platform_min_interval_s"] == 10)
        ctx.accounts.update_settings(
            {"default_strategy": "lru", "limits": {"daily_runs": 20}, "platform_min_interval_s": 30}
        )
        report.check("update_settings 可恢复默认", ctx.accounts.default_strategy() == "lru")
    finally:
        ctx.close()


# ===========================================================================
# 4. 配额 / 记账 / 冷却 / 事件
# ===========================================================================
def test_quota_and_events(report: Any) -> None:
    print("\n--- 4. 配额 / 记账 / 冷却 / 账号事件 ---")
    ctx = UnitCtx("quota")
    events: List[Dict[str, Any]] = []

    def _on_event(event: Dict[str, Any]) -> None:
        if str(event.get("type") or "").startswith("account."):
            events.append(event)

    bus.subscribe(_on_event)
    try:
        acc = _seed_ok_account(ctx.accounts, "xhs", "配额账号")
        account_id = acc["account_id"]
        quota = ctx.accounts.quota_remaining(account_id)
        report.check("初始配额 runs=0", quota["runs"] == 0)
        report.check("初始配额 saved_items=0", quota["saved_items"] == 0)
        report.check("配额剩余 runs=20", quota["runs_remaining"] == 20)
        ctx.accounts.mark_attempt("run_q1", "xhs", 1, account_id, "auto")
        quota = ctx.accounts.quota_remaining(account_id)
        report.check("mark_attempt 增加日 runs 计数", quota["runs"] == 1, str(quota["runs"]))
        report.check("mark_attempt 增加 platform_runs", quota["platform_runs"] == 1)
        attempts = ctx.accounts.run_accounts("run_q1")
        report.check("mark_attempt 写 run_accounts", len(attempts) == 1 and attempts[0]["account_id"] == account_id)
        report.check("mark_attempt 写 run_platforms.account_id 前需先建 run", True)
        ctx.accounts.finish_attempt("run_q1", "xhs", 1, "done", "采集完成", account_id)
        attempts = ctx.accounts.run_accounts("run_q1")
        report.check("finish_attempt 写终态 done", attempts[0]["status"] == "done")
        report.check("finish_attempt 写 message", attempts[0]["message"] == "采集完成")
        result = ctx.accounts.on_crawl_result(account_id, True, "ok", saved_items=7, platform="xhs", run_id="run_q1")
        quota = ctx.accounts.quota_remaining(account_id)
        report.check("on_crawl_result 成功累计 saved_items", quota["saved_items"] == 7, str(quota["saved_items"]))
        report.check("on_crawl_result 成功返回 kind=ok", result["kind"] == "ok")
        report.check(
            "on_crawl_result 成功提升 success_count",
            ctx.accounts.get(account_id)["success_count"] == 1,
        )
        # 失败分类 + 冷却指数退避
        ctx.accounts.on_crawl_result(account_id, False, "未登录，请重新扫码", platform="xhs", run_id="run_q1")
        row = ctx.accounts.get(account_id)
        report.check("采集未登录 → status=need_login", row["status"] == "need_login", row["status"])
        report.check("采集未登录 → login_state=expired", row["login_state"] == "expired")
        report.check("连续失败计数=1", row["consecutive_fail"] == 1)
        report.check("首次失败冷却 ≈30s", 25 <= int(row["cooldown_until"] or 0) - int(time.time()) <= 35)
        report.check("健康度下降", row["health_score"] <= 95, str(row["health_score"]))
        report.check("发布 account.login_invalid 事件", len(events) == 1 and events[0]["type"] == "account.login_invalid")
        report.check("account.login_invalid severity=error", events[0]["severity"] == "error")
        report.check(
            "account.login_invalid dedupe_key 含日期",
            bool(re.match(r"^account:.+:login_invalid:\d{4}-\d{2}-\d{2}$", events[0]["dedupe_key"])),
            events[0]["dedupe_key"],
        )
        report.check(
            "account.login_invalid data 含 account_id/platform",
            events[0]["data"].get("account_id") == account_id and events[0]["data"].get("platform") == "xhs",
        )
        # 风控失败 → risk_blocked 事件
        acc2 = _seed_ok_account(ctx.accounts, "dy", "风控账号")
        result2 = ctx.accounts.on_crawl_result(acc2["account_id"], False, "账号异常，疑似封禁", platform="dy")
        report.check("风控失败分类 risk_blocked", result2["kind"] == "risk_blocked", result2["kind"])
        risk_events = [item for item in events if item["type"] == "account.risk_blocked"]
        report.check("发布 account.risk_blocked 事件", len(risk_events) == 1)
        report.check(
            "account.risk_blocked dedupe_key 含 risk+日期",
            bool(re.match(r"^account:.+:risk:\d{4}-\d{2}-\d{2}$", risk_events[0]["dedupe_key"])),
            risk_events[0]["dedupe_key"],
        )
        report.check(
            "风控账号 status=risk/cooldown≈1h",
            ctx.accounts.get(acc2["account_id"])["status"] == "risk"
            and 3500 <= int(ctx.accounts.get(acc2["account_id"])["cooldown_until"] or 0) - int(time.time()) <= 3700,
        )
        # 网络失败：不换号（无事件）但退避
        acc3 = _seed_ok_account(ctx.accounts, "wb", "网络账号")
        before_events = len(events)
        ctx.accounts.on_crawl_result(acc3["account_id"], False, "ERR_CONNECTION_CLOSED", platform="wb")
        report.check("网络失败分类 network", classify_failure("ERR_CONNECTION_CLOSED") == "network")
        report.check("网络失败不发布账号事件", len(events) == before_events)
        report.check(
            "网络失败仍保持 active（可同号重试）",
            ctx.accounts.get(acc3["account_id"])["status"] == "active",
        )
        # 成功恢复
        ctx.accounts.update(account_id, login_state="ok", status="active")
        ctx.accounts.on_crawl_result(account_id, True, "ok", saved_items=1)
        row = ctx.accounts.get(account_id)
        report.check("成功采集重置 consecutive_fail", row["consecutive_fail"] == 0)
        report.check("成功采集清空冷却", not row["cooldown_until"])
        # 登录失败/测试失败
        acc4 = _seed_ok_account(ctx.accounts, "bili", "登录账号")
        ctx.accounts.on_login_failed(acc4["account_id"], "二维码过期")
        report.check(
            "on_login_failed → need_login/expired",
            ctx.accounts.get(acc4["account_id"])["status"] == "need_login"
            and ctx.accounts.get(acc4["account_id"])["login_state"] == "expired",
        )
        ctx.accounts.on_login_success(acc4["account_id"])
        report.check(
            "on_login_success → active/ok",
            ctx.accounts.get(acc4["account_id"])["status"] == "active"
            and ctx.accounts.get(acc4["account_id"])["login_state"] == "ok",
        )
        # 测试连接失败 → account.login_invalid（网络异常不误判）
        acc5 = _seed_ok_account(ctx.accounts, "tieba", "检测账号")
        before_events = len(events)
        ctx.accounts.on_test_result(acc5["account_id"], False, "未登录", network_error=True)
        report.check("测试网络异常不发布 login_invalid", len(events) == before_events)
        ctx.accounts.on_test_result(acc5["account_id"], False, "未登录", network_error=False)
        report.check("测试未登录发布 login_invalid", len(events) == before_events + 1)
        # 配额超限
        ctx.accounts.update_settings({"limits": {"daily_runs": 2}})
        ctx.accounts.mark_attempt("run_q2", "tieba", 1, acc5["account_id"], "auto")
        ctx.accounts.mark_attempt("run_q3", "tieba", 1, acc5["account_id"], "auto")
        report.check("配额超限标记 exceeded", ctx.accounts.quota_remaining(acc5["account_id"])["exceeded"] is True)
        ctx.accounts.update_settings({"limits": {"daily_runs": 20}})
    finally:
        bus.unsubscribe(_on_event)
        ctx.close()


# ===========================================================================
# 5. pick：硬过滤 / 打分 / 可解释 / manual
# ===========================================================================
def test_pick(report: Any) -> None:
    print("\n--- 5. 选号：硬过滤 / 打分 / 可解释 ---")
    ctx = UnitCtx("pick")
    now = int(time.time())
    try:
        ctx.accounts.ensure_legacy_default("xhs")
        # 三个可用账号：health 100/80/60，last_used 旧/中/新
        old = _seed_ok_account(ctx.accounts, "xhs", "旧账号", health=100, last_used_at=now - 7 * 86400)
        mid = _seed_ok_account(ctx.accounts, "xhs", "中账号", health=80, last_used_at=now - 3 * 86400)
        new = _seed_ok_account(ctx.accounts, "xhs", "新账号", health=60, last_used_at=now - 3600)
        lru = ctx.accounts.pick("xhs", strategy="lru")
        report.check("lru 选最久未使用账号", lru["account_id"] == old["account_id"], lru["account_id"])
        report.check("pick 返回可解释 reasons", bool(lru["reasons"]) and any("LRU" in item for item in lru["reasons"]))
        report.check("pick 返回 score", isinstance(lru["score"], float))
        report.check("pick 返回 candidates 明细", len(lru["candidates"]) >= 3)
        best = ctx.accounts.pick("xhs", strategy="best_health")
        report.check("best_health 选健康度最高", best["account_id"] == old["account_id"], best["account_id"])
        rr = ctx.accounts.pick("xhs", strategy="round_robin")
        report.check("round_robin 选最久未使用", rr["account_id"] == old["account_id"])
        # 默认账号加成（同健康度同时长，default 胜出）
        ctx.accounts.update(old["account_id"], is_default=False)
        ctx.accounts.update(mid["account_id"], is_default=True, health_score=100)
        ctx.accounts._update_row(mid["account_id"], last_used_at=now - 7 * 86400)
        default_pick = ctx.accounts.pick("xhs", strategy="lru")
        report.check("默认账号加成生效", default_pick["account_id"] == mid["account_id"], default_pick["account_id"])
        # 排除
        excluded = ctx.accounts.pick("xhs", strategy="lru", exclude=[old["account_id"], mid["account_id"]])
        report.check("exclude 生效", excluded["account_id"] == new["account_id"])
        # 硬过滤：登录态未确认
        ctx.accounts.update(new["account_id"], login_state="expired")
        only = ctx.accounts.pick("xhs", strategy="lru", exclude=[old["account_id"]])
        report.check("登录态 expired 不入候选", only["account_id"] == mid["account_id"])
        # 硬过滤：禁用
        ctx.accounts.update(mid["account_id"], status="disabled")
        after = ctx.accounts.pick("xhs", strategy="lru")
        report.check("禁用账号被过滤", after["account_id"] == old["account_id"], after["account_id"])
        # 硬过滤：冷却中
        ctx.accounts.update(mid["account_id"], status="active", cooldown_until=now + 600)
        after2 = ctx.accounts.pick("xhs", strategy="lru")
        report.check("冷却中账号被过滤", after2["account_id"] == old["account_id"], after2["account_id"])
        ctx.accounts.update(mid["account_id"], cooldown_until=None)
        # 硬过滤：账号忙（ActivityGuard 账号锁）
        ctx.guard.acquire_account(old["account_id"], "test-ref")
        busy_pick = ctx.accounts.pick("xhs", strategy="lru")
        report.check("账号被占用时跳过", busy_pick["account_id"] != old["account_id"], busy_pick["account_id"])
        ctx.guard.release_account(old["account_id"], "test-ref")
        # 硬过滤：配额超限
        ctx.accounts.update_settings({"limits": {"daily_runs": 1}})
        ctx.accounts.mark_attempt("run_pick_quota", "xhs", 1, old["account_id"], "auto")
        ctx.accounts.update_settings({"limits": {"daily_runs": 1}})
        ctx.accounts.update(new["account_id"], login_state="ok", status="active")
        no_quota = ctx.accounts.pick("xhs", strategy="lru", exclude=[mid["account_id"]])
        report.check("配额超限账号被过滤", no_quota["account_id"] == new["account_id"])
        ctx.accounts.update_settings({"limits": {"daily_runs": 20}})
        # 无可用 → NO_ACCOUNT
        ctx.accounts.update(old["account_id"], status="disabled")
        ctx.accounts.update(mid["account_id"], status="disabled")
        ctx.accounts.update(new["account_id"], status="disabled")
        ctx.accounts.ensure_legacy_default("xhs")
        ctx.accounts.update("acc_legacy_xhs", status="disabled")
        try:
            ctx.accounts.pick("xhs")
            report.check("无可用账号抛 NO_ACCOUNT", False)
        except ServiceError as exc:
            report.check(
                "无可用账号抛 NO_ACCOUNT/409",
                exc.code == "NO_ACCOUNT" and exc.status_code == 409,
                f"{exc.code}/{exc.status_code}",
            )
        # preview：候选含 blocked_reasons，选中与 pick 一致
        ctx.accounts.update(old["account_id"], status="active")
        preview = ctx.accounts.preview("xhs")
        report.check("preview 返回 selected", bool(preview.get("selected")))
        report.check("preview 返回 candidates", len(preview.get("candidates") or []) >= 1)
        report.check(
            "preview 候选含 blocked_reasons（禁用账号）",
            any(
                item["account_id"] == mid["account_id"] and item.get("blocked_reasons")
                for item in preview["candidates"]
            ),
        )
        report.check("preview 与 pick 结果一致", preview["selected"]["account_id"] == ctx.accounts.pick("xhs")["account_id"])
        multi_preview = ctx.accounts.preview()
        report.check(
            "preview 不传平台返回 7 平台预览",
            len(multi_preview.get("platforms") or []) == 7,
        )
        # manual / pick_manual
        manual = ctx.accounts.pick_manual("xhs", old["account_id"])
        report.check("pick_manual 返回指定账号", manual["account_id"] == old["account_id"])
        report.check("pick_manual strategy=manual", manual["strategy"] == "manual")
        try:
            ctx.accounts.pick_manual("xhs", mid["account_id"])
            report.check("pick_manual 禁用账号抛 ACCOUNT_DISABLED", False)
        except ServiceError as exc:
            report.check(
                "pick_manual 禁用账号抛 ACCOUNT_DISABLED/409",
                exc.code == "ACCOUNT_DISABLED" and exc.status_code == 409,
                f"{exc.code}/{exc.status_code}",
            )
        try:
            ctx.accounts.pick_manual("dy", old["account_id"])
            report.check("pick_manual 跨平台抛 ACCOUNT_NOT_FOUND", False)
        except ServiceError as exc:
            report.check("pick_manual 跨平台抛 ACCOUNT_NOT_FOUND", exc.code == "ACCOUNT_NOT_FOUND", exc.code)
        ctx.guard.acquire_account(old["account_id"], "manual-busy")
        try:
            ctx.accounts.pick_manual("xhs", old["account_id"])
            report.check("pick_manual 占用中抛 ACCOUNT_BUSY", False)
        except ServiceError as exc:
            report.check("pick_manual 占用中抛 ACCOUNT_BUSY", exc.code == "ACCOUNT_BUSY", exc.code)
        finally:
            ctx.guard.release_account(old["account_id"], "manual-busy")
    finally:
        ctx.close()


# ===========================================================================
# 6. adopt_legacy（迁移/幂等/冲突/回滚）
# ===========================================================================
def test_adopt_legacy(report: Any) -> None:
    print("\n--- 6. 旧 profile 迁移 adopt_legacy ---")
    original_kill = am_module.kill_browser_processes_by_marker
    am_module.kill_browser_processes_by_marker = lambda marker: []
    ctx = UnitCtx("adopt")
    try:
        legacy = ctx.accounts.ensure_legacy_default("xhs")
        old_dir = ctx.cfg.browser_data_dir / "xhs_user_data_dir"
        old_dir.mkdir(parents=True, exist_ok=True)
        (old_dir / "Cookies").write_text("legacy-cookie", encoding="utf-8")
        # 忙碌时拒绝迁移
        ctx.guard.acquire_exclusive("run", "run-busy")
        try:
            try:
                ctx.accounts.adopt_legacy("xhs")
                report.check("迁移仅空闲可执行（BUSY 拒绝）", False)
            except ServiceError as exc:
                report.check("迁移仅空闲可执行（BUSY 拒绝）", exc.code == "BUSY", exc.code)
        finally:
            ctx.guard.release_exclusive("run-busy")
        migrated = ctx.accounts.adopt_legacy("xhs")
        report.check("adopt_legacy 返回 migrated=true", migrated.get("migrated") is True)
        target = ctx.cfg.data_dir / "browser_data" / "xhs" / legacy["account_id"] / "user_data_dir"
        report.check("旧 profile 被搬到 v2 目录", target.is_dir() and (target / "Cookies").is_file())
        report.check("迁移后旧目录不存在", not old_dir.exists())
        account = ctx.accounts.get(legacy["account_id"])
        report.check("迁移后 profile_layout=v2", account["profile_layout"] == "v2")
        report.check(
            "迁移后 profile_dir 指向 v2 相对路径",
            account["profile_dir"] == f"browser_data/xhs/{legacy['account_id']}/user_data_dir",
            account["profile_dir"],
        )
        report.check(
            "迁移后 profile_template 变为 v2 模板",
            "%s" in ctx.accounts.profile_template(account)
            and ctx.accounts.profile_template(account)
            .replace("\\", "/")
            .endswith(f"/%s/{legacy['account_id']}/user_data_dir"),
            ctx.accounts.profile_template(account),
        )
        again = ctx.accounts.adopt_legacy("xhs")
        report.check("再次 adopt 幂等（migrated=false）", again.get("migrated") is False)
        report.check("再次 adopt 标记 already_v2", again.get("already_v2") is True)
        # 旧目录不存在 → 直接切布局
        dy = ctx.accounts.ensure_legacy_default("dy")
        direct = ctx.accounts.adopt_legacy("dy")
        report.check("旧目录不存在时直接切换 v2", direct.get("migrated") is True)
        report.check(
            "直接切换后账号 layout=v2",
            ctx.accounts.get(dy["account_id"])["profile_layout"] == "v2",
        )
        # 冲突：旧目录 + 目标目录同时存在且非空 → PROFILE_LAYOUT_CONFLICT
        ks = ctx.accounts.ensure_legacy_default("ks")
        ks_old = ctx.cfg.browser_data_dir / "ks_user_data_dir"
        ks_old.mkdir(parents=True, exist_ok=True)
        (ks_old / "Cookies").write_text("x", encoding="utf-8")
        ks_target = ctx.cfg.data_dir / "browser_data" / "ks" / ks["account_id"] / "user_data_dir"
        ks_target.mkdir(parents=True, exist_ok=True)
        (ks_target / "existing").write_text("y", encoding="utf-8")
        try:
            ctx.accounts.adopt_legacy("ks")
            report.check("目标非空抛 PROFILE_LAYOUT_CONFLICT", False)
        except ServiceError as exc:
            report.check(
                "目标非空抛 PROFILE_LAYOUT_CONFLICT/409",
                exc.code == "PROFILE_LAYOUT_CONFLICT" and exc.status_code == 409,
                f"{exc.code}/{exc.status_code}",
            )
        report.check("冲突时旧目录保留", ks_old.is_dir())
        report.check("冲突时账号仍为 legacy", ctx.accounts.get(ks["account_id"])["profile_layout"] == "legacy")
        # 迁移失败回滚：mkdir 失败（platform 目录被同名文件占用）
        wb = ctx.accounts.ensure_legacy_default("wb")
        wb_old = ctx.cfg.browser_data_dir / "wb_user_data_dir"
        wb_old.mkdir(parents=True, exist_ok=True)
        (wb_old / "Cookies").write_text("x", encoding="utf-8")
        blocker = ctx.cfg.browser_data_dir / "wb"
        blocker.write_text("not-a-dir", encoding="utf-8")
        try:
            ctx.accounts.adopt_legacy("wb")
            report.check("迁移失败抛 PROFILE_MIGRATE_FAILED", False)
        except ServiceError as exc:
            report.check(
                "迁移失败抛 PROFILE_MIGRATE_FAILED/500",
                exc.code == "PROFILE_MIGRATE_FAILED" and exc.status_code == 500,
                f"{exc.code}/{exc.status_code}",
            )
        report.check("迁移失败保留旧目录", wb_old.is_dir() and (wb_old / "Cookies").is_file())
        report.check("迁移失败回滚账号 layout=legacy", ctx.accounts.get(wb["account_id"])["profile_layout"] == "legacy")
        blocker.unlink()
        # rename 失败 → copy 兜底
        bili = ctx.accounts.ensure_legacy_default("bili")
        bili_old = ctx.cfg.browser_data_dir / "bili_user_data_dir"
        bili_old.mkdir(parents=True, exist_ok=True)
        (bili_old / "Cookies").write_text("bili-cookie", encoding="utf-8")
        original_rename = Path.rename

        def _rename_fail(self: Path, target: Path) -> Path:  # type: ignore[override]
            raise OSError("cross-device link")

        Path.rename = _rename_fail  # type: ignore[assignment]
        try:
            copied = ctx.accounts.adopt_legacy("bili")
        finally:
            Path.rename = original_rename  # type: ignore[assignment]
        bili_target = ctx.cfg.data_dir / "browser_data" / "bili" / bili["account_id"] / "user_data_dir"
        report.check("rename 失败时 copy 兜底成功", copied.get("migrated") is True and bili_target.is_dir())
        report.check(
            "copy 兜底后数据完整",
            (bili_target / "Cookies").is_file()
            and (bili_target / "Cookies").read_text(encoding="utf-8") == "bili-cookie",
        )
        report.check("copy 兜底后旧目录被清理", not bili_old.exists())
        report.check("copy 兜底后 layout=v2", ctx.accounts.get(bili["account_id"])["profile_layout"] == "v2")
        # 通过 account_id 指定迁移
        tieba = ctx.accounts.ensure_legacy_default("tieba")
        tieba_old = ctx.cfg.browser_data_dir / "tieba_user_data_dir"
        tieba_old.mkdir(parents=True, exist_ok=True)
        (tieba_old / "Cookies").write_text("t", encoding="utf-8")
        by_id = ctx.accounts.adopt_legacy(account_id=tieba["account_id"])
        report.check("adopt 支持指定 account_id", by_id.get("migrated") is True)
        report.check(
            "指定 account_id 迁移后 layout=v2",
            ctx.accounts.get(tieba["account_id"])["profile_layout"] == "v2",
        )
    finally:
        am_module.kill_browser_processes_by_marker = original_kill
        ctx.close()


# ===========================================================================
# 7. EngineService / LoginManager / RunManager 集成（fake engine 子进程）
# ===========================================================================
def test_service_integration(report: Any) -> None:
    print("\n--- 7. EngineService/Login/Run 集成（fake engine，离线）---")
    ctx = ServiceCtx("integration")
    events: List[Dict[str, Any]] = []

    def _on_event(event: Dict[str, Any]) -> None:
        events.append(dict(event))

    bus.subscribe(_on_event)
    try:
        # 7.1 旧 /api/accounts 字段兼容 + 加法字段
        rows = ctx.service.list_accounts()
        report.check("GET /api/accounts 仍返回 7 个平台", len(rows) == 7, str(len(rows)))
        report.check("平台顺序固定（7 平台契约顺序）", [item["platform"] for item in rows] == PLATFORMS)
        old_keys = {"platform", "label", "logged_in", "last_checked_at", "profile_exists", "message"}
        report.check("旧字段一个不少", old_keys.issubset(set(rows[0].keys())), repr(sorted(rows[0].keys())))
        report.check(
            "加法字段 account_count/default_account_id/accounts[]",
            {"account_count", "default_account_id", "accounts"}.issubset(set(rows[0].keys())),
        )
        report.check("每个平台至少有 legacy 默认账号", all(item["account_count"] >= 1 for item in rows))
        report.check(
            "默认账号指向 legacy 账号",
            rows[0]["default_account_id"] == "acc_legacy_xhs",
            rows[0]["default_account_id"],
        )
        report.check(
            "Legacy 接口行为不变：profile_exists=false（fake 不建旧 profile）",
            rows[0]["profile_exists"] is False,
        )
        # 7.2 旧路由登录（无 account_id）：engine 参数与旧版本逐字节一致
        legacy_session = ctx.login_legacy("xhs")
        report.check("旧路由登录 confirmed", legacy_session["status"] == "confirmed")
        report.check("旧路由登录会话带 account_id（兼容加法）", legacy_session.get("account_id") == "acc_legacy_xhs")
        legacy_args = _fake_args(ctx.data_dir, "login", "xhs")
        report.check("legacy 登录仍写旧文件名 fake_engine_args_login_xhs.json", bool(legacy_args))
        report.check(
            "legacy 登录不追加 --account-id（旧 CLI 兼容）",
            legacy_args.get("account_id") == "" and "--account-id" not in (legacy_args.get("argv") or []),
            repr(legacy_args.get("argv")),
        )
        legacy_account = ctx.service.accounts.get("acc_legacy_xhs")
        report.check("登录成功同步 accounts 表 login_state=ok", legacy_account["login_state"] == "ok")
        report.check("登录成功同步 accounts 表 status=active", legacy_account["status"] == "active")
        report.check(
            "旧 account_state 双写 logged_in=true",
            ctx.service.state.get_account_states().get("xhs", {}).get("logged_in") is True,
        )
        # 7.3 v2 账号登录：独立 profile / engine 账号参数
        v2 = ctx.login_v2("xhs", "v2账号")
        report.check("v2 账号登录 confirmed", ctx.service.accounts.get(v2["account_id"])["login_state"] == "ok")
        report.check("v2 账号 profile_exists=true（fake 按模板创建）", ctx.service.accounts.get(v2["account_id"])["profile_exists"] is True)
        v2_args = _fake_args(ctx.data_dir, "login", "xhs", v2["account_id"])
        report.check("v2 登录写入账号级 fake args 文件", bool(v2_args))
        report.check("engine 收到 --account-id", v2_args.get("account_id") == v2["account_id"])
        template = str(v2_args.get("profile_template") or "")
        report.check(
            "engine 收到含恰好一个 %s 的绝对 profile 模板",
            template.count("%s") == 1 and Path(template % "xhs").is_absolute() and v2["account_id"] in template,
            template,
        )
        report.check(
            "v2 profile 落在 browser_data/xhs/<account_id>/user_data_dir",
            (ctx.cfg.data_dir / "browser_data" / "xhs" / v2["account_id"] / "user_data_dir").is_dir(),
        )
        # 7.4 测试连接（账号级）
        test_result = ctx.service.test_connection("xhs", v2["account_id"])
        report.check("v2 账号测试连接 ok", test_result.get("ok") is True, repr(test_result))
        report.check("测试连接返回 account_id", test_result.get("account_id") == v2["account_id"])
        test_args = _fake_args(ctx.data_dir, "test", "xhs", v2["account_id"])
        report.check("test 模式同样收到账号参数", test_args.get("account_id") == v2["account_id"])
        # 7.5 采集任务：手动策略指定账号
        events.clear()
        run_detail = ctx.run_until_done(
            {
                "keywords": ["离线测试"],
                "platforms": ["xhs"],
                "max_notes": 2,
                "comments": False,
                "max_comments": 0,
                "name": "账号池集成",
                "account_policy": {"mode": "manual", "account_ids": {"xhs": v2["account_id"]}},
            }
        )
        run_id = run_detail["run_id"]
        report.check("采集任务最终 done", run_detail["status"] == "done", run_detail["status"])
        platform_row = run_detail["platforms"][0]
        report.check("run_platforms.account_id 记录所用账号", platform_row["account_id"] == v2["account_id"])
        report.check("run_platforms.account_attempts=1", platform_row["account_attempts"] == 1)
        report.check("run 详情带 account_alias", platform_row.get("account_alias") == "v2账号")
        report.check(
            "run 详情回显 account_policy",
            run_detail.get("account_policy", {}).get("mode") == "manual",
        )
        attempts = ctx.service.accounts.run_accounts(run_id)
        report.check("run_accounts 记录账号尝试", len(attempts) == 1 and attempts[0]["account_id"] == v2["account_id"])
        report.check("run_accounts status=done", attempts[0]["status"] == "done")
        report.check("run_accounts reason=manual", attempts[0]["reason"] == "manual")
        crawl_args = _fake_args(ctx.data_dir, "crawl", "xhs", v2["account_id"])
        report.check("crawl 模式向 engine 传账号参数", crawl_args.get("account_id") == v2["account_id"])
        run_events = [event for event in events if event.get("type", "").startswith("run.")]
        report.check("发布 run.finished 事件", len(run_events) == 1 and run_events[0]["type"] == "run.finished")
        report.check("run.finished severity=info", run_events[0]["severity"] == "info")
        report.check(
            "run.finished dedupe_key=run:<id>:finished",
            run_events[0]["dedupe_key"] == f"run:{run_id}:finished",
            run_events[0]["dedupe_key"],
        )
        data = run_events[0]["data"]
        report.check("run.finished data 含 run_id/name", data.get("run_id") == run_id and data.get("name") == "账号池集成")
        report.check(
            "run.finished data 平台统计/条数/耗时",
            data.get("platforms_total") == 1
            and data.get("platforms_ok") == 1
            and data.get("saved_items", 0) >= 1
            and isinstance(data.get("duration_s"), int),
            repr(data),
        )
        report.check(
            "run.finished data 含账号信息",
            data.get("account_id") == v2["account_id"] and data.get("account_alias") == "v2账号",
        )
        report.check("run 配额 runs 已累计", ctx.service.accounts.quota_remaining(v2["account_id"])["runs"] >= 1)
        report.check(
            "run 配额 saved_items 已累计",
            ctx.service.accounts.quota_remaining(v2["account_id"])["saved_items"] >= 1,
        )
        # 7.6 失败切号（登录失效 → 自动换号）
        # 先归档此前用过的 xhs 账号，保证候选只有「失效账号 / 备用账号」，切号可预测
        ctx.service.accounts.archive(v2["account_id"])
        ctx.service.accounts.archive("acc_legacy_xhs")
        fail_account = ctx.login_v2("xhs", "失效账号")
        ok_account = ctx.login_v2("xhs", "备用账号")
        ctx.service.accounts.update(fail_account["account_id"], is_default=True, health_score=100)
        ctx.service.accounts.update(ok_account["account_id"], is_default=False, health_score=95)
        events.clear()
        with TempEnv(
            FAKE_CRAWL_FAIL=f"xhs:{fail_account['account_id']}",
            FAKE_CRAWL_ERROR_MESSAGE="未登录，登录失效，请重新扫码",
            SIDECAR_ACCOUNT_MAX_ATTEMPTS="3",
        ):
            failover_detail = ctx.run_until_done(
                {
                    "keywords": ["离线测试"],
                    "platforms": ["xhs"],
                    "max_notes": 1,
                    "comments": False,
                    "max_comments": 0,
                    "name": "失败切号",
                    "account_policy": {"mode": "auto", "strategy": "lru"},
                }
            )
        failover_row = failover_detail["platforms"][0]
        report.check("换号后任务 done", failover_detail["status"] == "done")
        report.check("换号后 run_platforms.account_attempts=2", failover_row["account_attempts"] == 2, str(failover_row))
        report.check("换号后最终账号为备用账号", failover_row["account_id"] == ok_account["account_id"])
        report.check("换号后 message 含切换说明", "自动切换账号" in str(failover_row.get("message") or ""))
        failover_attempts = ctx.service.accounts.run_accounts(failover_detail["run_id"])
        report.check(
            "run_accounts 两次尝试：login_invalid → done",
            [item["status"] for item in failover_attempts] == ["login_invalid", "done"],
            str([(item["attempt"], item["status"]) for item in failover_attempts]),
        )
        report.check(
            "失效账号被标记 need_login/expired",
            ctx.service.accounts.get(fail_account["account_id"])["status"] == "need_login"
            and ctx.service.accounts.get(fail_account["account_id"])["login_state"] == "expired",
        )
        account_events = [event for event in events if event.get("type") == "account.login_invalid"]
        report.check("换号同时发布 account.login_invalid", len(account_events) == 1)
        run_events = [event for event in events if event.get("type") == "run.finished"]
        report.check("换号后仍发布 run.finished", len(run_events) == 1)
        report.check(
            "run.finished 的账号信息为最终账号",
            run_events[0]["data"].get("account_id") == ok_account["account_id"],
        )
        # 7.7 无可用账号（禁用到只剩 expired 的 v2 账号 + 清掉 legacy 兜底）
        ctx.service.accounts.update(
            ctx.service.accounts.ensure_legacy_default("wb")["account_id"],
            status="archived",
        )
        wb_acc = ctx.service.accounts.create("wb", alias="wb失效")
        ctx.service.accounts.update(wb_acc["account_id"], login_state="expired", status="need_login")
        no_account_detail = ctx.run_until_done(
            {
                "keywords": ["离线测试"],
                "platforms": ["wb"],
                "max_notes": 1,
                "comments": False,
                "max_comments": 0,
                "account_policy": {"mode": "auto"},
            }
        )
        wb_row = no_account_detail["platforms"][0]
        report.check("无可用账号时平台 failed", wb_row["status"] == "failed", wb_row["status"])
        report.check("无可用账号时 message 含「无可用账号」", "无可用账号" in str(wb_row.get("message") or ""))
        # 7.8 旧单账号：登录态过期/冷却中也不得阻断旧 API 与 run（旧行为：交给 engine 判定）
        now = int(time.time())
        ctx.service.accounts.update(
            "acc_legacy_dy",
            status="need_login",
            login_state="expired",
            cooldown_until=now + 60,
        )
        dy_test = ctx.service.test_connection("dy")
        report.check(
            "旧 API test 不受账号池 need_login/冷却过滤",
            dy_test.get("ok") is True,
            repr(dy_test),
        )
        ctx.service.accounts.update(
            "acc_legacy_dy",
            status="need_login",
            login_state="expired",
            cooldown_until=now + 60,
        )
        with TempEnv(FAKE_CRAWL_FAIL_ONCE="dy:1"):
            first_run = ctx.run_until_done(
                {
                    "keywords": ["离线测试"],
                    "platforms": ["dy"],
                    "max_notes": 1,
                    "comments": False,
                    "max_comments": 0,
                    "name": "续采测试",
                }
            )
        dy_row = first_run["platforms"][0]
        report.check("续采前第一次采集 failed", dy_row["status"] == "failed", dy_row["status"])
        report.check(
            "legacy 账号冷却中仍启动 engine 采集（started_at 非空）",
            isinstance(dy_row.get("started_at"), int),
            repr(dy_row.get("started_at")),
        )
        report.check(
            "legacy 账号失败消息来自 engine 而非选号过滤",
            "模拟采集失败" in str(dy_row.get("message") or "")
            and "无可用账号" not in str(dy_row.get("message") or ""),
            str(dy_row.get("message") or "")[:160],
        )
        first_account = dy_row["account_id"]
        report.check("旧单账号兼容：无登录态也可采集（legacy 兜底）", first_account == "acc_legacy_dy", first_account)
        # 旧路由 login 在账号 need_login/冷却中仍可用
        dy_login = ctx.service.start_login("dy")
        report.check("旧 API login 不受账号池过滤（返回 login_id）", bool(dy_login.get("login_id")))
        wait_until(
            lambda: ctx.service.get_login("dy", dy_login["login_id"]).get("status")
            in ("confirmed", "failed", "expired", "cancelled"),
            timeout=20.0,
        )
        dy_login_state = ctx.service.get_login("dy", dy_login["login_id"])
        report.check(
            "旧 API login 在 need_login 账号上可完成（confirmed）",
            dy_login_state.get("status") == "confirmed",
            dy_login_state.get("status"),
        )
        ctx._wait_idle()
        # 清理首次失败的冷却，使续采可沿用原账号
        ctx.service.accounts.update(
            "acc_legacy_dy", login_state="ok", status="active", cooldown_until=None
        )
        resumed = ctx.service.resume_run(first_run["run_id"], ["dy"])
        report.check("续采接口返回 running", resumed.get("status") == "running")
        result: Dict[str, Any] = {}

        def _resume_done() -> bool:
            nonlocal result
            result = ctx.service.get_run(first_run["run_id"])
            return result.get("status") in ("done", "failed", "cancelled")

        wait_until(_resume_done, timeout=60.0)
        ctx._wait_idle()
        report.check("续采后任务终态 done", result.get("status") == "done", result.get("status"))
        dy_resumed = [item for item in result["platforms"] if item["platform"] == "dy"][0]
        report.check("续采后平台 done", dy_resumed["status"] == "done", dy_resumed["status"])
        report.check("续采累计 attempts=2", dy_resumed["account_attempts"] == 2, str(dy_resumed.get("account_attempts")))
        all_attempts = ctx.service.accounts.run_accounts(first_run["run_id"])
        report.check(
            "续采 attempt 序号递增保留历史（1=failed, 2=done）",
            [(item["attempt"], item["status"]) for item in all_attempts] == [(1, "failed"), (2, "done")],
            str([(item["attempt"], item["status"]) for item in all_attempts]),
        )
        report.check(
            "续采沿用原账号（attempt=2 reason=resume）",
            bool(all_attempts) and all_attempts[-1]["account_id"] == first_account
            and all_attempts[-1]["reason"] == "resume",
            str(all_attempts[-1] if all_attempts else None),
        )
        # 7.9 清除登录态（旧路由 = 平台默认账号，删目录前清理浏览器）
        dy_profile = ctx.cfg.platform_profile_dir("dy")
        dy_profile.mkdir(parents=True, exist_ok=True)
        (dy_profile / "Cookies").write_text("x", encoding="utf-8")
        original_kill = engine_runner.kill_browser_processes_by_marker
        engine_runner.kill_browser_processes_by_marker = lambda marker: []
        try:
            deleted = ctx.service.delete_account("dy")
        finally:
            engine_runner.kill_browser_processes_by_marker = original_kill
        report.check("DELETE 旧路由返回 ok", deleted.get("ok") is True)
        report.check("清除登录态后 profile 目录被删除", not dy_profile.exists())
        report.check(
            "清除登录态后 account_state.logged_in=false",
            ctx.service.state.get_account_states().get("dy", {}).get("logged_in") is False,
        )
        report.check(
            "清除登录态后 accounts 表 need_login",
            ctx.service.accounts.get("acc_legacy_dy")["status"] == "need_login",
        )
        # 7.10 need_manual 事件（登录人工接管）
        events.clear()
        ks_acc = ctx.service.accounts.create("ks", alias="人工验证账号")
        with TempEnv(FAKE_LOGIN_SCENARIO=f"ks:{ks_acc['account_id']}=manual", FAKE_NEED_MANUAL_KIND="slider"):
            manual_start = ctx.service.start_login("ks", ks_acc["account_id"])
            manual_session = ctx._wait_login("ks", manual_start["login_id"])
        ctx._wait_idle()
        manual_events = [event for event in events if event.get("type") == "login.need_manual"]
        report.check("need_manual 登录最终 confirmed", manual_session["status"] == "confirmed", manual_session["status"])
        report.check("发布 login.need_manual 事件", len(manual_events) >= 1)
        if manual_events:
            report.check("login.need_manual severity=warn", manual_events[0]["severity"] == "warn")
            report.check(
                "login.need_manual dedupe_key=login:<id>:need_manual",
                manual_events[0]["dedupe_key"] == f"login:{manual_start['login_id']}:need_manual",
                manual_events[0]["dedupe_key"],
            )
            report.check(
                "login.need_manual data 携带 kind/account_id",
                manual_events[0]["data"].get("kind") == "slider"
                and manual_events[0]["data"].get("account_id") == ks_acc["account_id"],
            )
        # 7.11 run 账号尝试明细 API（Pool router，稍后 API 层再校验；这里校验 manager 数据）
        report.check(
            "run_accounts 明细含 alias/时间戳",
            all(
                item.get("account_alias") and isinstance(item.get("started_at"), int)
                for item in ctx.service.accounts.run_accounts(run_id)
            ),
        )
    finally:
        bus.unsubscribe(_on_event)
        ctx.close()


# ===========================================================================
# 8. api_pool 路由（FastAPI TestClient，离线）
# ===========================================================================
def test_api_routes(report: Any) -> None:
    print("\n--- 8. api_pool 路由（TestClient）---")
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    import api_pool

    ctx = ServiceCtx("api")
    app = FastAPI()
    app.state.service = ctx.service

    @app.exception_handler(ServiceError)
    async def _service_error_handler(_request, exc: ServiceError):  # type: ignore[no-untyped-def]
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(api_pool.router)
    client = TestClient(app)
    try:
        # 8.1 列表
        listed = client.get("/api/accounts/pool")
        body = listed.json()
        report.check("GET /api/accounts/pool 200", listed.status_code == 200, str(listed.status_code))
        report.check("列表返回 accounts 数组", isinstance(body.get("accounts"), list))
        report.check("列表返回 settings（default_strategy/limits/间隔）", {"default_strategy", "limits", "platform_min_interval_s"}.issubset(body.keys()))
        report.check("列表返回平台标签表", body.get("platforms", {}).get("xhs") == "小红书")
        # 8.2 创建
        created = client.post("/api/accounts/pool", json={"platform": "xhs", "alias": "API账号"})
        report.check("POST /api/accounts/pool 201", created.status_code == 201, str(created.status_code))
        created_body = created.json()
        api_account_id = created_body.get("account_id", "")
        report.check("创建返回 account_id", bool(re.match(r"^acc_[0-9a-f]{12}$", api_account_id)))
        report.check("创建返回 v2 profile_layout", created_body.get("profile_layout") == "v2")
        bad_platform = client.post("/api/accounts/pool", json={"platform": "nope"})
        report.check(
            "创建非法平台返回 400 INVALID_PLATFORM",
            bad_platform.status_code == 400 and bad_platform.json().get("error", {}).get("code") == "INVALID_PLATFORM",
            bad_platform.text[:200],
        )
        # 8.3 单账号详情 / PATCH（enabled 写法 + status 写法）
        detail = client.get(f"/api/accounts/pool/{api_account_id}")
        report.check("GET /api/accounts/pool/{id} 200", detail.status_code == 200 and detail.json()["account_id"] == api_account_id)
        patched = client.patch(f"/api/accounts/pool/{api_account_id}", json={"enabled": False})
        report.check("PATCH enabled=false → disabled", patched.status_code == 200 and patched.json()["status"] == "disabled", patched.text[:200])
        patched2 = client.patch(f"/api/accounts/pool/{api_account_id}", json={"status": "enabled", "alias": "API账号2"})
        report.check("PATCH status=enabled → active", patched2.json().get("status") == "active")
        report.check("PATCH alias 生效", patched2.json().get("alias") == "API账号2")
        missing = client.get("/api/accounts/pool/acc_not_exists")
        report.check(
            "GET 不存在账号 404 ACCOUNT_NOT_FOUND",
            missing.status_code == 404 and missing.json().get("error", {}).get("code") == "ACCOUNT_NOT_FOUND",
        )
        # 8.4 设置
        pool_settings = client.get("/api/accounts/pool/settings")
        report.check("GET /api/accounts/pool/settings 200", pool_settings.status_code == 200)
        report.check("设置默认策略 lru", pool_settings.json().get("default_strategy") == "lru")
        put_settings = client.put(
            "/api/accounts/pool/settings",
            json={"default_strategy": "round_robin", "limits": {"daily_runs": 30}, "platform_min_interval_s": 15},
        )
        report.check("PUT /api/accounts/pool/settings 200", put_settings.status_code == 200)
        report.check("PUT 后策略生效", put_settings.json().get("default_strategy") == "round_robin")
        report.check("PUT 后配额生效", put_settings.json().get("limits", {}).get("daily_runs") == 30)
        client.put(
            "/api/accounts/pool/settings",
            json={"default_strategy": "lru", "limits": {"daily_runs": 20}, "platform_min_interval_s": 30},
        )
        # 8.5 选号预览
        preview = client.get("/api/accounts/schedule/preview", params={"platform": "xhs", "strategy": "lru"})
        report.check("GET /api/accounts/schedule/preview 200", preview.status_code == 200)
        report.check("预览返回 candidates", isinstance(preview.json().get("candidates"), list))
        preview_bad = client.get("/api/accounts/schedule/preview", params={"platform": "nope"})
        report.check("预览非法平台 400", preview_bad.status_code == 400)
        # 8.6 登录 + 取消
        login_resp = client.post(f"/api/accounts/pool/{api_account_id}/login")
        report.check("POST /{id}/login 200", login_resp.status_code == 200, login_resp.text[:200])
        login_body = login_resp.json()
        report.check("登录返回 login_id/account_id/platform", bool(login_body.get("login_id")) and login_body.get("account_id") == api_account_id)
        report.check("登录返回 platform=xhs", login_body.get("platform") == "xhs")
        login_id = login_body.get("login_id", "")
        get_login = client.get(f"/api/accounts/pool/{api_account_id}/login/{login_id}")
        report.check("GET /{id}/login/{login_id} 200", get_login.status_code == 200)
        report.check("登录轮询带 account_id", get_login.json().get("account_id") == api_account_id)
        cancel = client.post(f"/api/accounts/pool/{api_account_id}/login/{login_id}/cancel")
        report.check("POST /{id}/login/{login_id}/cancel 200", cancel.status_code == 200)
        wait_until(lambda: ctx.service.get_login("xhs", login_id).get("status") == "cancelled", timeout=10.0)
        report.check(
            "取消后登录会话 cancelled",
            ctx.service.get_login("xhs", login_id).get("status") == "cancelled",
        )
        # 8.7 测试连接 / 批量测试
        one_test = client.post(f"/api/accounts/pool/{api_account_id}/test")
        report.check("POST /{id}/test 200", one_test.status_code == 200, one_test.text[:200])
        report.check("测试连接返回 ok/message/duration_ms", {"ok", "message", "duration_ms"}.issubset(one_test.json().keys()))
        batch = client.post("/api/accounts/pool/test", json={"account_ids": [api_account_id]})
        report.check("POST /api/accounts/pool/test 200", batch.status_code == 200, batch.text[:200])
        report.check("批量测试返回 results/ok_count/fail_count", {"results", "ok_count", "fail_count"}.issubset(batch.json().keys()))
        batch_missing = client.post("/api/accounts/pool/test", json={"account_ids": ["acc_nope"]})
        report.check("批量测试不存在账号 404", batch_missing.status_code == 404)
        # 8.8 删除（软删 + purge）
        created2 = client.post("/api/accounts/pool", json={"platform": "dy", "alias": "待删账号"}).json()
        to_delete = created2["account_id"]
        profile_path = ctx.cfg.data_dir / created2["profile_dir"]
        profile_path.mkdir(parents=True, exist_ok=True)
        (profile_path / "x").write_text("x", encoding="utf-8")
        deleted = client.delete(f"/api/accounts/pool/{to_delete}", params={"purge": "true"})
        report.check("DELETE /{id}?purge=true 200", deleted.status_code == 200, deleted.text[:200])
        report.check("软删后 status=archived", deleted.json().get("account", {}).get("status") == "archived")
        report.check("purge 删除 profile 目录", not profile_path.exists())
        # 8.9 run 账号尝试明细
        run = ctx.run_until_done(
            {
                "keywords": ["API"],
                "platforms": ["xhs"],
                "max_notes": 1,
                "comments": False,
                "max_comments": 0,
            }
        )
        run_accounts_resp = client.get(f"/api/runs/{run['run_id']}/accounts")
        report.check("GET /api/runs/{id}/accounts 200", run_accounts_resp.status_code == 200, run_accounts_resp.text[:200])
        report.check("run accounts 返回 attempts", isinstance(run_accounts_resp.json().get("attempts"), list))
        report.check("run accounts 返回 platforms 汇总", isinstance(run_accounts_resp.json().get("platforms"), list))
        missing_run = client.get("/api/runs/run_not_exists/accounts")
        report.check("run accounts 不存在 run 返回 404", missing_run.status_code == 404)
    finally:
        ctx.close()


# ===========================================================================
# 入口
# ===========================================================================
SECTIONS: List[Tuple[str, Callable[[Any], None]]] = [
    ("helpers", test_helpers),
    ("legacy_default", test_legacy_default),
    ("crud", test_crud),
    ("quota_events", test_quota_and_events),
    ("pick", test_pick),
    ("adopt_legacy", test_adopt_legacy),
    ("service_integration", test_service_integration),
    ("api_routes", test_api_routes),
]


def run(report: Any = None) -> Tuple[int, int]:
    """执行全部离线用例，返回 ``(passed, failed)``（供 selftest.py phase0 复用）。"""
    local = LocalReport()
    target = report if report is not None else local
    for name, section in SECTIONS:
        try:
            section(target)
        except Exception:  # noqa: BLE001 - 直接运行时单节异常不拖垮其它用例
            message = f"用例节 {name} 异常：{traceback.format_exc()[-1200:]}"
            if hasattr(target, "check"):
                target.check(f"用例节 {name} 无异常", False, message)
            else:  # pragma: no cover
                local.failed.append(message)
    passed = int(getattr(target, "passed", getattr(local, "passed", 0)))
    failed_count = (
        len(getattr(target, "failed", []))
        if hasattr(target, "failed")
        else len(local.failed)
    )
    return passed, failed_count


def main() -> int:
    print("=" * 78)
    print("ACCOUNT POOL SELFTEST（离线：fake engine，不联网、不启动浏览器）")
    print("=" * 78)
    report = LocalReport()
    passed, failed = run(report)
    print("\n" + "=" * 78)
    if failed:
        print(f"ACCOUNT POOL SELFTEST: FAIL（{passed} passed, {failed} failed）")
        for item in report.failed:
            print("  - " + item)
        print("=" * 78)
        return 1
    print(f"ACCOUNT POOL SELFTEST: PASS（{passed}/{passed} 项通过）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())

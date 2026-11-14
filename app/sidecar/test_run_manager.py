# -*- coding: utf-8 -*-
"""RunManager 离线单测（M2）：全部用 FakeBackend，不启子进程、不联网。

覆盖：happy path / 平台失败继续 / 网络重试 / login_invalid 换号 / risk_blocked 换号失败 /
stop→cancelled / 续采 attempt 编号 / 无可用账号 / 配额记账 / 失败分类 / 事件契约 + 门面导出。

运行：
    D:\\OpinionLens\\MediaCrawler\\.venv\\Scripts\\python.exe app\\sidecar\\test_run_manager.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SIDECAR_DIR = Path(__file__).resolve().parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))

import engine_runner as er  # noqa: E402
from events import bus  # noqa: E402
from runner.run import FakeBackend, RunManager, _is_retryable_crawl_error  # noqa: E402
from state import StateStore  # noqa: E402

CHECKS: List[tuple] = []
FAILED: List[tuple] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok)))
    if not ok:
        FAILED.append((name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  |  {detail}" if detail else ""))
    return bool(ok)


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + float(timeout)
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 轮询忽略瞬态
            pass
        time.sleep(interval)
    return False


class TempEnv:
    def __init__(self, **values: Any) -> None:
        self.values = {key: str(value) for key, value in values.items()}
        self.old: Dict[str, Optional[str]] = {}

    def __enter__(self) -> "TempEnv":
        for key, value in self.values.items():
            self.old[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *_exc: Any) -> None:
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class FakeAccounts:
    """最小账号池替身：只实现 RunManager 用到的接口，记录调用便于断言。"""

    def __init__(
        self,
        pool: Optional[Dict[str, List[Dict[str, str]]]] = None,
        kinds: Optional[Dict[str, str]] = None,
    ) -> None:
        self.pool = {platform: [dict(item) for item in items] for platform, items in (pool or {}).items()}
        self.kinds = dict(kinds or {})
        self.attempts: List[tuple] = []
        self.finished: List[tuple] = []
        self.results: List[tuple] = []

    def default_strategy(self) -> str:
        return "lru"

    def _items(self, platform: str) -> List[Dict[str, str]]:
        return self.pool.setdefault(platform, [])

    def pick(self, platform: str, strategy: str = "", exclude: Optional[List[str]] = None) -> Dict[str, Any]:
        exclude_set = set(exclude or [])
        for account in self._items(platform):
            account_id = account["account_id"]
            if account_id in exclude_set or account.get("status", "active") != "active":
                continue
            return {
                "account_id": account_id,
                "alias": account.get("alias") or account_id,
                "account": account,
                "strategy": strategy or "lru",
                "reasons": ["fake"],
            }
        raise er.ServiceError("NO_ACCOUNT", "没有可用账号", 409)

    def pick_manual(self, platform: str, account_id: str) -> Dict[str, Any]:
        for account in self._items(platform):
            if account["account_id"] == account_id:
                return {"account_id": account_id, "alias": account.get("alias") or account_id, "account": account}
        raise er.ServiceError("ACCOUNT_NOT_FOUND", f"账号不存在：{account_id}", 404)

    def legacy_fallback(self, platform: str, exclude: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        return None

    def engine_account_args(self, account: Dict[str, Any]) -> Dict[str, str]:
        return {"account_id": str(account.get("account_id") or ""), "profile_template": ""}

    def max_attempt(self, run_id: str, platform: str) -> int:
        numbers = [item[2] for item in self.attempts if item[0] == run_id and item[1] == platform]
        return max(numbers, default=0)

    def mark_attempt(self, run_id: str, platform: str, attempt_no: int, account_id: str, reason: str = "") -> None:
        self.attempts.append((run_id, platform, int(attempt_no), account_id, reason))

    def on_crawl_result(
        self,
        account_id: str,
        ok: bool,
        message: str = "",
        saved_items: int = 0,
        platform: str = "",
        run_id: str = "",
    ) -> Dict[str, Any]:
        kind = "" if ok else str(self.kinds.get(account_id, self.kinds.get("*", "")))
        self.results.append((account_id, bool(ok), str(message)[:80], kind))
        return {"kind": kind}

    def finish_attempt(
        self,
        run_id: str,
        platform: str,
        attempt_no: int,
        status: str,
        message: str = "",
        account_id: str = "",
    ) -> None:
        self.finished.append((run_id, platform, int(attempt_no), status))

    def last_attempt_accounts(self, run_id: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for item in self.attempts:
            if item[0] == run_id:
                out[item[1]] = item[3]
        return out

    def alias_of(self, account_id: str) -> str:
        return f"alias-{account_id}"


def make_cfg(data_dir: Path) -> er.SidecarConfig:
    mc_root = SIDECAR_DIR.parent.parent / "MediaCrawler"
    if not (mc_root / "main.py").is_file():
        mc_root = Path(tempfile.gettempdir()) / f"opinionlens_runmgr_mc_{os.getpid()}"
        (mc_root / "media_platform").mkdir(parents=True, exist_ok=True)
        (mc_root / "main.py").write_text("# stub for offline RunManager test\n", encoding="utf-8")
    entry = Path(tempfile.gettempdir()) / "opinionlens_fake_engine.py"
    if not entry.is_file():
        entry.write_text("# fake engine placeholder (FakeBackend 不启动它)\n", encoding="utf-8")
    return er.SidecarConfig.from_env(
        data_dir, mc_root=mc_root, python=sys.executable, engine_entry=entry
    )


class Ctx:
    """一次测试的 RunManager 上下文（临时 data-dir + 真实 StateStore）。"""

    def __init__(self, name: str, accounts: Optional[FakeAccounts], backend: FakeBackend) -> None:
        self.data_dir = Path(tempfile.mkdtemp(prefix=f"runmgr_{name}_")).resolve()
        self.cfg = make_cfg(self.data_dir)
        self.cfg.ensure_dirs()
        self.state = StateStore(self.cfg.app_db)
        self.guard = er.ActivityGuard()
        self.accounts = accounts
        self.backend = backend
        # runner=None：执行完全走注入的 backend（RunManager 不再自己 new 进程）
        self.manager = RunManager(None, self.state, self.guard, self.cfg, accounts=accounts, backend=backend)

    def create(self, platforms: List[str], **overrides: Any) -> str:
        params = {
            "keywords": ["示例科技"],
            "platforms": list(platforms),
            "max_notes": 5,
            "comments": True,
            "max_comments": 3,
            "name": "单测任务",
            "account_policy": overrides.pop("account_policy", {}),
        }
        params.update(overrides)
        return self.manager.create(params)["run_id"]

    def wait_done(self, run_id: str, timeout: float = 15.0) -> Dict[str, Any]:
        result: Dict[str, Any] = {}

        def _done() -> bool:
            nonlocal result
            result = self.state.get_run(run_id) or {}
            return bool(result) and result.get("status") != "running"

        wait_until(_done, timeout=timeout)
        return result


def ok_events(saved: int = 3, message: str = "采集完成") -> List[Dict[str, Any]]:
    return [
        {"type": "engine.started", "mode": "crawl", "platform": "xhs"},
        {"type": "progress", "stage": "search", "page": 1, "saved": saved, "message": "搜索"},
        {"type": "engine.finished", "ok": True, "message": message, "db_path": "/tmp/fake.db"},
    ]


def fail_events(message: str) -> List[Dict[str, Any]]:
    return [
        {"type": "engine.started", "mode": "crawl", "platform": "xhs"},
        {"type": "engine.finished", "ok": False, "message": message, "db_path": None},
    ]


def platform_row(run: Dict[str, Any], platform: str) -> Dict[str, Any]:
    for item in run.get("platforms") or []:
        if item.get("platform") == platform:
            return item
    return {}


# ---------------------------------------------------------------------------
# 1. happy path
# ---------------------------------------------------------------------------
def test_happy_path() -> None:
    print("\n--- 1. happy path：2 平台都 done ---")
    accounts = FakeAccounts({"xhs": [{"account_id": "a1"}], "dy": [{"account_id": "d1"}]})
    backend = FakeBackend({"xhs": [ok_events(3)], "dy": [ok_events(2)]})
    ctx = Ctx("happy", accounts, backend)
    events: List[Dict[str, Any]] = []
    handler = events.append
    bus.subscribe(handler)
    try:
        run_id = ctx.create(["xhs", "dy"])
        run = ctx.wait_done(run_id)
    finally:
        bus.unsubscribe(handler)

    xhs = platform_row(run, "xhs")
    dy = platform_row(run, "dy")
    run_events = [event for event in events if str(event.get("type") or "").startswith("run.")]
    check("happy: run=done", run.get("status") == "done", str(run.get("status")))
    check("happy: 两个平台都 done", xhs.get("status") == "done" and dy.get("status") == "done", f"xhs={xhs.get('status')} dy={dy.get('status')}")
    check(
        "happy: 事件顺序 = 平台开始/结束日志 → run.finished（bus 只发一条收尾事件）",
        len(run_events) == 1
        and run_events[0].get("type") == "run.finished"
        and xhs.get("started_at") and xhs.get("finished_at")
        and run_events[0]["data"]["platforms_ok"] == 2,
        f"events={[e.get('type') for e in run_events]}",
    )
    check("happy: 两个平台各起一次 backend", [p for p, _ in backend.started] == ["xhs", "dy"], str([p for p, _ in backend.started]))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. 单平台失败但任务继续
# ---------------------------------------------------------------------------
def test_platform_failure_continues() -> None:
    print("\n--- 2. 单平台失败但另一平台仍完成 ---")
    accounts = FakeAccounts({"xhs": [{"account_id": "a1"}], "dy": [{"account_id": "d1"}]})
    backend = FakeBackend(
        {"xhs": [fail_events("登录失效，请重新扫码")], "dy": [ok_events(4)]}
    )
    ctx = Ctx("partial", accounts, backend)
    with TempEnv(SIDECAR_ACCOUNT_MAX_ATTEMPTS="1"):
        run_id = ctx.create(["xhs", "dy"])
        run = ctx.wait_done(run_id)
    xhs = platform_row(run, "xhs")
    dy = platform_row(run, "dy")
    check("失败继续: xhs failed / dy done", xhs.get("status") == "failed" and dy.get("status") == "done", f"xhs={xhs.get('status')} dy={dy.get('status')}")
    check("失败继续: run 结束（非 running）", run.get("status") in ("done", "failed"), str(run.get("status")))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. 网络类失败 → 同号重试
# ---------------------------------------------------------------------------
def test_network_retry() -> None:
    print("\n--- 3. 网络失败同号重试（SIDECAR_AUTO_RETRY_MAX）---")
    accounts = FakeAccounts({"xhs": [{"account_id": "a1"}]})
    backend = FakeBackend(
        {"xhs": [fail_events("net::ERR_CONNECTION_CLOSED at https://edith.xiaohongshu.com"), ok_events(5)]}
    )
    ctx = Ctx("retry", accounts, backend)
    with TempEnv(SIDECAR_AUTO_RETRY_MAX="1", SIDECAR_AUTO_RETRY_DELAY="1", SIDECAR_ACCOUNT_MAX_ATTEMPTS="1"):
        run_id = ctx.create(["xhs"])
        run = ctx.wait_done(run_id, timeout=20.0)
    xhs = platform_row(run, "xhs")
    check("网络重试: 同平台起 2 次 backend（首次失败 + 重试成功）", len([p for p, _ in backend.started if p == "xhs"]) == 2, str(backend.started))
    check("网络重试: 最终 done", xhs.get("status") == "done" and run.get("status") == "done", f"xhs={xhs.get('status')} run={run.get('status')}")
    check("网络重试: 重试仍用同一账号（attempt 记录 1 次换号）", [a[3] for a in accounts.attempts] == ["a1"], str(accounts.attempts))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4/9. login_invalid → 换号；mark_attempt 记账
# ---------------------------------------------------------------------------
def test_login_invalid_failover_and_accounting() -> None:
    print("\n--- 4/9. login_invalid 换号 + 配额记账 ---")
    accounts = FakeAccounts(
        {"xhs": [{"account_id": "a1"}, {"account_id": "a2"}]},
        kinds={"a1": "login_invalid"},
    )
    backend = FakeBackend(
        {"xhs": [fail_events("登录失效，请重新扫码"), ok_events(6)]}
    )
    ctx = Ctx("failover", accounts, backend)
    with TempEnv(SIDECAR_ACCOUNT_MAX_ATTEMPTS="2", SIDECAR_AUTO_RETRY_MAX="0"):
        run_id = ctx.create(["xhs"])
        run = ctx.wait_done(run_id)
    xhs = platform_row(run, "xhs")
    attempts = [(item[2], item[3]) for item in accounts.attempts]
    check("换号: 第 1 次 a1 失败 → 第 2 次 a2 成功", xhs.get("status") == "done" and [a[1] for a in attempts] == ["a1", "a2"], f"attempts={attempts}")
    check("换号: run_accounts 记录 attempt 1/2（配额记账被调用）", attempts == [(1, "a1"), (2, "a2")] and len(accounts.finished) == 2, f"finished={accounts.finished}")
    check("换号: mark_attempt 次数 = 尝试次数", len(accounts.attempts) == 2, str(len(accounts.attempts)))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5. risk_blocked 换号后仍失败
# ---------------------------------------------------------------------------
def test_risk_blocked_failover_failed() -> None:
    print("\n--- 5. risk_blocked 换号后仍失败 ---")
    accounts = FakeAccounts(
        {"xhs": [{"account_id": "a1"}, {"account_id": "a2"}]},
        kinds={"*": "risk_blocked"},
    )
    backend = FakeBackend(
        {"xhs": [fail_events("账号异常，疑似封禁"), fail_events("账号异常，疑似封禁")]}
    )
    ctx = Ctx("risk", accounts, backend)
    with TempEnv(SIDECAR_ACCOUNT_MAX_ATTEMPTS="2"):
        run_id = ctx.create(["xhs"])
        run = ctx.wait_done(run_id)
    xhs = platform_row(run, "xhs")
    check("风控: 两个账号都失败 → 平台 failed", xhs.get("status") == "failed", str(xhs.get("status")))
    check("风控: 确实换了号（a1 → a2）", [a[3] for a in accounts.attempts] == ["a1", "a2"], str(accounts.attempts))
    check("风控: message 带上自动换号说明", "自动切换账号" in str(xhs.get("message") or ""), str(xhs.get("message"))[:120])
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6. stop → cancelled + 未开始平台 skipped
# ---------------------------------------------------------------------------
def test_stop_cancelled() -> None:
    print("\n--- 6. stop → cancelled，未开始平台 skipped ---")
    accounts = FakeAccounts({"xhs": [{"account_id": "a1"}], "dy": [{"account_id": "d1"}]})
    backend = FakeBackend({"xhs": [ok_events()], "dy": [ok_events()]}, manual_platforms={"xhs"})
    ctx = Ctx("stop", accounts, backend)
    run_id = ctx.create(["xhs", "dy"])
    wait_until(lambda: len(backend.started) >= 1, timeout=5.0)
    ctx.manager.stop(run_id)
    run = ctx.wait_done(run_id)
    xhs = platform_row(run, "xhs")
    dy = platform_row(run, "dy")
    check("stop: run=cancelled", run.get("status") == "cancelled", str(run.get("status")))
    check("stop: 运行中平台 failed（已取消）", xhs.get("status") == "failed" and "取消" in str(xhs.get("message") or ""), f"xhs={xhs.get('status')} msg={xhs.get('message')}")
    check("stop: 未开始平台 skipped", dy.get("status") == "skipped", str(dy.get("status")))
    check("stop: backend.stop 被调用", bool(backend.stopped), str(len(backend.stopped)))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7. 续采：attempt 编号递增不覆盖历史
# ---------------------------------------------------------------------------
def test_resume_attempt_numbering() -> None:
    print("\n--- 7. 续采 attempt 编号递增 ---")
    accounts = FakeAccounts({"xhs": [{"account_id": "a1"}]})
    backend = FakeBackend(
        {"xhs": [fail_events("登录失效，请重新扫码"), ok_events(7)]}
    )
    ctx = Ctx("resume", accounts, backend)
    with TempEnv(SIDECAR_ACCOUNT_MAX_ATTEMPTS="1", SIDECAR_AUTO_RETRY_MAX="0"):
        run_id = ctx.create(["xhs"])
        first = ctx.wait_done(run_id)
        first_attempts = [(a[2], a[3]) for a in accounts.attempts]
        ctx.manager.resume(run_id)
        second = ctx.wait_done(run_id)
    second_attempts = [(a[2], a[3]) for a in accounts.attempts]
    check("续采: 首轮平台 failed", platform_row(first, "xhs").get("status") == "failed", str(platform_row(first, "xhs").get("status")))
    check("续采: attempt 编号递增（1 → 2，不覆盖历史）", first_attempts == [(1, "a1")] and second_attempts == [(1, "a1"), (2, "a1")], f"{first_attempts} -> {second_attempts}")
    check("续采: 续采后全平台 done → run=done", second.get("status") == "done", str(second.get("status")))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 8. 无可用账号
# ---------------------------------------------------------------------------
def test_no_account() -> None:
    print("\n--- 8. 无可用账号 → 平台 failed ---")
    accounts = FakeAccounts({})
    backend = FakeBackend({})
    ctx = Ctx("noaccount", accounts, backend)
    run_id = ctx.create(["xhs"])
    run = ctx.wait_done(run_id)
    xhs = platform_row(run, "xhs")
    check("无账号: 平台 failed 且 message 含「无可用账号」", xhs.get("status") == "failed" and "无可用账号" in str(xhs.get("message") or ""), str(xhs.get("message"))[:120])
    check("无账号: 没有启动任何引擎进程", backend.started == [], str(backend.started))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 10. 失败分类
# ---------------------------------------------------------------------------
def test_retryable_classification() -> None:
    print("\n--- 10. _is_retryable_crawl_error 分类 ---")
    check("分类: 未登录 / 扫码 → 不重试", _is_retryable_crawl_error("未登录，请扫码") is False and _is_retryable_crawl_error("二维码超时") is False)
    check("分类: net::ERR_* → 重试", _is_retryable_crawl_error("net::ERR_CONNECTION_CLOSED") is True and _is_retryable_crawl_error("Page.goto: Timeout 15000ms") is True)
    check("分类: 空消息 → 不重试", _is_retryable_crawl_error("") is False)


# ---------------------------------------------------------------------------
# 11. 事件契约 + 门面导出
# ---------------------------------------------------------------------------
def test_event_contract_and_facade() -> None:
    print("\n--- 11. 事件契约 + engine_runner 门面导出 ---")
    accounts = FakeAccounts({"xhs": [{"account_id": "a1"}]})
    backend = FakeBackend({"xhs": [ok_events(2)]})
    ctx = Ctx("event", accounts, backend)
    events: List[Dict[str, Any]] = []
    handler = events.append
    bus.subscribe(handler)
    try:
        run_id = ctx.create(["xhs"])
        ctx.wait_done(run_id)
    finally:
        bus.unsubscribe(handler)
    run_events = [event for event in events if event.get("type") == "run.finished"]
    data = (run_events[0].get("data") if run_events else {}) or {}
    check(
        "事件: run.finished 带 run_id/severity/dedupe_key/ts",
        bool(run_events)
        and data.get("run_id") == run_id
        and run_events[0].get("severity") == "info"
        and run_events[0].get("dedupe_key") == f"run:{run_id}:finished"
        and isinstance(run_events[0].get("ts"), int),
        str({k: data.get(k) for k in ("run_id", "platforms_ok", "saved_items", "duration_s")}),
    )
    check(
        "事件: data 含平台统计/条数/耗时（字段名与拆分前一致）",
        data.get("platforms_ok") == 1
        and data.get("platforms_total") == 1
        and data.get("saved_items") == 2
        and isinstance(data.get("duration_s"), int),
        str(data),
    )

    required = [
        "PLATFORM_LABELS","PLATFORM_IDS","PLATFORMS","SIDECAR_VERSION","EngineService","ServiceError",
        "SidecarConfig","ActivityGuard","_to_int","_env_str","_truthy","kill_browser_processes_by_marker",
        "list_browser_processes","read_log_file_tail","classify_log_level","load_exporter","load_run_feed",
        "log","_is_retryable_crawl_error","default_need_manual_message","infer_need_manual_kind",
        "EngineRunner","EngineProcess","LogSink","LoginSession","LoginManager","RunManager","RunRuntime",
        "RUN_STATUS_RUNNING","RUN_STATUS_DONE","RUN_STATUS_FAILED","RUN_STATUS_CANCELLED","CrawlBackend",
        "EngineProcessBackend","FakeBackend","_calc_progress","_run_to_api","_run_to_summary",
        "BUSY_GLOBAL_MESSAGE","_BUSY_MESSAGE","EVENT_PREFIX","LOG_TAIL_MAX","DEFAULT_LOGIN_TIMEOUT",
        "LOGIN_TERMINAL_STATUSES","_ENGINE_LOGIN_STATUS_MAP",
    ]
    missing = [name for name in required if not hasattr(er, name)]
    check("门面: 外部 import 的符号逐个 getattr 全部存在", not missing, str(missing))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


def test_backend_param_contract() -> None:
    """参数转手守卫：RunManager 传给 backend 的 params 键集合必须与生产实现消费的一致。

    历史风险：RunManager 把 keywords/max_notes/comments/max_comments/account_id/profile_template
    平铺进 dict，EngineProcessBackend 再逐个 .get 还原；将来一方加参数而另一方没跟上会**静默丢参**。
    """
    print("\n--- 12. backend 参数契约（防静默丢参） ---")
    accounts = FakeAccounts({"xhs": [{"account_id": "a1"}]})
    backend = FakeBackend({"xhs": [ok_events(1)]})
    ctx = Ctx("params", accounts, backend)
    try:
        run_id = ctx.create(["xhs"])
        ctx.wait_done(run_id)
    finally:
        pass
    expected = {"keywords", "max_notes", "comments", "max_comments", "account_id", "profile_template"}
    started = backend.started or []
    params = started[0][1] if started else {}
    check("backend 参数: 键集合与契约一致（新增参数必须同步 backend）", set(params.keys()) == expected, str(sorted(params.keys())))
    check(
        "backend 参数: 取值来自 run.params（max_notes/comments/max_comments）",
        params.get("max_notes") == 5 and params.get("comments") is True and params.get("max_comments") == 3,
        str({k: params.get(k) for k in ("max_notes", "comments", "max_comments")}),
    )
    check("backend 参数: 带账号 identity（account_id 非空）", bool(params.get("account_id")), str(params.get("account_id")))
    shutil.rmtree(ctx.data_dir, ignore_errors=True)


def test_config_path_contract() -> None:
    """路径守卫：分包后 SIDECAR_DIR 必须仍指向 app/sidecar（而不是 runner/）。

    历史事故：`Path(__file__).parent` 拆分后变成 `app/sidecar/runner`，默认 engine_entry 变成
    `runner/_fake_engine.py`，只有集成自测才会报「引擎入口不存在」。
    """
    print("\n--- 13. 配置路径契约（分包后 __file__ 基准） ---")
    from runner import config as config_mod

    check("路径: SIDECAR_DIR 指向 app/sidecar", config_mod.SIDECAR_DIR.name == "sidecar", str(config_mod.SIDECAR_DIR))
    check("路径: SIDECAR_DIR/main.py 存在", (config_mod.SIDECAR_DIR / "main.py").is_file())
    check("路径: APP_DIR = app", config_mod.APP_DIR.name == "app", str(config_mod.APP_DIR))

    data_dir = Path(tempfile.mkdtemp(prefix="runmgr_cfg_")).resolve()
    try:
        with TempEnv(FAKE_ENGINE="1"):
            cfg_fake = er.SidecarConfig.from_env(data_dir, mc_root=data_dir, python=sys.executable)
        check(
            "路径: FAKE_ENGINE=1 时默认入口落在 sidecar/_fake_engine.py",
            cfg_fake.engine_entry.name == "_fake_engine.py" and cfg_fake.engine_entry.parent.name == "sidecar",
            str(cfg_fake.engine_entry),
        )
        with TempEnv(FAKE_ENGINE="0"):
            cfg_real = er.SidecarConfig.from_env(data_dir, mc_root=data_dir, python=sys.executable)
        check(
            "路径: 非 fake 时默认入口落在 app/engine/entry.py",
            cfg_real.engine_entry.name == "entry.py" and cfg_real.engine_entry.parent.name == "engine",
            str(cfg_real.engine_entry),
        )
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


def main() -> int:
    print("RunManager 离线单测（FakeBackend，不启子进程）")
    tests = [
        test_happy_path,
        test_platform_failure_continues,
        test_network_retry,
        test_login_invalid_failover_and_accounting,
        test_risk_blocked_failover_failed,
        test_stop_cancelled,
        test_resume_attempt_numbering,
        test_no_account,
        test_retryable_classification,
        test_event_contract_and_facade,
        test_backend_param_contract,
        test_config_path_contract,
    ]
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001 - 单测框架：异常记为失败
            import traceback

            traceback.print_exc()
            FAILED.append((test.__name__, "unexpected exception"))
    passed = sum(1 for _name, ok in CHECKS if ok)
    total = len(CHECKS)
    print("\n" + "=" * 70)
    if FAILED:
        print(f"RUN MANAGER SELFTEST: FAIL（{passed}/{total} 项通过；失败 {len(FAILED)}）")
        for name, detail in FAILED:
            print(f"  - {name}  {detail}")
        print("=" * 70)
        return 1
    print(f"RUN MANAGER SELFTEST: PASS（{passed}/{total} 项通过）")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

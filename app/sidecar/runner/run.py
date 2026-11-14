# -*- coding: utf-8 -*-
"""采集任务编排：RunRuntime / RunManager + CrawlBackend（M2 从 engine_runner.py 拆出）。

CrawlBackend 让 RunManager 不再直接持有子进程：
* 生产实现 ``EngineProcessBackend`` 把 ``EngineRunner.start_crawl`` 的 spawn 包起来；
* 单测用 ``FakeBackend`` 按脚本产事件，不启子进程。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from events import bus
from state import StateStore, now_ts

from .config import PLATFORM_LABELS, EngineStartError, ServiceError, SidecarConfig, _to_int
from .process import EngineProcess, LogSink, read_log_file_page, read_log_file_tail

log = logging.getLogger("sidecar.engine")  # 沿用旧 logger 名：日志输出与拆分前逐字节一致


RUN_STATUS_RUNNING = "running"
RUN_STATUS_DONE = "done"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

def _calc_progress(saved: int, expected: int, stage: str) -> int:
    """由事件里的 saved/stage 估算 0-100 进度（不追求精确，UI 只做展示）。"""
    if stage == "done":
        return 100
    if expected and expected > 0:
        return max(1, min(99, int(saved * 100 / expected)))
    stage_weights = {"boot": 5, "login": 10, "search": 20, "detail": 40, "comments": 60}
    return stage_weights.get(stage, 0)

@dataclass
class RunRuntime:
    run_id: str
    params: Dict[str, Any]
    keywords: List[str]
    platforms: List[str]
    started_ts: int
    expected_items: int
    sink: LogSink
    resume: bool = False
    account_policy: Dict[str, Any] = field(default_factory=dict)
    resume_accounts: Dict[str, str] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    process: Optional[EngineProcess] = None
    process_lock: threading.RLock = field(default_factory=threading.RLock)
    results_lock: threading.RLock = field(default_factory=threading.RLock)
    platform_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    saved_counts: Dict[str, int] = field(default_factory=dict)
    finished: bool = False
    thread: Optional[threading.Thread] = None

def _is_retryable_crawl_error(message: str) -> bool:
    """判断采集失败是否为可自动重试的瞬时网络/浏览器错误。

    不重试「未登录、登录失效、扫码/验证码」等需要人工操作的错误，避免反复
    拉起浏览器打扰用户。
    """
    text = str(message or "").strip()
    if not text:
        return False
    lowered = text.lower()
    non_retryable = (
        "未登录",
        "登录失效",
        "登录失败",
        "需要登录",
        "扫码",
        "二维码",
        "验证码",
        "滑块",
        "拼图",
        "账号异常",
        "封禁",
        "not logged",
        "login required",
        "login failed",
    )
    if any(token in text or token in lowered for token in non_retryable):
        return False
    retryable = (
        "err_connection_closed",
        "err_connection_reset",
        "err_connection_refused",
        "err_connection_aborted",
        "err_timed_out",
        "err_name_not_resolved",
        "err_network_changed",
        "err_internet_disconnected",
        "net::err_",
        "page.goto",
        "retryerror",
        "datafetcherror",
        "timeouterror",
        "timeout",
        "timed out",
        "超时",
        "econnreset",
        "econnrefused",
        "connection closed",
        "connection reset",
        "target page, context or browser has been closed",
        "browser has been closed",
        "target closed",
        "引擎未返回 engine.finished",
        "引擎启动失败",
        "引擎启动异常",
    )
    return any(token in text or token in lowered for token in retryable)

class RunManager:
    """采集任务编排：按平台串行跑 engine crawl，单平台失败继续。"""

    def __init__(
        self,
        runner: EngineRunner,
        state: StateStore,
        guard: ActivityGuard,
        cfg: SidecarConfig,
        accounts: Optional[Any] = None,
        backend: Optional[CrawlBackend] = None,
    ) -> None:
        self.runner = runner
        # 采集后端注入（M2）：RunManager 不再直接 new 进程，生产走 EngineProcessBackend，
        # 单测注入 FakeBackend；``runner`` 仅保留给旧调用方/清理逻辑读取。
        self.backend: CrawlBackend = backend if backend is not None else EngineProcessBackend(runner)
        self.state = state
        self.guard = guard
        self.cfg = cfg
        self.accounts = accounts
        self._lock = threading.RLock()
        self._runtimes: Dict[str, RunRuntime] = {}
        self._finished_order: List[str] = []

    # -- 账号策略 ---------------------------------------------------------
    def _normalize_account_policy(self, raw: Any) -> Dict[str, Any]:
        """解析任务账号策略：``{"mode":"auto"|"manual","strategy":"lru","account_ids":{platform:id}}``。"""
        policy = raw if isinstance(raw, dict) else {}
        mode = str(policy.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "manual"):
            raise ServiceError("INVALID_PARAMS", f"account_policy.mode 不合法：{mode}", 400)
        strategy = str(policy.get("strategy") or "").strip().lower()
        if not strategy:
            strategy = self.accounts.default_strategy() if self.accounts is not None else "lru"
        if strategy not in ("lru", "best_health", "round_robin", "manual"):
            raise ServiceError("INVALID_PARAMS", f"account_policy.strategy 不合法：{strategy}", 400)
        account_ids: Dict[str, str] = {}
        raw_ids = policy.get("account_ids") or {}
        if isinstance(raw_ids, dict):
            items = list(raw_ids.items())
        elif isinstance(raw_ids, list):
            items = [
                (item.get("platform"), item.get("account_id"))
                for item in raw_ids
                if isinstance(item, dict)
            ]
        else:
            items = []
        for platform, account_id in items:
            platform_text = str(platform or "").strip()
            account_id_text = str(account_id or "").strip()
            if not platform_text or not account_id_text:
                continue
            if platform_text not in PLATFORM_LABELS:
                raise ServiceError("INVALID_PLATFORM", f"account_policy 平台不支持：{platform_text}", 400)
            account_ids[platform_text] = account_id_text
        if mode == "manual" and not account_ids:
            raise ServiceError("INVALID_PARAMS", "account_policy.mode=manual 时必须指定 account_ids", 400)
        return {"mode": mode, "strategy": strategy, "account_ids": account_ids}

    def _aliases(self) -> Dict[str, str]:
        if self.accounts is None:
            return {}
        try:
            return self.accounts.aliases()
        except Exception:  # noqa: BLE001 - 别名仅展示用
            log.exception("读取账号别名失败")
            return {}

    # -- 对外接口 ---------------------------------------------------------
    def create(self, params: Dict[str, Any]) -> Dict[str, Any]:
        keywords: List[str] = list(params["keywords"])
        platforms: List[str] = list(params["platforms"])
        account_policy = self._normalize_account_policy(params.get("account_policy"))
        run_id = "run_" + uuid.uuid4().hex[:12]
        busy = self.guard.acquire_exclusive("run", run_id)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        sink: Optional[LogSink] = None
        started_ts = now_ts()
        expected_items = max(
            1,
            _to_int(params.get("max_notes"), 0) * max(1, len(keywords)),
        )
        rt: Optional[RunRuntime] = None
        try:
            sink = LogSink(self.cfg.logs_dir / f"run-{run_id}.log", scope=f"run-{run_id}")
            rt = RunRuntime(
                run_id=run_id,
                params=dict(params),
                keywords=keywords,
                platforms=platforms,
                started_ts=started_ts,
                expected_items=expected_items,
                sink=sink,
                account_policy=account_policy,
            )
            self.state.create_run(
                run_id=run_id,
                params=dict(params),
                keywords=keywords,
                platforms=platforms,
                started_ts=started_ts,
                name=str(params.get("name") or ""),
                account_policy=account_policy,
            )
        except Exception:
            if sink is not None:
                sink.close()
            self.guard.release_exclusive(run_id)
            raise
        assert rt is not None and sink is not None
        with self._lock:
            self._prune_finished()
            self._runtimes[run_id] = rt
        sink.append(
            f"[sidecar] 创建采集任务 run_id={run_id} platforms={','.join(platforms)} "
            f"keywords={','.join(keywords)} max_notes={params.get('max_notes')} "
            f"account_policy={json.dumps(account_policy, ensure_ascii=False)}"
        )
        thread = threading.Thread(target=self._worker, args=(rt,), name=f"run-{run_id}", daemon=True)
        rt.thread = thread
        try:
            thread.start()
        except Exception as exc:  # noqa: BLE001 - 线程创建失败也要释放互斥并标记失败
            rt.finished = True
            try:
                self.state.update_run(
                    run_id, status=RUN_STATUS_FAILED, finished_at=now_ts(), error=str(exc)
                )
            except Exception:  # noqa: BLE001
                pass
            sink.close()
            self.guard.release_exclusive(run_id)
            raise ServiceError("RUN_START_FAILED", f"采集线程启动失败：{exc}", 500) from exc
        return {"run_id": run_id, "status": "running"}

    def resume(self, run_id: str, platforms: Optional[List[str]] = None) -> Dict[str, Any]:
        """断点续采：复用同一 run_id / started_ts，只重跑未完成（或显式指定）的平台。"""
        run = self.state.get_run(run_id)
        if run is None:
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        if run["status"] == RUN_STATUS_RUNNING:
            raise ServiceError("RUN_BUSY", "任务仍在运行，无法继续采集", 409)
        with self._lock:
            old_rt = self._runtimes.get(run_id)
        if old_rt is not None and not old_rt.finished:
            raise ServiceError("RUN_BUSY", "任务仍在运行，无法继续采集", 409)

        run_platforms = list(run["platforms"])
        by_platform = {item["platform"]: item for item in run_platforms}
        order = {item["platform"]: int(item.get("seq") or 0) for item in run_platforms}

        selected: List[str] = []
        if platforms:
            seen: set = set()
            for raw in platforms:
                platform = str(raw or "").strip()
                if not platform or platform in seen:
                    continue
                if platform not in by_platform:
                    raise ServiceError(
                        "INVALID_PLATFORM",
                        f"平台 {platform} 不在该任务中（可选：{','.join(by_platform)}）",
                        422,
                    )
                seen.add(platform)
                selected.append(platform)
        if not selected:
            # 默认：只续跑未完成平台（queued/failed/skipped）
            selected = [
                item["platform"]
                for item in run_platforms
                if item["status"] in ("queued", "failed", "skipped")
            ]
            if not selected:
                raise ServiceError(
                    "NO_PENDING_PLATFORMS",
                    "该任务所有平台均已完成，无需继续采集",
                    409,
                )
        selected.sort(key=lambda platform: order.get(platform, 0))

        busy = self.guard.acquire_exclusive("run", run_id)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        sink: Optional[LogSink] = None
        rt: Optional[RunRuntime] = None
        try:
            fresh = self.state.get_run(run_id)
            if fresh is None:
                raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
            if fresh["status"] == RUN_STATUS_RUNNING:
                raise ServiceError("RUN_BUSY", "任务仍在运行，无法继续采集", 409)

            baselines = {
                platform: _to_int(by_platform[platform].get("saved_items"), 0)
                for platform in selected
            }
            expected_items = max(
                1,
                _to_int(fresh["params"].get("max_notes"), 0) * max(1, len(fresh["keywords"])),
            )
            for platform in selected:
                baseline = baselines[platform]
                progress = (
                    max(1, min(99, int(baseline * 100 / expected_items))) if baseline else 0
                )
                self.state.update_platform(
                    run_id,
                    platform,
                    status="queued",
                    message="等待继续采集",
                    stage="",
                    progress=progress,
                    saved_items=baseline,
                    started_at=None,
                    finished_at=None,
                )
            self.state.reset_run_for_resume(run_id)

            sink = LogSink(self.cfg.logs_dir / f"run-{run_id}.log", scope=f"run-{run_id}")
            resume_accounts = (
                self.accounts.last_attempt_accounts(run_id) if self.accounts is not None else {}
            )
            resume_policy = fresh.get("account_policy") or self._normalize_account_policy(
                fresh["params"].get("account_policy")
            )
            rt = RunRuntime(
                run_id=run_id,
                params=dict(fresh["params"]),
                keywords=list(fresh["keywords"]),
                platforms=list(selected),
                started_ts=int(fresh["started_ts"]),
                expected_items=expected_items,
                sink=sink,
                resume=True,
                account_policy=dict(resume_policy or {}),
                resume_accounts={
                    platform: account_id
                    for platform, account_id in (resume_accounts or {}).items()
                    if platform in selected
                },
            )
            with rt.results_lock:
                rt.saved_counts = dict(baselines)
            with self._lock:
                old = self._runtimes.get(run_id)
                if old is not None and old.finished:
                    old.sink.close()
                if run_id in self._finished_order:
                    self._finished_order.remove(run_id)
                self._prune_finished()
                self._runtimes[run_id] = rt
            sink.append(
                f"[sidecar] 断点续采 run_id={run_id} platforms={','.join(selected)} "
                f"started_ts={fresh['started_ts']}（复用原值）account_policy={json.dumps(rt.account_policy, ensure_ascii=False)}"
            )
            for platform in selected:
                sink.append(
                    f"[sidecar] 续采平台 {platform}：saved_items 基线={baselines[platform]} "
                    f"优先沿用账号={rt.resume_accounts.get(platform) or '自动选号'}"
                )
            thread = threading.Thread(
                target=self._worker, args=(rt,), name=f"run-{run_id}-resume", daemon=True
            )
            rt.thread = thread
            try:
                thread.start()
            except Exception as exc:  # noqa: BLE001
                rt.finished = True
                self.guard.release_exclusive(run_id)
                sink.close()
                try:
                    self.state.update_run(
                        run_id, status=RUN_STATUS_FAILED, finished_at=now_ts(), error=str(exc)
                    )
                except Exception:  # noqa: BLE001
                    pass
                raise ServiceError("RUN_START_FAILED", f"续采线程启动失败：{exc}", 500) from exc
        except BaseException:
            if sink is not None and (rt is None or rt.thread is None):
                sink.close()
            self.guard.release_exclusive(run_id)
            raise
        return {"run_id": run_id, "status": "running", "resumed_platforms": selected}

    def stop(self, run_id: str) -> Dict[str, Any]:
        if not self.state.run_exists(run_id):
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        with self._lock:
            rt = self._runtimes.get(run_id)
        if rt is None or rt.finished:
            # 已结束：幂等返回 ok（UI 可能停止按钮点晚）
            return {"ok": True}
        rt.stop_event.set()
        with rt.process_lock:
            proc = rt.process
        self.backend.stop(proc)
        rt.sink.append("[sidecar] 收到停止请求，正在终止引擎…")
        return {"ok": True}

    def get_run(self, run_id: str) -> Dict[str, Any]:
        run = self.state.get_run(run_id)
        if run is None:
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        return _run_to_api(run, self._aliases())

    def get_run_raw(self, run_id: str) -> Dict[str, Any]:
        run = self.state.get_run(run_id)
        if run is None:
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        return run

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        runs = self.state.list_runs(limit=limit)
        aliases = self._aliases()
        return [_run_to_summary(run, aliases) for run in runs]

    def logs(
        self, run_id: str, tail: int = 200, after_line: Optional[int] = None
    ) -> Tuple[List[Dict[str, Any]], int]:
        """返回 ``(行列表, next_line)``。

        ``after_line=None`` → 尾部 ``tail`` 行（现状语义）；传 ``after_line`` →
        只返回该行号（0 基）之后的新行，``next_line`` 供前端下次轮询使用。
        """
        if not self.state.run_exists(run_id):
            raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
        with self._lock:
            rt = self._runtimes.get(run_id)
        log_path = self.cfg.logs_dir / f"run-{run_id}.log"
        if rt is not None:
            return rt.sink.page(tail, after_line)
        return read_log_file_page(log_path, tail, after_line)

    def is_running(self, run_id: str) -> bool:
        run = self.state.get_run(run_id)
        return bool(run and run["status"] == RUN_STATUS_RUNNING)

    def shutdown(self) -> None:
        with self._lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            if rt.finished:
                continue
            rt.stop_event.set()
            with rt.process_lock:
                proc = rt.process
            self.backend.stop(proc)
        deadline = time.monotonic() + 8.0
        for rt in runtimes:
            if rt.thread is not None:
                rt.thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for rt in runtimes:
            if not rt.finished:
                try:
                    self.state.update_run(
                        rt.run_id, status=RUN_STATUS_CANCELLED, finished_at=now_ts()
                    )
                except Exception:  # noqa: BLE001
                    pass
        for rt in runtimes:
            rt.sink.close()

    # -- 编排实现 ---------------------------------------------------------
    def _prune_finished(self) -> None:
        """内存中最多保留 20 个已结束 runtime（更多历史从日志文件读取）。"""
        while len(self._finished_order) > 20:
            run_id = self._finished_order.pop(0)
            rt = self._runtimes.pop(run_id, None)
            if rt is not None:
                rt.sink.close()

    def _worker(self, rt: RunRuntime) -> None:
        final_status = RUN_STATUS_DONE
        current_platform: Optional[str] = None
        try:
            for platform in rt.platforms:
                current_platform = platform
                if rt.stop_event.is_set():
                    self.state.update_platform(
                        rt.run_id,
                        platform,
                        status="skipped",
                        message="已取消，未执行",
                        finished_at=now_ts(),
                    )
                    continue
                self._run_one_platform(rt, platform)
                if rt.stop_event.is_set():
                    # 后续平台在下一轮循环开头被标记 skipped
                    continue
            run_error: Optional[str] = None
            if rt.stop_event.is_set():
                final_status = RUN_STATUS_CANCELLED
            elif rt.resume:
                # 续采结束按「全 run 所有平台是否都 done」判定：全 done 才 done，否则 failed 便于再次续采
                current = self.state.get_run(rt.run_id) or {"platforms": []}
                all_done = bool(current["platforms"]) and all(
                    item["status"] == "done" for item in current["platforms"]
                )
                final_status = RUN_STATUS_DONE if all_done else RUN_STATUS_FAILED
                run_error = "" if all_done else "部分平台失败"
                rt.sink.append(
                    f"[sidecar] 续采结束：全 run 平台 "
                    + ",".join(
                        f"{item['platform']}={item['status']}" for item in current["platforms"]
                    )
                    + f" → {final_status}"
                )
            else:
                final_status = RUN_STATUS_DONE
            self.state.update_run(
                rt.run_id, status=final_status, finished_at=now_ts(), error=run_error
            )
        except Exception as exc:  # noqa: BLE001 - 编排层异常不能拖垮 sidecar
            log.exception("采集任务 %s 编排异常", rt.run_id)
            final_status = RUN_STATUS_FAILED
            try:
                if current_platform is not None:
                    self.state.update_platform(
                        rt.run_id,
                        current_platform,
                        status="failed",
                        message=f"编排异常：{exc}",
                        finished_at=now_ts(),
                    )
                self.state.update_run(
                    rt.run_id, status=RUN_STATUS_FAILED, finished_at=now_ts(), error=str(exc)
                )
            except Exception:  # noqa: BLE001
                pass
        finally:
            rt.finished = True
            rt.sink.append(f"===== 采集任务 {rt.run_id} 结束：{final_status} =====")
            with rt.process_lock:
                rt.process = None
            self.guard.release_exclusive(rt.run_id)
            self._publish_run_event(rt, final_status)
            with self._lock:
                self._finished_order.append(rt.run_id)

    def _publish_run_event(self, rt: RunRuntime, final_status: str) -> None:
        """发布 run.finished / run.failed / run.partial 事件（契约 §2.1）。"""
        try:
            run = self.state.get_run(rt.run_id) or {}
            platforms = run.get("platforms") or []
            ok_count = sum(1 for item in platforms if item.get("status") == "done")
            failed_count = sum(1 for item in platforms if item.get("status") == "failed")
            skipped_count = sum(
                1 for item in platforms if item.get("status") in ("skipped", "queued")
            )
            saved_items = sum(_to_int(item.get("saved_items"), 0) for item in platforms)
            duration_s = max(0, now_ts() - int(rt.started_ts))
            platform = ""
            account_id = ""
            for item in reversed(platforms):
                if item.get("account_id"):
                    platform = str(item.get("platform") or "")
                    account_id = str(item.get("account_id") or "")
                    break
            account_alias = ""
            if account_id and self.accounts is not None:
                try:
                    account_alias = self.accounts.alias_of(account_id)
                except Exception:  # noqa: BLE001 - 别名仅展示用
                    account_alias = ""
            name = str(rt.params.get("name") or "")
            if final_status == RUN_STATUS_DONE:
                event_type, severity, dedupe_key = "run.finished", "info", f"run:{rt.run_id}:finished"
                title = f"采集完成：{name or rt.run_id}"
            elif final_status == RUN_STATUS_FAILED:
                event_type, severity, dedupe_key = "run.failed", "error", f"run:{rt.run_id}:failed"
                title = f"采集失败：{name or rt.run_id}"
            else:
                event_type, severity, dedupe_key = "run.partial", "warn", f"run:{rt.run_id}:partial"
                title = f"采集部分完成：{name or rt.run_id}"
            parts = [
                f"平台 {ok_count}/{len(platforms)} 成功",
                f"入库 {saved_items} 条",
                f"耗时 {duration_s} 秒",
            ]
            if failed_count:
                parts.append(f"{failed_count} 个平台失败")
            if skipped_count:
                parts.append(f"{skipped_count} 个平台未执行")
            message = "，".join(parts)
            error = str(run.get("error") or "")
            if error and final_status == RUN_STATUS_FAILED:
                message += f"（{error[:200]}）"
            event = {
                "type": event_type,
                "severity": severity,
                "dedupe_key": dedupe_key,
                "ts": now_ts(),
                "data": {
                    "run_id": rt.run_id,
                    "name": name,
                    "keywords_count": len(rt.keywords),
                    "platforms_total": len(platforms),
                    "platforms_ok": ok_count,
                    "platforms_failed": failed_count,
                    "platforms_skipped": skipped_count,
                    "saved_items": saved_items,
                    "duration_s": duration_s,
                    "error": error,
                    "platform": platform,
                    "account_id": account_id,
                    "account_alias": account_alias,
                    "report_id": "",
                    "version": 0,
                    "title": title,
                    "message": message,
                },
            }
            bus.publish(event)
            rt.sink.append(f"[sidecar] 已发布事件 {event_type}（dedupe_key={dedupe_key}）")
        except Exception:  # noqa: BLE001 - 通知事件失败不能影响采集任务收尾
            log.exception("发布 run 事件失败：%s", rt.run_id)

    def _run_platform_with_retries(
        self,
        rt: RunRuntime,
        platform: str,
        engine_account_id: str = "",
        profile_template: str = "",
        account_attempt: int = 1,
    ) -> None:
        """同一账号内执行一个平台；网络类瞬时失败自动重试（无需用户手动点）。

        自动重试只针对网络/浏览器连接类错误（ERR_CONNECTION_CLOSED、
        Page.goto、RetryError/DataFetchError、超时等），不重试未登录、
        扫码失败等需要人工处理的错误（账号类失败由 ``_run_one_platform`` 换号）。
        次数/间隔可用环境变量调整：
          SIDECAR_AUTO_RETRY_MAX   默认 2（首次失败后再试 2 次）
          SIDECAR_AUTO_RETRY_DELAY 默认 5 秒（之后 10s、20s...）
        """
        try:
            max_retries = int(os.environ.get("SIDECAR_AUTO_RETRY_MAX", "2"))
        except (TypeError, ValueError):
            max_retries = 2
        max_retries = max(0, min(max_retries, 5))
        try:
            base_delay = float(os.environ.get("SIDECAR_AUTO_RETRY_DELAY", "5"))
        except (TypeError, ValueError):
            base_delay = 5.0
        base_delay = max(1.0, min(base_delay, 30.0))

        attempt = 0
        while True:
            self._run_one_platform_attempt(
                rt, platform, engine_account_id, profile_template, account_attempt
            )
            if rt.stop_event.is_set():
                return
            row = self._platform_row(rt.run_id, platform)
            if row is None or row.get("status") != "failed":
                return
            message = str(row.get("message") or "")
            if attempt >= max_retries or not _is_retryable_crawl_error(message):
                return
            attempt += 1
            delay = base_delay * (2 ** (attempt - 1))
            label = PLATFORM_LABELS.get(platform, platform)
            retry_message = (
                f"网络波动，{int(delay)} 秒后自动重试（{attempt}/{max_retries}）：{message[:180]}"
            )
            self.state.update_platform(
                rt.run_id,
                platform,
                status="running",
                progress=_to_int(row.get("progress"), 0),
                stage="retry",
                saved_items=_to_int(row.get("saved_items"), 0),
                message=retry_message,
                finished_at=None,
            )
            rt.sink.append("")
            rt.sink.append(f"===== [{label}] 自动重试 {attempt}/{max_retries}，{int(delay)} 秒后重新采集 =====")
            rt.sink.append(f"      原因：{message[:500]}")
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                if rt.stop_event.wait(0.5):
                    return

    # -- 选号 / 换号 ------------------------------------------------------
    def _select_account(
        self,
        rt: RunRuntime,
        platform: str,
        attempt: int,
        tried: List[str],
    ) -> Dict[str, Any]:
        """为一个平台选号：手动策略/续采优先 → 自动选号 → 旧单账号兼容兜底。"""
        assert self.accounts is not None
        policy = rt.account_policy or {}
        mode = str(policy.get("mode") or "auto")
        strategy = str(policy.get("strategy") or self.accounts.default_strategy())
        manual_ids = policy.get("account_ids") or {}
        if attempt == 1 and rt.resume_accounts.get(platform):
            resume_id = str(rt.resume_accounts.get(platform))
            try:
                selected = self.accounts.pick_manual(platform, resume_id)
                selected["strategy"] = "resume"
                selected["reasons"] = ["续采：沿用上次采集账号"] + list(selected.get("reasons") or [])
                rt.sink.append(f"[sidecar] 续采：优先沿用原账号 {resume_id}")
                return selected
            except ServiceError as exc:
                rt.sink.append(f"[sidecar] 续采原账号 {resume_id} 不可用（{exc.message}），重新选号")
        if mode == "manual":
            account_id = str(manual_ids.get(platform) or "")
            if not account_id:
                raise ServiceError(
                    "NO_ACCOUNT", f"平台 {PLATFORM_LABELS.get(platform, platform)} 手动模式未指定账号", 409
                )
            selected = self.accounts.pick_manual(platform, account_id)
            selected["strategy"] = "manual"
            return selected
        try:
            return self.accounts.pick(platform, strategy=strategy, exclude=tried)
        except ServiceError as exc:
            if exc.code != "NO_ACCOUNT" or tried:
                raise
            fallback = self.accounts.legacy_fallback(platform, exclude=tried)
            if fallback is None:
                raise
            rt.sink.append(
                "[sidecar] 旧单账号兼容：legacy 默认账号登录态未确认，沿用旧行为继续采集"
            )
            return fallback

    def _run_one_platform(self, rt: RunRuntime, platform: str) -> None:
        """执行一个平台：选号 → 账号独占锁 → 采集（网络类同号重试）→ 失败分类换号。"""
        if self.accounts is None:
            # 兼容/单元测试未注入账号模块：沿用旧单账号编排
            self._run_platform_with_retries(rt, platform, "", "", 1)
            return
        try:
            max_attempts = int(os.environ.get("SIDECAR_ACCOUNT_MAX_ATTEMPTS", "2"))
        except (TypeError, ValueError):
            max_attempts = 2
        max_attempts = max(1, min(max_attempts, 5))
        tried: List[str] = []
        failover_aliases: List[str] = []
        last_alias = ""
        label = PLATFORM_LABELS.get(platform, platform)
        # 续采/同 run 重跑时 attempt 序号从已有最大值继续（避免覆盖 run_accounts 历史）
        attempt_base = self.accounts.max_attempt(rt.run_id, platform)
        for attempt in range(1, max_attempts + 1):
            if rt.stop_event.is_set():
                return
            attempt_no = attempt_base + attempt
            try:
                selected = self._select_account(rt, platform, attempt, tried)
            except ServiceError as exc:
                if not tried:
                    message = (
                        f"无可用账号：{exc.message}"
                        if exc.code == "NO_ACCOUNT"
                        else f"选号失败：{exc.message}"
                    )
                    self.state.update_platform(
                        rt.run_id,
                        platform,
                        status="failed",
                        message=message[:1000],
                        finished_at=now_ts(),
                    )
                    rt.sink.append(f"===== [{label}] {message} =====")
                break
            account = selected.get("account") or {}
            account_id = str(selected.get("account_id") or account.get("account_id") or "")
            alias = str(
                selected.get("alias") or account.get("display_alias") or account.get("alias") or account_id
            )
            last_alias = alias
            pick_reason = str(selected.get("strategy") or "auto")
            engine_args = self.accounts.engine_account_args(account)
            account_busy = (
                self.guard.acquire_account(account_id, rt.run_id) if account_id else None
            )
            if account_busy:
                message = f"账号 {alias} 正在被占用：{account_busy}"
                rt.sink.append(f"===== [{label}] {message} =====")
                if not tried:
                    self.state.update_platform(
                        rt.run_id,
                        platform,
                        status="failed",
                        message=message,
                        finished_at=now_ts(),
                    )
                break
            try:
                self.accounts.mark_attempt(
                    rt.run_id, platform, attempt_no, account_id, reason=pick_reason
                )
                rt.sink.append(
                    f"===== [{label}] 第 {attempt_no} 次尝试：账号 {alias}"
                    f"（{account_id or '-'}，原因={pick_reason}）====="
                )
                self._run_platform_with_retries(
                    rt,
                    platform,
                    str(engine_args.get("account_id") or ""),
                    str(engine_args.get("profile_template") or ""),
                    attempt_no,
                )
                row = self._platform_row(rt.run_id, platform) or {}
                status = str(row.get("status") or "failed")
                message = str(row.get("message") or "")
                saved = _to_int(row.get("saved_items"), 0)
                result = self.accounts.on_crawl_result(
                    account_id,
                    ok=(status == "done"),
                    message=message,
                    saved_items=saved if status == "done" else 0,
                    platform=platform,
                    run_id=rt.run_id,
                )
                kind = str(result.get("kind") or "")
                finish_status = "done" if status == "done" else (
                    kind if kind in ("login_invalid", "risk_blocked") else "failed"
                )
                self.accounts.finish_attempt(
                    rt.run_id, platform, attempt_no, finish_status, message, account_id
                )
                if status == "done":
                    break
                if kind in ("login_invalid", "risk_blocked") and attempt < max_attempts:
                    tried.append(account_id)
                    failover_aliases.append(alias)
                    rt.sink.append(
                        f"[sidecar] 账号 {alias} 失败（{kind}: {message[:120]}）→ 自动切换下一个账号"
                    )
                    continue
                break
            finally:
                if account_id:
                    self.guard.release_account(account_id, rt.run_id)
        if failover_aliases and not rt.stop_event.is_set():
            row = self._platform_row(rt.run_id, platform) or {}
            note = "已自动切换账号：" + "、".join(failover_aliases) + f" → {last_alias}"
            if row.get("status") in ("failed", "done"):
                merged = (str(row.get("message") or "") + f"（{note}）")[:1000]
                self.state.update_platform(rt.run_id, platform, message=merged)
                rt.sink.append(f"===== [{label}] {note} =====")

    def _platform_row(self, run_id: str, platform: str) -> Optional[Dict[str, Any]]:
        run = self.state.get_run(run_id) or {}
        for item in run.get("platforms") or []:
            if item.get("platform") == platform:
                return item
        return None

    def _run_one_platform_attempt(
        self,
        rt: RunRuntime,
        platform: str,
        engine_account_id: str = "",
        profile_template: str = "",
        account_attempt: int = 1,
    ) -> None:
        label = PLATFORM_LABELS.get(platform, platform)
        with rt.results_lock:
            baseline = _to_int(rt.saved_counts.get(platform, 0), 0)
        start_progress = (
            _calc_progress(baseline, rt.expected_items, "boot") if baseline else 0
        )
        start_message = "正在启动采集引擎…"
        if rt.resume and baseline:
            start_message = f"正在启动采集引擎…（续采，已保留历史 {baseline} 条）"
        if engine_account_id:
            start_message += f"（账号 {engine_account_id}，第 {account_attempt} 次尝试）"
        self.state.update_platform(
            rt.run_id,
            platform,
            status="running",
            progress=start_progress,
            stage="boot",
            saved_items=baseline,
            message=start_message,
            started_at=now_ts(),
        )
        with rt.results_lock:
            rt.platform_results.pop(platform, None)
        rt.sink.append("")
        rt.sink.append(
            f"===== [{label}] 开始采集 platform={platform} "
            f"account={engine_account_id or 'legacy'} attempt={account_attempt} ====="
        )
        try:
            proc = self.backend.start(
                platform,
                {
                    "keywords": rt.keywords,
                    "max_notes": _to_int(rt.params.get("max_notes"), 15),
                    "comments": bool(rt.params.get("comments", True)),
                    "max_comments": _to_int(rt.params.get("max_comments"), 20),
                    "account_id": engine_account_id,
                    "profile_template": profile_template,
                },
                sink=rt.sink,
                on_event=lambda evt: self._on_crawl_event(rt, platform, evt),
            )
        except EngineStartError as exc:
            self.state.update_platform(
                rt.run_id,
                platform,
                status="failed",
                message=f"引擎启动失败：{exc}",
                finished_at=now_ts(),
            )
            rt.sink.append(f"===== [{label}] 引擎启动失败：{exc} =====")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("启动采集引擎异常：%s", platform)
            self.state.update_platform(
                rt.run_id,
                platform,
                status="failed",
                message=f"引擎启动异常：{exc}",
                finished_at=now_ts(),
            )
            rt.sink.append(f"===== [{label}] 引擎启动异常：{exc} =====")
            return

        with rt.process_lock:
            rt.process = proc
        timed_out = False
        try:
            deadline = time.monotonic() + float(self.cfg.crawl_timeout)
            while True:
                if proc.wait(0.2):
                    break
                if rt.stop_event.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
            if proc.is_running():
                proc.terminate()
            proc.wait(10)
        finally:
            with rt.process_lock:
                rt.process = None

        if rt.stop_event.is_set():
            self.state.update_platform(
                rt.run_id,
                platform,
                status="failed",
                message="已取消",
                finished_at=now_ts(),
            )
            rt.sink.append(f"===== [{label}] 已取消 =====")
            return

        with rt.results_lock:
            finished_event = rt.platform_results.pop(platform, None)

        if timed_out:
            status = "failed"
            message = f"采集超时（>{int(self.cfg.crawl_timeout)} 秒），已终止"
        elif finished_event is None:
            status = "failed"
            message = f"引擎未返回 engine.finished 事件（进程退出码 {proc.returncode}）"
        elif bool(finished_event.get("ok")):
            status = "done"
            message = str(finished_event.get("message") or "采集完成")
        else:
            status = "failed"
            message = str(finished_event.get("message") or "采集失败")

        fields: Dict[str, Any] = {
            "status": status,
            "message": message,
            "finished_at": now_ts(),
        }
        if status == "done":
            fields["progress"] = 100
            fields["stage"] = "done"
        self.state.update_platform(rt.run_id, platform, **fields)
        rt.sink.append(f"===== [{label}] 结束：{status} {message} =====")

    def _on_crawl_event(self, rt: RunRuntime, platform: str, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        if event_type == "progress":
            stage = str(event.get("stage") or "")
            saved = _to_int(event.get("saved"), 0)
            with rt.results_lock:
                prev = rt.saved_counts.get(platform, 0)
                saved = max(prev, saved)
                rt.saved_counts[platform] = saved
            progress = _calc_progress(saved, rt.expected_items, stage)
            message = str(event.get("message") or "")
            fields: Dict[str, Any] = {
                "progress": progress,
                "stage": stage,
                "saved_items": saved,
            }
            if message:
                fields["message"] = message
            self.state.update_platform(rt.run_id, platform, **fields)
            return
        if event_type == "engine.finished":
            with rt.results_lock:
                rt.platform_results[platform] = dict(event)
            return
        if event_type == "engine.started":
            message = str(event.get("message") or "引擎已启动")
            self.state.update_platform(rt.run_id, platform, message=message)
            return

def _run_to_api(run: Dict[str, Any], aliases: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """契约 §2.2 ``GET /api/runs/{run_id}`` 响应（字段固定 + 账号化加法字段）。"""
    alias_map = aliases or {}
    platforms: List[Dict[str, Any]] = []
    for item in run["platforms"]:
        account_id = str(item.get("account_id") or "")
        platforms.append(
            {
                "platform": item["platform"],
                "label": PLATFORM_LABELS.get(item["platform"], item["platform"]),
                "status": item["status"],
                "progress": item["progress"],
                "stage": item["stage"],
                "saved_items": item["saved_items"],
                "message": item["message"],
                "started_at": item["started_at"],
                "finished_at": item["finished_at"],
                # 账号化加法字段（旧前端忽略即可）
                "account_id": account_id,
                "account_alias": alias_map.get(account_id, ""),
                "account_attempts": int(item.get("account_attempts") or 0),
            }
        )
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "created_at": run["created_at"],
        "finished_at": run["finished_at"],
        "params": run["params"],
        "account_policy": run.get("account_policy") or {},
        "platforms": platforms,
        "excel_path": run["excel_path"],
    }


def _run_to_summary(
    run: Dict[str, Any], aliases: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """``GET /api/runs`` 的 run 摘要。

    契约未固定摘要字段；为了让 desktop renderer 能直接复用任务详情渲染逻辑，
    这里返回与 ``GET /api/runs/{run_id}`` 同构的字段（platforms 为对象数组），
    并额外补 name/keywords/total_saved 便于列表展示。
    """
    summary = _run_to_api(run, aliases)
    summary["name"] = run.get("name") or ""
    summary["keywords"] = list(run["keywords"])
    summary["total_saved"] = sum(int(item.get("saved_items") or 0) for item in run["platforms"])
    return summary


# ---------------------------------------------------------------------------
# CrawlBackend：把「起子进程」从 RunManager 里抽掉（M2）
# ---------------------------------------------------------------------------
class CrawlHandle(Protocol):
    """一次采集进程的句柄（EngineProcess 天然满足；FakeProc 用于单测）。"""

    returncode: Optional[int]

    def wait(self, timeout: Optional[float] = None) -> bool: ...

    def is_running(self) -> bool: ...

    def terminate(self) -> None: ...


class CrawlBackend(Protocol):
    """采集后端协议：RunManager 只通过它启动/停止采集，不直接 new 进程。"""

    def start(
        self,
        platform: str,
        params: Dict[str, Any],
        sink: "LogSink",
        on_event: Callable[[Dict[str, Any]], None],
    ) -> CrawlHandle: ...

    def stop(self, handle: Optional[CrawlHandle]) -> None: ...


class EngineProcessBackend:
    """生产实现：把 ``EngineRunner.start_crawl`` 的 spawn 逻辑包成 CrawlBackend。"""

    def __init__(self, runner: Any) -> None:
        self.runner = runner

    def start(
        self,
        platform: str,
        params: Dict[str, Any],
        sink: "LogSink",
        on_event: Callable[[Dict[str, Any]], None],
    ) -> CrawlHandle:
        return self.runner.start_crawl(
            platform=platform,
            keywords=list(params.get("keywords") or []),
            max_notes=_to_int(params.get("max_notes"), 0),
            comments=bool(params.get("comments", True)),
            max_comments=_to_int(params.get("max_comments"), 0),
            sink=sink,
            on_event=on_event,
            account_id=str(params.get("account_id") or ""),
            profile_template=str(params.get("profile_template") or ""),
        )

    def stop(self, handle: Optional[CrawlHandle]) -> None:
        if handle is not None and handle.is_running():
            handle.terminate()


class FakeProc:
    """单测用进程句柄：``wait()`` 时按脚本回调事件，不启子进程。"""

    def __init__(
        self,
        events: Optional[List[Dict[str, Any]]] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        returncode: int = 0,
        manual: bool = False,
    ) -> None:
        self.returncode = int(returncode)
        self.events = list(events or [])
        self.on_event = on_event
        # manual=True：一直 running，直到测试/编排层调用 terminate()（模拟长跑进程，用于 stop 用例）
        self.manual = bool(manual)
        self.terminated = False
        self.emitted = False
        self.waits = 0

    def _emit(self) -> None:
        if self.emitted:
            return
        self.emitted = True
        if self.on_event is not None:
            for event in self.events:
                self.on_event(event)

    def wait(self, timeout: Optional[float] = None) -> bool:
        self.waits += 1
        if self.manual:
            return self.terminated
        self._emit()
        return True

    def is_running(self) -> bool:
        if self.manual:
            return not self.terminated
        return not self.terminated and not self.emitted

    def terminate(self) -> None:
        self.terminated = True


class FakeBackend:
    """单测用 CrawlBackend：按平台排队投放事件脚本（每次 start 消耗一条）。"""

    def __init__(
        self,
        plans: Optional[Dict[str, List[List[Dict[str, Any]]]]] = None,
        default_events: Optional[List[Dict[str, Any]]] = None,
        returncode: int = 0,
        manual_platforms: Optional[Any] = None,
    ) -> None:
        self.plans: Dict[str, List[List[Dict[str, Any]]]] = {
            str(platform): [list(item) for item in queue]
            for platform, queue in (plans or {}).items()
        }
        self.default_events = list(default_events or [])
        self.returncode = int(returncode)
        # 这些平台的句柄是 manual 的：不自动结束，用于测 stop/取消
        self.manual_platforms = set(manual_platforms or ())
        self.started: List[Tuple[str, Dict[str, Any]]] = []
        self.stopped: List[Optional[CrawlHandle]] = []
        self.handles: List[FakeProc] = []

    def start(
        self,
        platform: str,
        params: Dict[str, Any],
        sink: "LogSink",
        on_event: Callable[[Dict[str, Any]], None],
    ) -> CrawlHandle:
        self.started.append((platform, dict(params)))
        queue = self.plans.get(platform) or []
        events = queue.pop(0) if queue else list(self.default_events)
        proc = FakeProc(
            events,
            on_event,
            returncode=self.returncode,
            manual=str(platform) in self.manual_platforms,
        )
        self.handles.append(proc)
        return proc

    def stop(self, handle: Optional[CrawlHandle]) -> None:
        self.stopped.append(handle)
        if handle is not None and handle.is_running():
            handle.terminate()

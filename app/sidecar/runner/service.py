# -*- coding: utf-8 -*-
"""EngineService 组装（HTTP 层只依赖它）+ 导出/内容流加载器（M2 从 engine_runner.py 纯搬迁）。

依赖方向：service -> run/login/guard/process/config。
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from state import StateStore, now_ts
from version import APP_VERSION as SIDECAR_VERSION

from .config import (
    PLATFORM_LABELS,
    PLATFORMS,
    EngineStartError,
    ServiceError,
    SidecarConfig,
    _env_str,
    _to_int,
)
from .guard import BUSY_GLOBAL_MESSAGE, BUSY_PLATFORM_TEST_MESSAGE, ActivityGuard
from .login import LoginManager
from .process import EngineProcess, EngineRunner, LogSink
from .run import RUN_STATUS_RUNNING, EngineProcessBackend, RunManager

log = logging.getLogger("sidecar.engine")  # 沿用旧 logger 名：日志输出与拆分前逐字节一致


def load_exporter() -> Tuple[Optional[Callable[..., Any]], Optional[Callable[..., Any]], str]:
    """尝试加载同目录 exporter。

    契约 §3 要求 ``from exporter import export_run_excel/preview_run``；默认模块名
    就是 ``exporter``。selftest 可用 ``SIDECAR_EXPORTER_MODULE=_fake_exporter``
    （仅测试）注入替身，生产环境不受影响。

    返回 ``(export_fn, preview_fn, error)``；未就绪时两个函数为 None 并给出原因。
    """
    import importlib

    module_name = (_env_str("SIDECAR_EXPORTER_MODULE") or "exporter").strip()
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 缺 exporter 时 sidecar 仍要能启动
        return None, None, f"无法 import {module_name}：{exc}"
    export_fn = getattr(module, "export_run_excel", None)
    preview_fn = getattr(module, "preview_run", None)
    if not callable(export_fn):
        return None, preview_fn if callable(preview_fn) else None, (
            f"{module_name}.export_run_excel 不存在或不可调用"
        )
    return export_fn, preview_fn if callable(preview_fn) else None, ""


def load_run_feed() -> Tuple[Optional[Callable[..., Any]], str]:
    """尝试加载同目录 run_feed（「采集动态」内容流数据层）。

    默认模块名 ``run_feed``；selftest/联调可用 ``SIDECAR_RUN_FEED_MODULE`` 注入替身。
    返回 ``(build_run_feed_fn, error)``；未就绪时函数为 None 并给出原因（接口降级 503）。
    """
    import importlib

    module_name = (_env_str("SIDECAR_RUN_FEED_MODULE") or "run_feed").strip()
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 内容流未就绪时 sidecar 仍要能启动
        return None, f"无法 import {module_name}：{exc}"
    build_fn = getattr(module, "build_run_feed", None)
    if not callable(build_fn):
        return None, f"{module_name}.build_run_feed 不存在或不可调用"
    return build_fn, ""

class EngineService:
    """sidecar 全部业务能力的门面（main.py 的 endpoint 只调这里）。"""

    def __init__(self, cfg: SidecarConfig) -> None:
        self.cfg = cfg
        cfg.ensure_dirs()
        self.state = StateStore(cfg.app_db)
        recovered = self.state.recover_interrupted_runs()
        if recovered:
            log.warning("已恢复 %d 个上次中断的采集任务（running -> failed）", recovered)
        self.guard = ActivityGuard()
        self.runner = EngineRunner(cfg)
        # 多账号号池（account_manager 延迟导入，避免 engine_runner ↔ account_manager 循环导入）
        from account_manager import AccountManager

        self.accounts = AccountManager(cfg, self.state, self.guard)
        # 启动整形：为每个平台补齐默认账号（v2），并把「从未登录过」的 legacy 账号原地升级为 v2。
        # 有登录态的旧目录保持 legacy（legacy_fallback 继续可用），因此升级不丢 Cookie。
        try:
            self.accounts.ensure_all_default_accounts()
        except Exception:  # noqa: BLE001 - 账号整形失败不能拖垮服务启动
            log.exception("账号池启动整形失败（服务继续启动，账号页仍可懒创建）")
        self.logins = LoginManager(self.runner, self.state, self.guard, cfg, accounts=self.accounts)
        self.runs = RunManager(
            self.runner, self.state, self.guard, cfg, accounts=self.accounts,
            backend=EngineProcessBackend(self.runner),
        )
        self._export_fn, self._preview_fn, self._exporter_error = load_exporter()
        self._feed_fn, self._feed_error = load_run_feed()
        self._shutdown_lock = threading.Lock()
        self._shutdown_done = False

    # -- config / info ----------------------------------------------------
    def config_info(self) -> Dict[str, Any]:
        return {
            "data_dir": str(self.cfg.data_dir),
            "mc_root": str(self.cfg.mc_root),
            "python": str(self.cfg.python),
            "platform_labels": dict(PLATFORM_LABELS),
            # 契约外附加字段：便于 UI/验收脚本确认登录模式与超时策略
            "version": SIDECAR_VERSION,
            "login_headless": bool(self.cfg.login_headless),
            "login_timeout": int(self.cfg.login_timeout),
        }

    def system_info(self) -> Dict[str, Any]:
        return {
            "version": SIDECAR_VERSION,
            "data_dir": str(self.cfg.data_dir),
            "logs_dir": str(self.cfg.logs_dir),
        }

    # -- accounts ---------------------------------------------------------
    def list_accounts(self) -> List[Dict[str, Any]]:
        """``GET /api/accounts``：旧字段保持不变，加法追加账号池字段（契约 §6.2）。"""
        states = self.state.get_account_states()
        accounts: List[Dict[str, Any]] = []
        for platform, label in PLATFORMS:
            row = states.get(platform) or {}
            try:
                default = self.accounts.ensure_default_account(platform)
            except Exception:  # noqa: BLE001 - 账号模块异常不能拖垮旧接口
                log.exception("账号池懒适配失败：%s", platform)
                default = None
            platform_accounts = self.accounts.list(platform) if default is not None else []
            if default is not None:
                profile_exists = bool(default.get("profile_exists"))
            else:
                profile_dir = self.cfg.platform_profile_dir(platform)
                try:
                    profile_exists = profile_dir.is_dir() and any(profile_dir.iterdir())
                except OSError:
                    profile_exists = profile_dir.is_dir()
            logged_in = row.get("logged_in")
            if logged_in is None and not profile_exists:
                logged_in = None
            health_values = [int(item.get("health_score") or 0) for item in platform_accounts]
            accounts.append(
                {
                    # ---- 旧字段（一个都不能少，selftest/旧 renderer 依赖）----
                    "platform": platform,
                    "label": label,
                    "logged_in": logged_in,
                    "last_checked_at": row.get("last_checked_at"),
                    "profile_exists": bool(profile_exists),
                    "message": row.get("message") or "",
                    # ---- 账号池加法字段 ----
                    "account_count": len(platform_accounts),
                    "default_account_id": (default or {}).get("account_id", ""),
                    "default_account_alias": (default or {}).get("display_alias", ""),
                    "health": (
                        int(sum(health_values) / len(health_values)) if health_values else None
                    ),
                    "accounts": platform_accounts,
                }
            )
        return accounts

    def start_login(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        return self.logins.start(platform, account_id)

    def get_login(self, platform: str, login_id: str) -> Dict[str, Any]:
        return self.logins.get(platform, login_id)

    def cancel_login(self, platform: str, login_id: str) -> Dict[str, Any]:
        return self.logins.cancel(platform, login_id)

    def _is_platform_default(self, platform: str, account_id: str) -> bool:
        try:
            default = self.accounts.ensure_default_account(platform)
        except Exception:  # noqa: BLE001
            return False
        return str(default.get("account_id") or "") == str(account_id or "")

    def test_connection(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        """同步测试连接（HTTP 路由用同步 def → FastAPI 线程池，可多平台/多账号并行）。

        ``account_id`` 为空 → 平台默认账号（旧路由行为完全不变）。
        """
        account = self.accounts.resolve(platform, account_id or "")
        account_id_resolved = str(account.get("account_id") or "")
        engine_args = self.accounts.engine_account_args(account)
        engine_account_id = str(engine_args.get("account_id") or "")
        profile_template = str(engine_args.get("profile_template") or "")
        ref = f"test-{platform}-{uuid.uuid4().hex[:8]}"
        # 先占 test 槽（旧语义：platform 级；v2 账号：platform@account_id），再占账号独占锁
        busy = self.guard.acquire_test(platform, ref, account_id=engine_account_id)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        account_busy = (
            self.guard.acquire_account(account_id_resolved, ref) if account_id_resolved else None
        )
        if account_busy:
            self.guard.release_test(platform, ref, account_id=engine_account_id)
            raise ServiceError("ACCOUNT_BUSY", account_busy, 409)
        started_mono = time.monotonic()
        sink: Optional[LogSink] = None
        collected: Dict[str, Optional[Dict[str, Any]]] = {"test": None, "finished": None}
        collected_lock = threading.Lock()

        def on_event(event: Dict[str, Any]) -> None:
            event_type = str(event.get("type") or "")
            if event_type == "test.result":
                with collected_lock:
                    collected["test"] = dict(event)
            elif event_type == "engine.finished":
                with collected_lock:
                    collected["finished"] = dict(event)

        proc: Optional[EngineProcess] = None
        timed_out = False
        try:
            sink_suffix = EngineRunner._scope_suffix(engine_account_id)
            sink = LogSink(
                self.cfg.logs_dir / f"test-{platform}{sink_suffix}-{now_ts()}-{ref[-8:]}.log",
                scope=f"test-{platform}{sink_suffix}",
            )
            try:
                sink.append(
                    f"[sidecar] 测试连接 platform={platform} account={account_id_resolved or '-'} ref={ref}"
                )
                proc = self.runner.start_test(
                    platform,
                    sink,
                    on_event,
                    account_id=engine_account_id,
                    profile_template=profile_template,
                )
                deadline = time.monotonic() + float(self.cfg.test_timeout)
                while time.monotonic() < deadline:
                    if proc.wait(0.1):
                        break
                if proc.is_running():
                    timed_out = True
                    proc.terminate()
                proc.wait(10)
            except EngineStartError as exc:
                sink.append(f"[sidecar] 引擎启动失败：{exc}")
                duration_ms = int((time.monotonic() - started_mono) * 1000)
                self._update_account_after_test(
                    platform, account_id_resolved, False, f"引擎启动失败：{exc}"
                )
                return {
                    "ok": False,
                    "message": f"引擎启动失败：{exc}",
                    "duration_ms": duration_ms,
                    "account_id": account_id_resolved,
                    "account_alias": account.get("display_alias") or "",
                }
            except Exception as exc:  # noqa: BLE001
                log.exception("测试连接异常")
                duration_ms = int((time.monotonic() - started_mono) * 1000)
                self._update_account_after_test(
                    platform, account_id_resolved, False, f"测试异常：{exc}"
                )
                return {
                    "ok": False,
                    "message": f"测试异常：{exc}",
                    "duration_ms": duration_ms,
                    "account_id": account_id_resolved,
                    "account_alias": account.get("display_alias") or "",
                }

            duration_ms = int((time.monotonic() - started_mono) * 1000)
            network_error = False
            with collected_lock:
                test_event = collected["test"]
                finished_event = collected["finished"]
            if test_event is not None:
                ok = bool(test_event.get("ok"))
                network_error = bool(test_event.get("network_error"))
                message = str(test_event.get("message") or ("已登录" if ok else ("网络异常，登录态未确认" if network_error else "未登录")))
                engine_duration = _to_int(test_event.get("duration_ms"), 0)
                if engine_duration > 0:
                    duration_ms = engine_duration
            elif finished_event is not None:
                ok = bool(finished_event.get("ok"))
                message = str(finished_event.get("message") or ("检测完成" if ok else "检测失败"))
            else:
                ok = False
                message = "检测超时（90 秒）" if timed_out else (
                    f"引擎异常退出且未返回 test.result（退出码 {proc.returncode if proc else '无'}）"
                )
            sink.append(f"[sidecar] 测试连接结束：ok={ok} network_error={network_error} message={message}")
            self._update_account_after_test(
                platform,
                account_id_resolved,
                ok,
                message,
                network_error=network_error,
            )
            return {
                "ok": ok,
                "message": message,
                "duration_ms": duration_ms,
                "network_error": network_error,
                "account_id": account_id_resolved,
                "account_alias": account.get("display_alias") or "",
                "platform": platform,
            }
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception:  # noqa: BLE001
                    pass
            self.guard.release_test(platform, ref, account_id=engine_account_id)
            if account_id_resolved:
                self.guard.release_account(account_id_resolved, ref)

    def _update_account_after_test(
        self,
        platform: str,
        account_id: str,
        ok: bool,
        message: str,
        network_error: bool = False,
    ) -> None:
        """测试结果双写：accounts（真源）+ account_state（旧字段，仅平台默认账号）。"""
        try:
            if self.accounts is not None and account_id:
                self.accounts.on_test_result(
                    account_id,
                    ok,
                    message,
                    network_error=network_error,
                )
        except Exception:  # noqa: BLE001
            log.exception("写入账号测试结果失败：%s", account_id)
        if not account_id or not self._is_platform_default(platform, account_id):
            return
        try:
            if network_error:
                # 网络异常不能把账号标记成“未登录”，否则会误导用户重新扫码
                self.state.set_account_state(
                    platform,
                    logged_in=None,
                    last_checked_at=now_ts(),
                    message=message or "网络异常，登录态未确认",
                )
                return
            self.state.set_account_state(
                platform,
                logged_in=bool(ok),
                last_checked_at=now_ts(),
                message=("已登录" if ok else (message or "未登录")),
            )
        except Exception:  # noqa: BLE001
            log.exception("写入测试连接结果失败：%s", platform)

    def delete_account(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        """清除登录态/删除 profile（旧路由 = 平台默认账号；账号池 DELETE 走 archive）。"""
        account = self.accounts.resolve(platform, account_id or "")
        account_id_resolved = str(account.get("account_id") or "")
        # 保持全局 busy 拒绝：任意 login/run/test 进行中都不删除登录态；
        # 若恰是该平台在检测，用更明确的平台文案。
        if self.guard.is_exclusive_busy() or self.guard.is_test_busy():
            message = (
                BUSY_PLATFORM_TEST_MESSAGE
                if self.guard.is_test_busy(platform)
                else BUSY_GLOBAL_MESSAGE
            )
            raise ServiceError("BUSY", message, 409)
        # 占用平台槽位 + 账号独占锁，防「检查后、删除前」并发插入 test/run
        ref = f"delete-{platform}-{uuid.uuid4().hex[:6]}"
        busy = self.guard.acquire_test(platform, ref)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        account_busy = (
            self.guard.acquire_account(account_id_resolved, ref) if account_id_resolved else None
        )
        if account_busy:
            self.guard.release_test(platform, ref)
            raise ServiceError("ACCOUNT_BUSY", account_busy, 409)
        try:
            profile_dir = self.accounts.profile_path(account)
            # 账号化：删目录前先按 profile marker 杀掉残留 Edge/Chrome（避免 LOCK 占用导致 DELETE_FAILED）
            engine_args = self.accounts.engine_account_args(account)
            try:
                killed = self.runner.kill_orphan_browsers(
                    platform,
                    account_id=str(engine_args.get("account_id") or ""),
                    profile_template=str(engine_args.get("profile_template") or ""),
                )
                if killed:
                    log.info("清除登录态前已清理残留浏览器 %s 个（platform=%s）", len(killed), platform)
                    time.sleep(0.3)
            except Exception:  # noqa: BLE001 - 查杀失败仍继续尝试删除
                log.exception("清理残留浏览器失败（继续删除）：%s", platform)
            # Windows 下 Edge 退出后 profile 里的 LOCK 文件可能短暂占用，重试删除
            last_error: Optional[Exception] = None
            for attempt in range(3):
                try:
                    if profile_dir.exists():
                        shutil.rmtree(profile_dir, ignore_errors=False)
                    last_error = None
                    break
                except OSError as exc:
                    last_error = exc
                    time.sleep(0.3 * (attempt + 1))
            if last_error is not None or (profile_dir.exists() and any(profile_dir.iterdir())):
                detail = str(last_error) if last_error is not None else "目录仍存在"
                raise ServiceError("DELETE_FAILED", f"删除登录态失败：{detail}", 500)
            self.state.set_account_state(
                platform,
                logged_in=False,
                last_checked_at=now_ts(),
                profile_exists=False,
                message="已清除登录态",
            )
            try:
                self.accounts.update(
                    account_id_resolved,
                    login_state="unknown",
                    status="need_login",
                    risk_note="已清除登录态",
                )
            except Exception:  # noqa: BLE001
                log.exception("写入账号清除登录态失败：%s", account_id_resolved)
            return {"ok": True, "account_id": account_id_resolved}
        finally:
            self.guard.release_test(platform, ref)
            if account_id_resolved:
                self.guard.release_account(account_id_resolved, ref)

    # -- runs -------------------------------------------------------------
    def create_run(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.runs.create(params)

    def resume_run(self, run_id: str, platforms: Optional[List[str]] = None) -> Dict[str, Any]:
        return self.runs.resume(run_id, platforms)

    def list_runs(self) -> List[Dict[str, Any]]:
        return self.runs.list_runs()

    def get_run(self, run_id: str) -> Dict[str, Any]:
        return self.runs.get_run(run_id)

    def get_run_logs(
        self, run_id: str, tail: int = 200, after_line: Optional[int] = None
    ) -> Dict[str, Any]:
        """技术日志（契约 §2.2 ``GET /api/runs/{run_id}/logs``）。

        ``after_line`` 为 0 基行号：不传 → 尾部 ``tail`` 行（现状语义）；传 →
        只返回该行之后的新行。响应固定 ``{lines, next_line}``（``next_line`` 为 M5a 加法字段）。
        """
        lines, next_line = self.runs.logs(run_id, tail, after_line)
        return {"lines": lines, "next_line": next_line}

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        return self.runs.stop(run_id)

    def export_run(self, run_id: str) -> Dict[str, Any]:
        run_raw = self.runs.get_run_raw(run_id)
        if run_raw["status"] == RUN_STATUS_RUNNING:
            raise ServiceError("RUN_RUNNING", "任务仍在运行，结束后才能导出", 409)
        if self._export_fn is None:
            raise ServiceError(
                "EXPORTER_UNAVAILABLE",
                f"导出组件未就绪：{self._exporter_error or 'exporter.py 尚未提供'}",
                503,
            )
        try:
            result = self._export_fn(
                str(self.cfg.media_db),
                str(self.cfg.data_dir),
                run_id,
                list(run_raw["keywords"]),
                int(run_raw["started_ts"]),
            )
        except Exception as exc:  # noqa: BLE001 - exporter 内部错误统一转 500
            log.exception("导出失败：%s", run_id)
            raise ServiceError("EXPORT_FAILED", f"导出失败：{exc}", 500) from exc
        if not isinstance(result, dict):
            raise ServiceError("EXPORT_FAILED", "导出组件返回格式非法", 500)
        file_path = result.get("file_path")
        if not file_path:
            raise ServiceError("EXPORT_FAILED", "导出组件未返回 file_path", 500)
        path = Path(str(file_path))
        if not path.is_absolute():
            path = (self.cfg.data_dir / path).resolve()
        if not path.is_file():
            raise ServiceError("EXPORT_FAILED", f"导出文件未生成：{path}", 500)
        sheets = result.get("sheets") or []
        total_rows = _to_int(result.get("total_rows"), 0)
        if total_rows <= 0 and isinstance(sheets, list):
            total_rows = sum(_to_int(item.get("rows"), 0) for item in sheets if isinstance(item, dict))
        self.state.set_excel_path(run_id, str(path))
        return {
            "ok": True,
            "file_path": str(path),
            "sheets": sheets,
            "total_rows": total_rows,
        }

    def preview_run(self, run_id: str, limit: int = 20) -> Dict[str, Any]:
        run_raw = self.runs.get_run_raw(run_id)
        if self._preview_fn is None:
            raise ServiceError(
                "EXPORTER_UNAVAILABLE",
                f"导出组件未就绪：{self._exporter_error or 'exporter.py 尚未提供'}",
                503,
            )
        try:
            result = self._preview_fn(
                str(self.cfg.media_db),
                list(run_raw["keywords"]),
                int(run_raw["started_ts"]),
                max(1, min(int(limit or 20), 200)),
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("数据预览失败：%s", run_id)
            raise ServiceError("PREVIEW_FAILED", f"数据预览失败：{exc}", 500) from exc
        if not isinstance(result, dict) or "sheets" not in result:
            return {"sheets": []}
        return {"sheets": result.get("sheets") or []}

    def get_run_feed(
        self, run_id: str, limit: int = 30, comments: int = 3, since_seq: Optional[int] = None
    ) -> Dict[str, Any]:
        """「采集动态」内容流（契约 §2.2 ``GET /api/runs/{run_id}/feed``）。

        - run 不存在 → 404 RUN_NOT_FOUND（沿用现有错误体）；
        - DB/表/列缺失、脏数据由 run_feed.build_run_feed 降级为空内容流，不报 500；
        - limit clamp 1–100（默认 30），comments clamp 0–10（默认 3）；
        - ``since_seq``（M5a）：不传 = 首屏全量语义；传 = 只返回 ``seq > since_seq`` 的新条目
          并带 ``next_seq``（``counts``/``comment_total`` 始终全量）。
        """
        run_raw = self.runs.get_run_raw(run_id)
        if self._feed_fn is None:
            raise ServiceError(
                "FEED_UNAVAILABLE",
                f"采集动态组件未就绪：{self._feed_error or 'run_feed.py 尚未提供'}",
                503,
            )
        limit_int = max(1, min(_to_int(limit, 30), 100))
        comment_limit = max(0, min(_to_int(comments, 3), 10))
        try:
            result = self._feed_fn(
                str(self.cfg.media_db),
                run_id=str(run_id),
                status=str(run_raw["status"]),
                keywords=list(run_raw["keywords"]),
                started_ts=run_raw["started_ts"],
                limit=limit_int,
                comments=comment_limit,
                since_seq=since_seq,
            )
        except Exception as exc:  # noqa: BLE001 - 内容流内部错误统一转 500（DB 缺失不在此列）
            log.exception("采集动态读取失败：%s", run_id)
            raise ServiceError("FEED_FAILED", f"采集动态读取失败：{exc}", 500) from exc
        if not isinstance(result, dict):
            raise ServiceError("FEED_FAILED", "采集动态组件返回格式非法", 500)
        return result

    # -- shutdown ---------------------------------------------------------
    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._shutdown_done:
                return
            self._shutdown_done = True
        log.info("sidecar 正在关闭：停止登录会话与采集任务…")
        try:
            self.logins.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("关闭登录会话失败")
        try:
            self.runs.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("关闭采集任务失败")
        try:
            self.state.close()
        except Exception:  # noqa: BLE001
            log.exception("关闭 app.db 失败")

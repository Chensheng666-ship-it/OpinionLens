# -*- coding: utf-8 -*-
"""登录会话状态机与登录管理（M2 从 engine_runner.py 纯搬迁）。

依赖方向：login -> process/config；不反向依赖 run/service。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from state import StateStore, now_ts

from .config import DEFAULT_LOGIN_TIMEOUT, EngineStartError, ServiceError, SidecarConfig
from .process import (
    EngineProcess,
    EngineRunner,
    LogSink,
    default_need_manual_message,
    infer_need_manual_kind,
)

if TYPE_CHECKING:
    # 仅用于 __init__ 的注解：guard 是叶子模块，运行期不引入新的依赖边
    from .guard import ActivityGuard

log = logging.getLogger("sidecar.engine")  # 沿用旧 logger 名：日志输出与拆分前逐字节一致


LOGIN_TERMINAL_STATUSES = frozenset({"confirmed", "failed", "expired", "cancelled"})
# engine login.status -> API status。
# need_manual = 平台要求人工接管（滑块/拼图/短信验证码），不是失败，UI 需提示用户去浏览器窗口完成。
_ENGINE_LOGIN_STATUS_MAP = {
    "waiting_qr": "waiting_qr",
    "waiting_scan": "waiting_scan",
    "need_manual": "need_manual",
    "confirmed": "confirmed",
    "expired": "expired",
    "failed": "failed",
}

LOGIN_WATCHDOG_GRACE = 120

@dataclass
class LoginSession:
    login_id: str
    platform: str
    # 账号化（契约 §2.3.1：key 仍是 login_id，只给 session 加 account 字段）
    account_id: str = ""
    account_alias: str = ""
    engine_account_id: str = ""
    profile_template: str = ""
    status: str = "starting"
    qr: Optional[Dict[str, Any]] = None
    message: str = ""
    started_at: int = field(default_factory=now_ts)
    finished_at: Optional[int] = None
    process: Optional[EngineProcess] = None
    sink: Optional[LogSink] = None
    stop_requested: bool = False
    released: bool = False
    # 人工接管（滑块/拼图/短信验证码）：最近一次 need_manual 信息，粘性保留到终态
    kind: str = ""
    need_manual: Optional[Dict[str, Any]] = None
    need_manual_count: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def is_terminal(self) -> bool:
        with self.lock:
            return self.status in LOGIN_TERMINAL_STATUSES

    def to_api(self) -> Dict[str, Any]:
        with self.lock:
            qr = None
            if self.qr:
                qr = {
                    "seq": self.qr.get("seq"),
                    "image": self.qr.get("image"),
                    "file": self.qr.get("file"),
                }
            need_manual = dict(self.need_manual) if self.need_manual else None
            return {
                "login_id": self.login_id,
                "platform": self.platform,
                "account_id": self.account_id,
                "account_alias": self.account_alias,
                "status": self.status,
                "qr": qr,
                "message": self.message,
                "started_at": self.started_at,
                # 契约外附加字段（任务要求）：人工接管信息供 UI 展示
                "kind": self.kind,
                "need_manual": need_manual,
                "manual_required": self.status == "need_manual",
            }


class LoginManager:
    """登录会话状态机：spawn login 模式 engine，事件驱动状态流转。"""

    def __init__(
        self,
        runner: EngineRunner,
        state: StateStore,
        guard: ActivityGuard,
        cfg: SidecarConfig,
        accounts: Optional[Any] = None,
    ) -> None:
        self.runner = runner
        self.state = state
        self.guard = guard
        self.cfg = cfg
        self.accounts = accounts
        self._lock = threading.RLock()
        self._sessions: Dict[str, LoginSession] = {}
        self._active_login_id: Optional[str] = None
        self._timers: Dict[str, threading.Timer] = {}
        self._release_timers: Dict[str, threading.Timer] = {}

    # -- 对外接口 ---------------------------------------------------------
    def _resolve_account(self, platform: str, account_id: str = "") -> Optional[Dict[str, Any]]:
        if self.accounts is None:
            return None
        try:
            return self.accounts.resolve(platform, account_id or "")
        except ServiceError:
            raise
        except Exception:  # noqa: BLE001 - 账号模块异常时退回旧单账号行为
            log.exception("解析账号失败（退回旧单账号行为）：platform=%s account_id=%s", platform, account_id)
            return None

    def start(self, platform: str, account_id: str = "") -> Dict[str, Any]:
        login_id = "login_" + uuid.uuid4().hex[:12]
        account = self._resolve_account(platform, account_id)
        account_id_resolved = str(account["account_id"]) if account else ""
        engine_args = (
            self.accounts.engine_account_args(account)
            if (self.accounts is not None and account is not None)
            else {"account_id": "", "profile_template": ""}
        )
        session = LoginSession(
            login_id=login_id,
            platform=platform,
            account_id=account_id_resolved,
            account_alias=str(account.get("display_alias") or account.get("alias") or "") if account else "",
            engine_account_id=str(engine_args.get("account_id") or ""),
            profile_template=str(engine_args.get("profile_template") or ""),
        )
        busy = self.guard.acquire_exclusive("login", login_id, platform=platform)
        if busy:
            raise ServiceError("BUSY", busy, 409)
        account_busy = (
            self.guard.acquire_account(account_id_resolved, login_id)
            if account_id_resolved
            else None
        )
        if account_busy:
            self.guard.release_exclusive(login_id)
            raise ServiceError("ACCOUNT_BUSY", account_busy, 409)
        with self._lock:
            self._prune()
            self._sessions[login_id] = session
            self._active_login_id = login_id
        try:
            suffix = self.runner._scope_suffix(session.engine_account_id)
            sink_name = (
                f"login-{platform}{suffix}-{login_id}.log"
                if session.engine_account_id
                else f"login-{platform}-{login_id}.log"
            )
            session.sink = LogSink(
                self.cfg.logs_dir / sink_name,
                scope=f"login-{platform}{suffix}",
            )
            self._start_timer(session)
            session.process = self.runner.start_login(
                platform,
                session.sink,
                lambda evt: self._handle_event(session, evt),
                account_id=session.engine_account_id,
                profile_template=session.profile_template,
            )
            # 监听进程退出：兜底「引擎崩溃/被杀但没发 engine.finished」的会话状态，
            # 并在进程真正退出后才释放互斥（避免 confirmed 后浏览器尚未关闭时被 run 抢用 profile）
            threading.Thread(
                target=self._watch_process,
                args=(session, session.process),
                name=f"login-watch-{login_id}",
                daemon=True,
            ).start()
        except EngineStartError as exc:
            self._finish(
                session,
                status="failed",
                message=f"引擎启动失败：{exc}",
                terminate_process=False,
            )
        except Exception as exc:  # noqa: BLE001 - 任何启动异常都要落到会话状态中
            log.exception("登录引擎启动异常")
            self._finish(
                session,
                status="failed",
                message=f"引擎启动异常：{exc}",
                terminate_process=False,
            )
        # 契约要求立即返回 {login_id, status:"starting"}
        return {"login_id": login_id, "status": "starting"}

    def get(self, platform: str, login_id: str) -> Dict[str, Any]:
        session = self._get_session(platform, login_id)
        return session.to_api()

    def cancel(self, platform: str, login_id: str) -> Dict[str, Any]:
        session = self._get_session(platform, login_id)
        with session.lock:
            already_terminal = session.status in LOGIN_TERMINAL_STATUSES
        if not already_terminal:
            self._finish(session, status="cancelled", message="已取消登录")
        proc = session.process
        if proc is not None and proc.is_running():
            proc.terminate()
        # 有头登录会拉起可见 Edge；引擎树被杀后仍可能残留浏览器进程，这里按 profile 兜底清理
        self._cleanup_browsers(session)
        self._write_session_log(session, "取消登录：已终止引擎进程树并清理该平台浏览器进程")
        return {"ok": True}

    def shutdown(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            if session.is_terminal():
                continue
            try:
                self._finish(session, status="cancelled", message="sidecar 关闭，登录已取消")
                if session.process is not None:
                    session.process.terminate()
                self._cleanup_browsers(session)
            except Exception:  # noqa: BLE001
                pass

    # -- 内部实现 ---------------------------------------------------------
    def _get_session(self, platform: str, login_id: str) -> LoginSession:
        with self._lock:
            session = self._sessions.get(login_id)
        if session is None or session.platform != platform:
            raise ServiceError("LOGIN_NOT_FOUND", f"登录会话不存在：{login_id}", 404)
        return session

    def _prune(self) -> None:
        """保留最近 50 个会话，避免内存无限增长（调用方已持锁）。"""
        if len(self._sessions) <= 50:
            return
        for login_id in list(self._sessions.keys()):
            session = self._sessions[login_id]
            if session.is_terminal() and login_id != self._active_login_id:
                del self._sessions[login_id]
                timer = self._timers.pop(login_id, None)
                if timer is not None:
                    timer.cancel()
                release_timer = self._release_timers.pop(login_id, None)
                if release_timer is not None:
                    release_timer.cancel()
                if session.sink is not None:
                    session.sink.close()
            if len(self._sessions) <= 50:
                break

    def _cleanup_browsers(self, session: LoginSession) -> List[int]:
        """杀掉该账号 profile 残留的 msedge/chrome（有头登录取消/崩溃后的兜底）。"""
        try:
            killed = self.runner.kill_orphan_browsers(
                session.platform,
                account_id=session.engine_account_id,
                profile_template=session.profile_template,
            )
        except Exception:  # noqa: BLE001
            log.exception("清理残留浏览器进程失败：%s", session.platform)
            return []
        if killed:
            self._write_session_log(
                session,
                f"已清理残留浏览器进程 {len(killed)} 个（platform={session.platform} "
                f"account={session.account_id or '-'}）：{killed[:12]}",
            )
        return killed

    def _start_timer(self, session: LoginSession) -> None:
        # 有头登录允许用户在浏览器里完成短信/滑块验证，engine 自身 --timeout 已放宽；
        # 这里再加宽限，避免 sidecar 比 engine 先放弃。
        delay = float(self.cfg.login_timeout) + float(LOGIN_WATCHDOG_GRACE)
        timer = threading.Timer(delay, self._on_watchdog, args=(session.login_id,))
        timer.daemon = True
        with self._lock:
            self._timers[session.login_id] = timer
        timer.start()

    def _on_watchdog(self, login_id: str) -> None:
        with self._lock:
            session = self._sessions.get(login_id)
        if session is None or session.is_terminal():
            return
        self._finish(session, status="failed", message="登录超时：引擎未返回结果")
        if session.process is not None:
            session.process.terminate()
        self._cleanup_browsers(session)

    def _watch_process(self, session: LoginSession, proc: EngineProcess) -> None:
        """登录引擎进程退出后的兜底：补终态 + 清理浏览器 + 释放互斥（幂等）。"""
        try:
            proc.wait()
        except Exception:  # noqa: BLE001
            pass
        try:
            # 引擎退出（正常/崩溃）后清理可能残留的可见浏览器窗口
            self._cleanup_browsers(session)
            with session.lock:
                terminal = session.status in LOGIN_TERMINAL_STATUSES
                current = session.status
            if not terminal:
                self._finish(
                    session,
                    status="failed",
                    message=f"登录引擎异常退出（退出码 {proc.returncode}），未返回结果",
                )
            elif current == "confirmed":
                self._write_session_log(session, "登录引擎已退出，浏览器/profile 已释放")
        finally:
            self._release(session)

    def _schedule_confirm_release(self, session: LoginSession, delay: float = 15.0) -> None:
        """confirmed 后若引擎迟迟不退出（浏览器收尾卡住），超时强制释放互斥并杀进程。"""
        timer = threading.Timer(delay, self._force_release_confirmed, args=(session.login_id,))
        timer.daemon = True
        with self._lock:
            old = self._release_timers.pop(session.login_id, None)
            if old is not None:
                old.cancel()
            self._release_timers[session.login_id] = timer
        timer.start()

    def _force_release_confirmed(self, login_id: str) -> None:
        with self._lock:
            session = self._sessions.get(login_id)
            self._release_timers.pop(login_id, None)
        if session is None:
            return
        self._write_session_log(session, "confirmed 后引擎退出超时，强制释放互斥并终止引擎进程")
        proc = session.process
        if proc is not None and proc.is_running():
            proc.terminate()
        self._cleanup_browsers(session)
        self._release(session)

    def _cancel_timer(self, session: LoginSession) -> None:
        with self._lock:
            timer = self._timers.pop(session.login_id, None)
        if timer is not None:
            timer.cancel()

    def _cancel_release_timer(self, session: LoginSession) -> None:
        with self._lock:
            timer = self._release_timers.pop(session.login_id, None)
        if timer is not None:
            timer.cancel()

    def _release(self, session: LoginSession) -> None:
        with session.lock:
            if session.released:
                return
            session.released = True
        self.guard.release_exclusive(session.login_id)
        if session.account_id:
            self.guard.release_account(session.account_id, session.login_id)
        self._cancel_timer(session)
        self._cancel_release_timer(session)

    def _finish(
        self,
        session: LoginSession,
        status: str,
        message: str,
        terminate_process: bool = False,
        account_invalid: bool = False,
    ) -> None:
        with session.lock:
            if session.status in LOGIN_TERMINAL_STATUSES:
                # 已终态：只补 message（例如 confirmed 后 engine.finished 带 info）
                if message and not session.message:
                    session.message = message
                return
            session.status = status
            session.message = message
            session.finished_at = now_ts()
            if session.need_manual:
                session.need_manual["active"] = False
        # 登录失败/过期：同步账号真源（need_login，避免下次被自动选中）
        if account_invalid and session.account_id and self.accounts is not None:
            try:
                self.accounts.on_login_failed(session.account_id, message or status)
            except Exception:  # noqa: BLE001
                log.exception("写入账号登录失败状态失败：%s", session.account_id)
        # confirmed 时浏览器/profile 可能还在收尾：互斥保留到引擎进程真正退出（_watch_process 释放）；
        # 另加 15s 安全兜底，防止引擎退出卡死导致后续 login/run 一直 BUSY。
        process_still_running = (
            status == "confirmed"
            and session.process is not None
            and session.process.is_running()
        )
        if process_still_running:
            self._cancel_timer(session)
            self._schedule_confirm_release(session)
        else:
            self._release(session)
        if terminate_process and session.process is not None:
            session.process.terminate()
        self._write_session_log(session, f"登录会话结束：{status} {message}")
        with self._lock:
            if self._active_login_id == session.login_id:
                self._active_login_id = None

    def _write_session_log(self, session: LoginSession, text: str) -> None:
        if session.sink is not None:
            session.sink.append(f"[sidecar] {text}")

    # -- 事件处理 ---------------------------------------------------------
    def _apply_need_manual(self, session: LoginSession, event: Dict[str, Any]) -> None:
        """处理 engine 的 need_manual（滑块/拼图/短信验证码）：状态置 need_manual 并保留信息。

        该状态不是失败：engine 会在用户于可见浏览器窗口完成验证后继续登录流程，
        期间可能周期发送 waiting_scan；本状态机保持 need_manual 粘性，直到终态。
        """
        kind = infer_need_manual_kind(event)
        message = str(event.get("message") or "").strip() or default_need_manual_message(kind)
        prev_kind = ""
        prev_status = ""
        with session.lock:
            if session.status in LOGIN_TERMINAL_STATUSES:
                return
            prev_kind = session.kind
            prev_status = session.status
            session.status = "need_manual"
            session.kind = kind
            session.message = message
            session.need_manual_count += 1
            session.need_manual = {
                "kind": kind,
                "message": message,
                "ts": now_ts(),
                "active": True,
                "count": session.need_manual_count,
            }
        if prev_status != "need_manual" or prev_kind != kind:
            self._write_session_log(
                session,
                f"平台需要人工接管（need_manual）：kind={kind or 'unknown'} message={message}",
            )
            # 通知事件（契约 §2.1）：首次进入 / 换验证类型时发布；同 kind 重复轮询不重复发
            if self.accounts is not None:
                try:
                    self.accounts.publish_login_need_manual(
                        session.login_id,
                        session.platform,
                        session.account_id,
                        kind=kind,
                        message=message,
                    )
                except Exception:  # noqa: BLE001 - 通知失败不影响登录状态机
                    log.exception("发布 login.need_manual 事件失败：%s", session.login_id)

    def _handle_event(self, session: LoginSession, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "")
        with session.lock:
            terminal = session.status in LOGIN_TERMINAL_STATUSES
        if terminal and event_type != "engine.finished":
            return

        # 兼容两种写法：login.status{status:need_manual} 或 login.need_manual
        if event_type == "login.need_manual":
            self._apply_need_manual(session, event)
            return

        if event_type == "login.qrcode":
            qr = {
                "seq": event.get("seq"),
                "image": event.get("image"),
                "file": event.get("file"),
            }
            with session.lock:
                session.qr = qr
                # 二维码（重新）出现说明页面已回到扫码流程：允许从 need_manual 回到 waiting_qr，
                # 但保留 need_manual 历史信息（active=False）供 UI 排查。
                if session.status not in LOGIN_TERMINAL_STATUSES:
                    session.status = "waiting_qr"
                if session.need_manual:
                    session.need_manual["active"] = False
                if not session.message:
                    session.message = "请使用手机 App 扫码登录"
            self._write_session_log(
                session,
                f"收到二维码 seq={qr.get('seq')} bytes={event.get('bytes')} file={qr.get('file')}",
            )
            return

        if event_type == "login.status":
            engine_status = str(event.get("status") or "")
            message = str(event.get("message") or "")
            mapped = _ENGINE_LOGIN_STATUS_MAP.get(engine_status)
            if mapped is None:
                self._write_session_log(session, f"忽略未知 login.status：{engine_status}")
                return
            if mapped == "need_manual":
                self._apply_need_manual(session, event)
                return
            if mapped in LOGIN_TERMINAL_STATUSES:
                self._finish(
                    session,
                    status=mapped,
                    message=message or self._default_message(mapped),
                    account_invalid=mapped in ("failed", "expired"),
                )
                if mapped == "confirmed" and session.status == "confirmed":
                    self._mark_account_confirmed(session)
            else:
                with session.lock:
                    if session.status in LOGIN_TERMINAL_STATUSES:
                        pass
                    elif session.status == "need_manual":
                        # 人工接管期间 engine 可能周期发 waiting_scan 提示：保持 need_manual 粘性；
                        # 验证完成后 engine 会发「验证已完成，继续登录…」，此时恢复正常状态。
                        if message and ("验证已完成" in message or "继续登录" in message):
                            session.status = mapped
                            session.message = message
                            if session.need_manual:
                                session.need_manual["active"] = False
                            self._write_session_log(session, f"人工验证完成，状态恢复为 {mapped}：{message}")
                        else:
                            if session.need_manual:
                                session.need_manual["active"] = True
                                session.need_manual["last_poll_ts"] = now_ts()
                    else:
                        session.status = mapped
                        if message:
                            session.message = message
                        elif not session.message:
                            session.message = self._default_message(mapped)
            return

        if event_type == "engine.finished":
            ok = bool(event.get("ok"))
            message = str(event.get("message") or "")
            with session.lock:
                current = session.status
                stop_requested = session.stop_requested
            if current in LOGIN_TERMINAL_STATUSES:
                if current == "confirmed":
                    self._mark_account_confirmed(session)
                return
            if stop_requested:
                self._finish(session, status="cancelled", message=message or "已取消登录")
            elif ok:
                self._finish(session, status="confirmed", message=message or "登录成功")
                if session.status == "confirmed":
                    self._mark_account_confirmed(session)
            else:
                low = message.lower()
                if "cancel" in low or "取消" in message:
                    self._finish(session, status="cancelled", message=message)
                elif "expire" in low or "过期" in message or "超时" in message or "timeout" in low:
                    self._finish(
                        session,
                        status="expired",
                        message=message or "二维码已过期",
                        account_invalid=True,
                    )
                else:
                    self._finish(
                        session,
                        status="failed",
                        message=message or "登录失败",
                        account_invalid=True,
                    )
            return

        # engine.started 等无状态事件：记录到日志方便排查
        self._write_session_log(session, f"事件 {event_type}: {json.dumps(event, ensure_ascii=False)[:300]}")

    @staticmethod
    def _default_message(status: str) -> str:
        return {
            "starting": "正在启动登录引擎…",
            "waiting_qr": "请使用手机 App 扫码登录",
            "waiting_scan": "已扫码，请在手机端确认",
            "need_manual": default_need_manual_message(""),
            "confirmed": "登录成功",
            "expired": "二维码已过期，请重新发起登录",
            "failed": "登录失败",
            "cancelled": "已取消登录",
        }.get(status, "")

    def _mark_account_confirmed(self, session: LoginSession) -> None:
        try:
            self.state.set_account_state(
                session.platform,
                logged_in=True,
                last_checked_at=now_ts(),
                profile_exists=True,
                message="已登录",
            )
        except Exception:  # noqa: BLE001
            log.exception("写入平台登录态失败：%s", session.platform)
        # 账号真源（accounts）：登录成功 → login_state=ok / status=active / last_login_at
        if session.account_id and self.accounts is not None:
            try:
                self.accounts.on_login_success(session.account_id)
            except Exception:  # noqa: BLE001
                log.exception("写入账号登录成功状态失败：%s", session.account_id)

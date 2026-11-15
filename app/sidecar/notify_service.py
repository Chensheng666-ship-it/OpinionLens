# -*- coding: utf-8 -*-
"""通知服务（舆见 OpinionLens）——事件 → outbox → 后台 worker → 通道发送。

完整链路（契约 §2.1 / §3 / §4）：::

    EventBus.publish(event)
        └─ NotifyService.on_event(event)          # 同步、极轻量，绝不拖垮采集
             ├─ 规则匹配（notify_rules：event_type / min_severity / channel_ids / conditions）
             ├─ 静默时段（notify.quiet_hours / notify.muted_until，error 穿透）
             ├─ 去重（notify_outbox.dedupe_key 唯一索引，幂等）
             ├─ 渲染（render_event：默认 privacy=minimal，不含关键词原文/昵称/正文/本地路径）
             └─ INSERT notify_outbox(status=pending) + 唤醒 worker
    NotifierWorker（单线程 daemon）
        └─ 认领 pending → 令牌桶（10 条/分/通道）→ notify_channels.send()
             ├─ 成功       → status=sent + notify_log(ok)
             ├─ 永久失败   → status=failed + 熔断计数（连续 5 次失败 → 通道熔断 30min）
             └─ 可重试失败 → 退避 15/30/60/120/240s（最多重试 5 次）→ 超限 failed

线程安全：本模块用单把 ``threading.RLock`` 保护内存态（令牌桶/熔断/worker 引用），
数据库写操作由 ``StateStore`` 自身串行化；``on_event`` 里的任何异常都被吞掉（只记日志），
保证通知永远不会影响采集/报告主流程。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from typing_utils import as_dict

import notify_channels
import secret_store

log = logging.getLogger("sidecar.notify")

BRAND = "舆见 OpinionLens"

# ---------------------------------------------------------------------------
# 常量（契约 §2.1 / §3）
# ---------------------------------------------------------------------------
# 默认订阅的 9 类事件（event_type -> 默认严重级别）
DEFAULT_EVENT_RULES: Tuple[Tuple[str, str], ...] = (
    ("run.finished", "info"),
    ("run.failed", "error"),
    ("run.partial", "warn"),
    ("report.finished", "info"),
    ("report.degraded", "warn"),
    ("report.failed", "error"),
    ("account.login_invalid", "error"),
    ("account.risk_blocked", "error"),
    ("login.need_manual", "warn"),
)
EVENT_LABELS: Dict[str, str] = {
    "run.finished": "采集完成",
    "run.failed": "采集失败",
    "run.partial": "采集部分完成",
    "report.finished": "报告已生成",
    "report.degraded": "报告已生成（降级）",
    "report.failed": "报告生成失败",
    "account.login_invalid": "账号登录失效",
    "account.risk_blocked": "账号触发风控",
    "login.need_manual": "需要人工验证",
    "notify.test": "通知测试",
}
SEVERITY_RANK: Dict[str, int] = {"info": 1, "warn": 2, "error": 3}
SEVERITY_ICONS: Dict[str, str] = {"error": "❌ ", "warn": "⚠️ ", "info": ""}
PLATFORM_LABELS: Dict[str, str] = {
    "xhs": "小红书",
    "dy": "抖音",
    "ks": "快手",
    "bili": "B站",
    "wb": "微博",
    "tieba": "贴吧",
    "zhihu": "知乎",
}
MANUAL_KIND_LABELS: Dict[str, str] = {
    "sms": "短信验证",
    "slider": "滑块验证",
    "captcha": "图形验证码",
}

# 设置键（契约 §4 app_settings 约定）
SETTING_ENABLED = "notify.enabled"
SETTING_PRIVACY_MODE = "notify.privacy_mode"
SETTING_QUIET_HOURS = "notify.quiet_hours"
SETTING_MUTED_UNTIL = "notify.muted_until"

VALID_PRIVACY_MODES = ("minimal", "standard", "detailed")
DEFAULT_PRIVACY_MODE = "minimal"
# 默认 22:00-08:00 抑制 info/warn，error 穿透；severity 表示“被抑制的最高级别”
DEFAULT_QUIET_HOURS: List[Dict[str, str]] = [
    {"start": "22:00", "end": "08:00", "severity": "warn"}
]

# 发送管线参数（契约 §3）
MAX_RETRIES = 5  # 首次发送 + 最多 5 次重试
BACKOFF_SECONDS: Tuple[int, ...] = (15, 30, 60, 120, 240)
RATE_LIMIT_PER_MINUTE = 10
RATE_WINDOW_SECONDS = 60
CIRCUIT_FAIL_THRESHOLD = 5
CIRCUIT_OPEN_SECONDS = 1800
WORKER_BATCH = 10
OUTBOX_STATUSES = ("pending", "sending", "sent", "failed", "filtered")

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:\\[^\s，,；;、）)]*"),
    re.compile(r"/(?:home|Users|private|var|tmp|opt)/[^\s，,；;、）)]*"),
)


class NotifyError(RuntimeError):
    """通知业务错误（API 层会转成 ``engine_runner.ServiceError``）。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = int(status_code)


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def normalize_severity(value: Any, default: str = "info") -> str:
    text = str(value or "").strip().lower()
    if text in SEVERITY_RANK:
        return text
    mapping = {"warning": "warn", "err": "error", "critical": "error", "info": "info"}
    return mapping.get(text, default)


def _clip(text: Any, limit: int = 200) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(flat) > limit:
        return flat[:limit].rstrip() + "…"
    return flat


def _strip_paths(text: Any) -> str:
    result = str(text or "")
    for pattern in _PATH_PATTERNS:
        result = pattern.sub("<路径>", result)
    return result


def _safe_reason(value: Any, limit: int = 120) -> str:
    """错误/原因字段：去路径 + 截断（外发内容永远不含本地路径）。"""
    return _clip(_strip_paths(value), limit)


def _fmt_duration(seconds: Any) -> str:
    total = _as_int(seconds, 0)
    if total <= 0:
        return "未知"
    if total >= 3600:
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}h{minutes}m" if not secs else f"{hours}h{minutes}m{secs}s"
    if total >= 60:
        minutes, secs = divmod(total, 60)
        return f"{minutes}m" if not secs else f"{minutes}m{secs}s"
    return f"{total}s"


def _fmt_ts(ts: Any) -> str:
    try:
        stamp = float(ts)
        if stamp <= 0:
            raise ValueError
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime())


def _platform_label(value: Any) -> str:
    """只映射已知平台，未知值一律忽略（防止任意文本进入外发消息）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text in PLATFORM_LABELS:
        return PLATFORM_LABELS[text]
    if text in PLATFORM_LABELS.values():
        return text
    return ""


def _platform_list(values: Any) -> str:
    if not isinstance(values, (list, tuple)):
        return ""
    labels: List[str] = []
    for item in values:
        label = _platform_label(item)
        if label and label not in labels:
            labels.append(label)
    return "、".join(labels[:8])


def _parse_hhmm(value: Any) -> Optional[int]:
    match = _HHMM_RE.match(str(value or "").strip())
    if not match:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _minute_in_window(minute: int, start: int, end: int) -> bool:
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _normalize_quiet_hours(value: Any) -> List[Dict[str, str]]:
    """校验/规范化静默时段列表：``[{start:HH:MM, end:HH:MM, severity:info|warn|error}]``。"""
    if value is None:
        return list(DEFAULT_QUIET_HOURS)
    if not isinstance(value, (list, tuple)):
        raise NotifyError("NOTIFY_SETTINGS_INVALID", "quiet_hours 必须是数组", 422)
    result: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise NotifyError("NOTIFY_SETTINGS_INVALID", "quiet_hours 每一项必须是对象", 422)
        start = _parse_hhmm(item.get("start"))
        end = _parse_hhmm(item.get("end"))
        if start is None or end is None:
            raise NotifyError(
                "NOTIFY_SETTINGS_INVALID", "静默时段 start/end 必须是 HH:MM 格式", 422
            )
        severity = normalize_severity(item.get("severity"), "info")
        result.append(
            {
                "start": str(item.get("start")).strip(),
                "end": str(item.get("end")).strip(),
                "severity": severity,
            }
        )
    return result


def _normalize_muted_until(value: Any) -> int:
    """``notify.muted_until`` 规范化：支持 epoch 秒 / 毫秒 / ISO 时间字符串；0/空 = 不静音。"""
    if value in (None, "", 0, "0", False):
        return 0
    if isinstance(value, (int, float)):
        stamp = int(value)
        return int(stamp / 1000) if stamp > 10_000_000_000 else stamp
    text = str(value).strip()
    if not text:
        return 0
    try:
        stamp = int(float(text))
        return int(stamp / 1000) if stamp > 10_000_000_000 else stamp
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(parsed.timestamp())
    except ValueError as exc:
        raise NotifyError(
            "NOTIFY_SETTINGS_INVALID",
            "muted_until 需为 epoch 秒或 ISO 时间（如 2026-11-12T23:00:00）",
            422,
        ) from exc


# ---------------------------------------------------------------------------
# 渲染（隐私模式：minimal 默认 / standard / detailed）
# ---------------------------------------------------------------------------
def render_event(event: Dict[str, Any], privacy_mode: str = DEFAULT_PRIVACY_MODE) -> Dict[str, str]:
    """把事件渲染成中文标题/正文。

    * ``minimal``（默认）：事件类型/状态/平台/条数/耗时/时间/账号别名；
      **不含**关键词原文、昵称、评论正文、任务名、报告标题、本地路径；
    * ``standard``：额外含任务名、报告标题、关键词数量说明等；
    * ``detailed``：额外含关键词原文（用户显式选择，仍永不含昵称/正文/本地路径）。

    返回值：``{"title": "...", "body": "..."}``；本函数永不抛异常。
    """
    try:
        return _render_event_impl(event, privacy_mode)
    except Exception:  # noqa: BLE001 - 渲染失败时给一条最小可用消息
        log.exception("通知渲染失败（降级为通用文案）")
        return {
            "title": f"通知事件 · {BRAND}",
            "body": f"状态：有一条新事件\n\n—— 本消息由「{BRAND}」自动发送。",
        }


def _render_event_impl(event: Dict[str, Any], privacy_mode: str) -> Dict[str, str]:
    event = event if isinstance(event, dict) else {}
    data = as_dict(event.get("data"))
    event_type = str(event.get("type") or "").strip()
    severity = normalize_severity(event.get("severity"))
    privacy = str(privacy_mode or "").strip().lower()
    if privacy not in VALID_PRIVACY_MODES:
        privacy = DEFAULT_PRIVACY_MODE
    label = EVENT_LABELS.get(event_type, "通知事件")
    title = f"{SEVERITY_ICONS.get(severity, '')}{label} · {BRAND}"
    lines: List[str] = []

    if event_type in ("run.finished", "run.failed", "run.partial"):
        status_text = {
            "run.finished": "完成",
            "run.failed": "失败",
            "run.partial": "部分完成",
        }[event_type]
        lines.append(f"状态：{status_text}")
        total = _as_int(data.get("platforms_total"), 0)
        ok = _as_int(data.get("platforms_ok"), 0)
        failed = _as_int(data.get("platforms_failed"), 0)
        skipped = _as_int(data.get("platforms_skipped"), 0)
        if total > 0:
            detail = f"平台：{ok}/{total} 完成"
            if failed:
                detail += f"（失败 {failed}）"
            if skipped:
                detail += f"（跳过 {skipped}）"
            lines.append(detail)
        else:
            platform = _platform_label(data.get("platform"))
            if platform:
                lines.append(f"平台：{platform}")
        failed_platforms = _platform_list(data.get("failed_platforms"))
        if failed_platforms:
            lines.append(f"失败平台：{failed_platforms}")
        saved = _as_int(data.get("saved_items"), 0)
        if saved > 0:
            lines.append(f"采集：{saved} 条")
        keywords_count = _as_int(data.get("keywords_count"), 0)
        if keywords_count > 0:
            lines.append(f"关键词：{keywords_count} 个")
        duration = _as_int(data.get("duration_s"), 0)
        if duration > 0:
            lines.append(f"耗时：{_fmt_duration(duration)}")
        alias = _clip(data.get("account_alias"), 40)
        if alias:
            lines.append(f"账号：{alias}")
        if privacy in ("standard", "detailed"):
            task_name = _clip(data.get("name"), 60)
            if task_name:
                lines.append(f"任务：{task_name}")
        if severity == "error" or event_type == "run.failed":
            reason = _safe_reason(data.get("error") or data.get("message"))
            if reason:
                lines.append(f"原因：{reason}")
        run_id = _clip(data.get("run_id"), 48)
        if run_id:
            lines.append(f"任务号：{run_id}")

    elif event_type.startswith("report."):
        status_text = {
            "report.finished": "完成",
            "report.degraded": "已降级（本地模板）",
            "report.failed": "失败",
        }.get(event_type, "更新")
        lines.append(f"状态：{status_text}")
        report_id = _clip(data.get("report_id"), 48)
        if report_id:
            lines.append(f"报告号：{report_id}")
        version = _as_int(data.get("version"), 0)
        if version > 0:
            lines.append(f"版本：v{version}")
        if privacy in ("standard", "detailed"):
            report_title = _clip(data.get("title"), 60)
            if report_title:
                lines.append(f"报告：{report_title}")
        if event_type in ("report.degraded", "report.failed"):
            reason = _safe_reason(data.get("message") or data.get("error"))
            if reason:
                lines.append(f"原因：{reason}")
        duration = _as_int(data.get("duration_s"), 0)
        if duration > 0:
            lines.append(f"耗时：{_fmt_duration(duration)}")

    elif event_type in ("account.login_invalid", "account.risk_blocked"):
        status_text = "登录失效" if event_type == "account.login_invalid" else "触发风控"
        lines.append(f"状态：{status_text}")
        platform = _platform_label(data.get("platform"))
        if platform:
            lines.append(f"平台：{platform}")
        alias = _clip(data.get("account_alias"), 40)
        if alias:
            lines.append(f"账号：{alias}")
        reason = _safe_reason(data.get("message") or data.get("error"))
        if reason:
            lines.append(f"原因：{reason}")
        if event_type == "account.login_invalid":
            lines.append("建议：请到「平台账号」页重新扫码登录该账号")
        else:
            lines.append("建议：请暂停该账号的采集任务，稍后再试")

    elif event_type == "login.need_manual":
        lines.append("状态：需要人工验证")
        platform = _platform_label(data.get("platform"))
        if platform:
            lines.append(f"平台：{platform}")
        alias = _clip(data.get("account_alias"), 40)
        if alias:
            lines.append(f"账号：{alias}")
        kind = MANUAL_KIND_LABELS.get(str(data.get("kind") or "").strip().lower())
        if kind:
            lines.append(f"验证方式：{kind}")
        lines.append("提示：请在打开的浏览器窗口中完成验证后继续")

    elif event_type == "notify.test":
        message = _clip(data.get("message") or "", 200)
        lines.append(f"状态：测试消息发送成功")
        if message:
            lines.append(message)

    else:
        lines.append("状态：新事件")
        message = _safe_reason(data.get("message"))
        if message:
            lines.append(message)

    if privacy == "detailed":
        raw_keywords = data.get("keywords")
        if isinstance(raw_keywords, (list, tuple)) and raw_keywords:
            words = [_clip(item, 24) for item in raw_keywords[:10]]
            lines.append("关键词原文：" + "、".join(w for w in words if w))

    timestamp = event.get("ts")
    lines.append(f"时间：{_fmt_ts(timestamp)}")
    lines.append("")
    lines.append(f"—— 本消息由「{BRAND}」自动发送，仅含摘要，不含采集内容。")
    return {"title": title, "body": "\n".join(lines)}


# ---------------------------------------------------------------------------
# 后台发送线程
# ---------------------------------------------------------------------------
class NotifierWorker(threading.Thread):
    """单线程 outbox 消费者（daemon；异常自愈，不退出）。"""

    def __init__(self, service: "NotifyService") -> None:
        super().__init__(name="notify-worker", daemon=True)
        self.service = service

    def run(self) -> None:  # noqa: D401 - thread body
        log.debug("通知发送线程启动")
        service = self.service
        while not service._stop_event.is_set():
            try:
                service.process_pending()
            except Exception:  # noqa: BLE001 - worker 必须自愈
                log.exception("通知发送线程处理异常（已忽略，继续运行）")
            try:
                timeout = service._next_wait_seconds()
            except Exception:  # noqa: BLE001
                timeout = 1.0
            service._wake_event.wait(timeout)
            service._wake_event.clear()
        log.debug("通知发送线程退出")


# ---------------------------------------------------------------------------
# NotifyService
# ---------------------------------------------------------------------------
class NotifyService:
    """通知服务入口：start/shutdown/on_event/config_summary + 设置页 CRUD 支撑。"""

    def __init__(self, cfg: Any, state: Any) -> None:
        self.cfg = cfg
        self.state = state
        self._lock = threading.RLock()
        self._started = False
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker: Optional[NotifierWorker] = None
        # 内存态：每通道令牌桶（时间戳 deque）与熔断计数
        self._buckets: Dict[str, Deque[float]] = {}
        # 熔断状态：fails/open_until 是数值、last_error 是字符串 -> 值是混合类型（Any）
        self._circuits: Dict[str, Dict[str, Any]] = {}
        self._last_sent_at: Optional[int] = None

    # ================= 生命周期 =======================================
    @property
    def enabled(self) -> bool:
        """通知总开关当前值（main.py 启动日志/健康检查用）。"""
        return self._get_enabled()

    def start(self, start_worker: bool = True) -> None:
        """种子默认规则 + 恢复崩溃残留 sending + 启动后台 worker（幂等）。"""
        with self._lock:
            if self._started:
                return
            try:
                self._seed_default_rules()
            except Exception:  # noqa: BLE001 - 通知初始化失败不能影响 sidecar 启动
                log.exception("通知默认规则初始化失败（忽略）")
            try:
                self.state.execute(
                    "UPDATE notify_outbox SET status = 'pending' WHERE status = 'sending'"
                )
            except Exception:  # noqa: BLE001
                log.exception("通知 outbox 恢复失败（忽略）")
            self._stop_event.clear()
            self._wake_event.clear()
            if start_worker:
                self._worker = NotifierWorker(self)
                self._worker.start()
            self._started = True
        try:
            channel_count = len(self.state.query("SELECT channel_id FROM notify_channels"))
            rule_count = len(self.state.query("SELECT rule_id FROM notify_rules"))
        except Exception:  # noqa: BLE001
            channel_count = rule_count = -1
        if notify_channels.test_mode():
            log.warning(
                "通知测试模式已开启（%s=1）：允许 http/任意 host 出站，仅用于离线测试，生产环境请勿设置",
                notify_channels.TEST_ENV,
            )
        log.info(
            "通知服务已启动：通道 %s 个，规则 %s 条，worker=%s",
            channel_count,
            rule_count,
            "on" if start_worker else "off",
        )

    def shutdown(self) -> None:
        """停止 worker（幂等；不关闭 StateStore，它由 EngineService 管理）。"""
        with self._lock:
            if not self._started and self._worker is None:
                return
            self._started = False
            self._stop_event.set()
            self._wake_event.set()
            worker = self._worker
            self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)
        log.info("通知服务已停止")

    # ================= 事件入口 =======================================
    def on_event(self, event: Dict[str, Any]) -> None:
        """事件总线回调：规则匹配 → 静默 → 去重 → 渲染 → 落 outbox → 唤醒 worker。

        本方法同步执行但只做 SQLite 写（微秒级）；任何异常都被吞掉，绝不回传业务主流程。
        """
        if not self._started:
            return
        try:
            self._handle_event(event)
        except Exception:  # noqa: BLE001 - 通知失败不得影响采集/报告
            log.exception("通知事件处理失败（忽略）：type=%s", (event or {}).get("type"))

    def _handle_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            return
        if not self._get_enabled():
            return
        severity = normalize_severity(event.get("severity"))
        rule = self._match_rule(event_type, severity, event)
        if rule is None:
            return
        channels = self._resolve_channels(rule)
        if not channels:
            log.debug("通知规则命中但没有可用通道：type=%s", event_type)
            return
        privacy_mode = self._get_privacy_mode()
        rendered = render_event(event, privacy_mode)
        title = str(rendered.get("title") or "")[:500]
        body = str(rendered.get("body") or "")[:8000]
        dedupe_key = str(event.get("dedupe_key") or "").strip() or f"{event_type}:{event.get('ts') or 0}"
        channel_ids = [str(row["channel_id"]) for row in channels]

        # 静默时段 / 临时静音：info/warn 被抑制（error 穿透），落 filtered 便于排查且参与去重
        quiet_reason = self._quiet_reason(severity)
        if quiet_reason:
            inserted = self._insert_outbox(
                event_type=event_type,
                severity=severity,
                title=title,
                body=body,
                channel_ids=channel_ids,
                dedupe_key=dedupe_key,
                status="filtered",
                last_error=quiet_reason,
            )
            if inserted:
                log.info("通知被静默时段抑制：type=%s severity=%s reason=%s", event_type, severity, quiet_reason)
            return

        inserted = self._insert_outbox(
            event_type=event_type,
            severity=severity,
            title=title,
            body=body,
            channel_ids=channel_ids,
            dedupe_key=dedupe_key,
        )
        if inserted:
            log.info(
                "通知已入队：type=%s severity=%s channels=%s",
                event_type,
                severity,
                len(channel_ids),
            )
            self.wake()
        else:
            log.debug("通知已去重：type=%s dedupe_key=%s", event_type, dedupe_key)

    def wake(self) -> None:
        """唤醒 worker（线程安全、幂等）。"""
        self._wake_event.set()

    # ================= 配置摘要 =======================================
    def config_summary(self) -> Dict[str, Any]:
        """设置页 GET /api/notify/config 的数据（通道密钥只回 masked/configured）。"""
        channels = [self._channel_public(row) for row in self._list_channel_rows()]
        return {
            "brand": BRAND,
            "enabled": self._get_enabled(),
            "privacy_mode": self._get_privacy_mode(),
            "quiet_hours": self._read_quiet_hours(),
            "muted_until": self._read_muted_until() or None,
            "channels": channels,
            "rules": self.list_rules(),
            "worker": self.worker_stats(),
            "channel_types": notify_channels.channel_type_catalog(),
            "event_types": [
                {"type": event_type, "label": EVENT_LABELS.get(event_type, event_type), "default_severity": severity}
                for event_type, severity in DEFAULT_EVENT_RULES
            ],
        }

    def worker_stats(self) -> Dict[str, Any]:
        """worker 概览：pending/failed/sending/sent/last_sent_at/last_error。"""
        stats: Dict[str, Any] = {
            "pending": 0,
            "failed": 0,
            "sending": 0,
            "sent": 0,
            "filtered": 0,
            "last_sent_at": self._last_sent_at,
            "last_error": "",
        }
        try:
            rows = self.state.query(
                "SELECT status, COUNT(*) AS total FROM notify_outbox GROUP BY status"
            )
            for row in rows:
                key = str(row.get("status") or "")
                if key in stats:
                    stats[key] = int(row.get("total") or 0)
            sent_row = self.state.query(
                "SELECT MAX(sent_at) AS last_sent_at FROM notify_outbox WHERE status = 'sent'"
            )
            if sent_row and sent_row[0].get("last_sent_at"):
                stats["last_sent_at"] = int(sent_row[0]["last_sent_at"])
                self._last_sent_at = stats["last_sent_at"]
            error_row = self.state.query(
                "SELECT last_error FROM notify_outbox WHERE status = 'failed' "
                "AND last_error != '' ORDER BY created_at DESC, rowid DESC LIMIT 1"
            )
            if error_row:
                stats["last_error"] = str(error_row[0].get("last_error") or "")
        except Exception:  # noqa: BLE001 - 状态统计失败不影响设置页其它内容
            log.exception("读取通知队列统计失败（忽略）")
        return stats

    # ================= 规则 ==========================================
    def list_rules(self) -> List[Dict[str, Any]]:
        rows = self.state.query("SELECT * FROM notify_rules ORDER BY rowid ASC")
        return [self._rule_public(row) for row in rows]

    def update_rules(self, payload: Any) -> List[Dict[str, Any]]:
        """更新事件订阅规则（enabled / min_severity / channel_ids / conditions）。"""
        if isinstance(payload, dict):
            items = payload.get("rules")
            if items is None:
                items = [payload]
        elif isinstance(payload, list):
            items = payload
        else:
            raise NotifyError("NOTIFY_RULE_INVALID", "rules 必须是数组或对象", 422)
        if not isinstance(items, list) or not items:
            raise NotifyError("NOTIFY_RULE_INVALID", "rules 不能为空", 422)
        now = int(time.time())
        existing_channels = {
            str(row["channel_id"]) for row in self.state.query("SELECT channel_id FROM notify_channels")
        }
        for item in items:
            if not isinstance(item, dict):
                raise NotifyError("NOTIFY_RULE_INVALID", "rules 每一项必须是对象", 422)
            event_type = str(item.get("event_type") or "").strip()
            if event_type not in EVENT_LABELS:
                raise NotifyError(
                    "NOTIFY_RULE_INVALID",
                    f"未知事件类型：{event_type or '(空)'}",
                    422,
                )
            current = self.state.query(
                "SELECT * FROM notify_rules WHERE event_type = ? ORDER BY rowid ASC LIMIT 1",
                (event_type,),
            )
            base = current[0] if current else {}
            enabled = _as_bool(item.get("enabled"), _as_bool(base.get("enabled"), True))
            min_severity = normalize_severity(
                item.get("min_severity"), normalize_severity(base.get("min_severity"), "info")
            )
            if "channel_ids" in item:
                raw_ids = item.get("channel_ids") or []
                if not isinstance(raw_ids, (list, tuple)):
                    raise NotifyError("NOTIFY_RULE_INVALID", "channel_ids 必须是数组", 422)
                channel_ids: List[str] = []
                for raw_id in raw_ids:
                    channel_id = str(raw_id or "").strip()
                    if not channel_id:
                        continue
                    if channel_id not in existing_channels:
                        raise NotifyError(
                            "NOTIFY_RULE_INVALID", f"通道不存在：{channel_id}", 422
                        )
                    if channel_id not in channel_ids:
                        channel_ids.append(channel_id)
            else:
                channel_ids = _json_loads(base.get("channel_ids_json"), [])
            conditions = item.get("conditions", _json_loads(base.get("conditions_json"), {}))
            if not isinstance(conditions, dict):
                raise NotifyError("NOTIFY_RULE_INVALID", "conditions 必须是对象", 422)
            template = str(item.get("template") or base.get("template") or "default")[:40]
            if current:
                self.state.execute(
                    "UPDATE notify_rules SET enabled = ?, min_severity = ?, channel_ids_json = ?, "
                    "conditions_json = ?, template = ?, updated_at = ? WHERE event_type = ?",
                    (
                        int(enabled),
                        min_severity,
                        _json_dumps(channel_ids),
                        _json_dumps(conditions),
                        template,
                        now,
                        event_type,
                    ),
                )
            else:
                self.state.execute(
                    "INSERT INTO notify_rules(rule_id, event_type, enabled, min_severity, "
                    "channel_ids_json, conditions_json, template, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._rule_id(event_type),
                        event_type,
                        int(enabled),
                        min_severity,
                        _json_dumps(channel_ids),
                        _json_dumps(conditions),
                        template,
                        now,
                    ),
                )
        return self.list_rules()

    # ================= 通道 ==========================================
    def list_channels(self) -> List[Dict[str, Any]]:
        return [self._channel_public(row) for row in self._list_channel_rows()]

    def create_channel(self, payload: Any) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise NotifyError("NOTIFY_CHANNEL_INVALID", "通道配置必须是对象", 422)
        channel_type = str(payload.get("type") or "").strip().lower()
        secret = str(payload.get("secret") or "")
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        try:
            normalized = notify_channels.validate_config(
                channel_type, config, secret, require_secret=True
            )
        except notify_channels.ChannelConfigError as exc:
            raise NotifyError("NOTIFY_CHANNEL_INVALID", str(exc), 422) from exc
        channel_id = "ch_" + uuid.uuid4().hex[:10]
        now = int(time.time())
        name = str(payload.get("name") or "").strip()[:60] or notify_channels.CHANNEL_TYPES[channel_type]["label"]
        enabled = _as_bool(payload.get("enabled"), True)
        quiet_hours = payload.get("quiet_hours")
        quiet_hours_json = _json_dumps(
            _normalize_quiet_hours(quiet_hours) if quiet_hours is not None else []
        )
        secret_enc = secret_store.protect(notify_channels.normalize_secret(channel_type, secret)) if secret else ""
        self.state.execute(
            "INSERT INTO notify_channels(channel_id, type, name, enabled, secret_enc, "
            "config_json, quiet_hours_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel_id,
                channel_type,
                name,
                int(enabled),
                secret_enc,
                _json_dumps(normalized),
                quiet_hours_json,
                now,
                now,
            ),
        )
        log.info("通知通道已创建：id=%s type=%s", channel_id, channel_type)
        return self._channel_public(self._get_channel_row(channel_id))

    def update_channel(self, channel_id: str, payload: Any) -> Dict[str, Any]:
        row = self._get_channel_row(channel_id)
        if row is None:
            raise NotifyError("NOTIFY_CHANNEL_NOT_FOUND", "通知通道不存在", 404)
        if not isinstance(payload, dict):
            raise NotifyError("NOTIFY_CHANNEL_INVALID", "通道配置必须是对象", 422)
        channel_type = str(payload.get("type") or row["type"]).strip().lower()
        if channel_type != str(row["type"]):
            raise NotifyError("NOTIFY_CHANNEL_INVALID", "不支持修改通道类型（请删除后重新添加）", 422)
        config = _json_loads(row.get("config_json"), {})
        if isinstance(payload.get("config"), dict):
            config.update(payload["config"])
        clear_secret = _as_bool(payload.get("clear_secret"), False)
        new_secret = str(payload.get("secret") or "")
        if clear_secret:
            secret_plain: Optional[str] = ""
        elif new_secret:
            secret_plain = notify_channels.normalize_secret(channel_type, new_secret)
        else:
            secret_plain = None  # 不传 secret = 保留旧密钥
        # 校验：编辑时若未提供新密钥（None=沿用旧密钥），跳过“密钥必填/URL 校验”
        try:
            normalized = notify_channels.validate_config(
                channel_type, config, secret_plain, require_secret=False
            )
        except notify_channels.ChannelConfigError as exc:
            raise NotifyError("NOTIFY_CHANNEL_INVALID", str(exc), 422) from exc
        if secret_plain is None:
            secret_enc = str(row.get("secret_enc") or "")
        else:
            secret_enc = secret_store.protect(secret_plain) if secret_plain else ""
        name = str(payload.get("name") or row["name"] or "").strip()[:60] or channel_type
        enabled = _as_bool(payload.get("enabled"), _as_bool(row.get("enabled"), True))
        if "quiet_hours" in payload:
            quiet_hours_json = _json_dumps(_normalize_quiet_hours(payload.get("quiet_hours")))
        else:
            quiet_hours_json = str(row.get("quiet_hours_json") or "[]")
        self.state.execute(
            "UPDATE notify_channels SET name = ?, enabled = ?, secret_enc = ?, config_json = ?, "
            "quiet_hours_json = ?, updated_at = ? WHERE channel_id = ?",
            (
                name,
                int(enabled),
                secret_enc,
                _json_dumps(normalized),
                quiet_hours_json,
                int(time.time()),
                channel_id,
            ),
        )
        log.info("通知通道已更新：id=%s clear_secret=%s", channel_id, clear_secret)
        return self._channel_public(self._get_channel_row(channel_id))

    def delete_channel(self, channel_id: str) -> Dict[str, Any]:
        row = self._get_channel_row(channel_id)
        if row is None:
            raise NotifyError("NOTIFY_CHANNEL_NOT_FOUND", "通知通道不存在", 404)
        self.state.execute("DELETE FROM notify_channels WHERE channel_id = ?", (channel_id,))
        with self._lock:
            self._buckets.pop(channel_id, None)
            self._circuits.pop(channel_id, None)
        log.info("通知通道已删除：id=%s", channel_id)
        return {"ok": True, "channel_id": channel_id}

    def test_channel(self, channel_id: str) -> Dict[str, Any]:
        """同步发送一条测试消息（≤10s），永不抛异常（密钥解密失败也返回 ok=false）。"""
        row = self._get_channel_row(channel_id)
        if row is None:
            raise NotifyError("NOTIFY_CHANNEL_NOT_FOUND", "通知通道不存在", 404)
        channel_type = str(row["type"])
        try:
            secret = secret_store.unprotect(str(row.get("secret_enc") or ""))
        except secret_store.SecretError:
            return {
                "ok": False,
                "channel_id": channel_id,
                "type": channel_type,
                "name": str(row.get("name") or ""),
                "latency_ms": 0,
                "http_status": None,
                "permanent": True,
                "error": "密钥无法解密（换 Windows 用户/换机后需重新填写）",
            }
        config = _json_loads(row.get("config_json"), {})
        title = f"通知测试 · {BRAND}"
        body = "\n".join(
            [
                "状态：测试消息发送成功",
                f"通道：{row.get('name') or channel_type}（{notify_channels.CHANNEL_TYPES.get(channel_type, {}).get('label', channel_type)}）",
                f"时间：{_fmt_ts(int(time.time()))}",
                "",
                f"—— 本消息由「{BRAND}」自动发送，用于验证通知通道是否配置正确。",
            ]
        )
        result = notify_channels.send(
            {"channel_id": channel_id, "type": channel_type, "config": config, "secret": secret},
            title,
            body,
        )
        self._write_notify_log("manual_test", channel_id, result)
        return {
            "ok": bool(result.ok),
            "channel_id": channel_id,
            "type": channel_type,
            "name": str(row.get("name") or ""),
            "latency_ms": int(result.latency_ms or 0),
            "http_status": result.http_status,
            "permanent": bool(result.permanent),
            "error": result.error or "",
        }

    def test_all_channels(self) -> Dict[str, Any]:
        """给所有启用中的通道同步发测试消息（POST /api/notify/test）。"""
        rows = [row for row in self._list_channel_rows() if _as_bool(row.get("enabled"), True)]
        if not rows:
            return {
                "ok": False,
                "sent": 0,
                "results": [],
                "error": "没有启用中的通知通道，请先添加并启用至少一个通道",
            }
        results = [self.test_channel(str(row["channel_id"])) for row in rows]
        return {
            "ok": all(bool(item.get("ok")) for item in results),
            "sent": sum(1 for item in results if item.get("ok")),
            "results": results,
        }

    # ================= outbox / 日志 =================================
    def list_outbox(self, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        status_value = str(status or "").strip()
        if status_value and status_value not in OUTBOX_STATUSES:
            raise NotifyError(
                "NOTIFY_OUTBOX_INVALID",
                f"status 只支持 {' / '.join(OUTBOX_STATUSES)}",
                422,
            )
        limit_value = max(1, min(_as_int(limit, 100), 200))
        if status_value:
            rows = self.state.query(
                "SELECT * FROM notify_outbox WHERE status = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (status_value, limit_value),
            )
        else:
            rows = self.state.query(
                "SELECT * FROM notify_outbox ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit_value,),
            )
        return [self._outbox_public(row) for row in rows]

    def retry_outbox(self, outbox_id: str) -> Dict[str, Any]:
        row = self.state.query(
            "SELECT * FROM notify_outbox WHERE outbox_id = ?", (str(outbox_id),)
        )
        if not row:
            raise NotifyError("NOTIFY_OUTBOX_NOT_FOUND", "通知队列记录不存在", 404)
        current = row[0]
        if str(current.get("status") or "") not in ("failed", "filtered"):
            raise NotifyError(
                "NOTIFY_OUTBOX_NOT_RETRYABLE",
                "仅失败或被过滤的通知可以手动重试",
                409,
            )
        self.state.execute(
            "UPDATE notify_outbox SET status = 'pending', attempts = 0, next_attempt_ts = ?, "
            "last_error = '' WHERE outbox_id = ?",
            (int(time.time()), str(outbox_id)),
        )
        self.wake()
        log.info("通知已手动重试：outbox_id=%s", outbox_id)
        return {"ok": True, "outbox_id": str(outbox_id), "status": "pending"}

    def list_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit_value = max(1, min(_as_int(limit, 100), 200))
        rows = self.state.query(
            "SELECT l.log_id, l.outbox_id, l.channel_id, l.ok, l.http_status, l.latency_ms, "
            "l.error, l.created_at, c.name AS channel_name, c.type AS channel_type "
            "FROM notify_log l LEFT JOIN notify_channels c ON c.channel_id = l.channel_id "
            "ORDER BY l.created_at DESC, l.rowid DESC LIMIT ?",
            (limit_value,),
        )
        items: List[Dict[str, Any]] = []
        for row in rows:
            items.append(
                {
                    "log_id": str(row.get("log_id") or ""),
                    "outbox_id": str(row.get("outbox_id") or ""),
                    "channel_id": str(row.get("channel_id") or ""),
                    "channel_name": str(row.get("channel_name") or ""),
                    "channel_type": str(row.get("channel_type") or ""),
                    "ok": bool(row.get("ok")),
                    "http_status": row.get("http_status"),
                    "latency_ms": row.get("latency_ms"),
                    "error": str(row.get("error") or ""),
                    "created_at": row.get("created_at"),
                }
            )
        return items

    # ================= 设置 ==========================================
    def update_settings(self, payload: Any) -> Dict[str, Any]:
        """PUT /api/notify/settings：enabled / privacy_mode / quiet_hours / muted_until。"""
        if not isinstance(payload, dict):
            raise NotifyError("NOTIFY_SETTINGS_INVALID", "设置必须是对象", 422)
        if "enabled" in payload or SETTING_ENABLED in payload:
            value = payload.get("enabled", payload.get(SETTING_ENABLED))
            self.state.set_setting(SETTING_ENABLED, bool(_as_bool(value, True)))
        if "privacy_mode" in payload or SETTING_PRIVACY_MODE in payload:
            mode = str(payload.get("privacy_mode", payload.get(SETTING_PRIVACY_MODE)) or "").strip().lower()
            if mode not in VALID_PRIVACY_MODES:
                raise NotifyError(
                    "NOTIFY_SETTINGS_INVALID",
                    f"privacy_mode 只支持 {' / '.join(VALID_PRIVACY_MODES)}",
                    422,
                )
            self.state.set_setting(SETTING_PRIVACY_MODE, mode)
        if "quiet_hours" in payload or SETTING_QUIET_HOURS in payload:
            raw = payload.get("quiet_hours", payload.get(SETTING_QUIET_HOURS))
            self.state.set_setting(SETTING_QUIET_HOURS, _normalize_quiet_hours(raw))
        if "muted_until" in payload or SETTING_MUTED_UNTIL in payload:
            raw = payload.get("muted_until", payload.get(SETTING_MUTED_UNTIL))
            self.state.set_setting(SETTING_MUTED_UNTIL, _normalize_muted_until(raw))
        return self.config_summary()

    # ================= 发送管线 ======================================
    def process_pending(self, limit: int = WORKER_BATCH) -> int:
        """处理一轮到期 pending（worker 与测试/人工都会调用）；返回处理条数。"""
        now = int(time.time())
        rows = self.state.query(
            "SELECT * FROM notify_outbox WHERE status = 'pending' ORDER BY "
            "COALESCE(next_attempt_ts, 0) ASC, created_at ASC, rowid ASC LIMIT ?",
            (max(1, int(limit)),),
        )
        processed = 0
        for row in rows:
            if self._stop_event.is_set():
                break
            next_attempt = row.get("next_attempt_ts")
            if next_attempt is not None and int(next_attempt) > now:
                break
            try:
                self._process_outbox_row(row)
                processed += 1
            except Exception:  # noqa: BLE001 - 单条失败不影响后续
                log.exception("处理通知 outbox 失败：id=%s", row.get("outbox_id"))
        return processed

    def _process_outbox_row(self, row: Dict[str, Any]) -> None:
        outbox_id = str(row.get("outbox_id") or "")
        channel_ids = [str(item) for item in _json_loads(row.get("channel_ids_json"), []) if item]
        now = int(time.time())
        if not channel_ids:
            self._finish_failed(outbox_id, "通知缺少目标通道")
            return
        rows = self._load_channel_rows(channel_ids)
        available = [item for item in rows if _as_bool(item.get("enabled"), True)]
        missing = [channel_id for channel_id in channel_ids if channel_id not in {str(r["channel_id"]) for r in rows}]
        disabled = [channel_id for channel_id in channel_ids if channel_id in {str(r["channel_id"]) for r in rows} and channel_id not in {str(r["channel_id"]) for r in available}]
        # 熔断中的通道：本轮跳过（不消耗重试次数），等熔断窗口结束后再试
        usable: List[Dict[str, Any]] = []
        circuit_open = 0
        for channel_row in available:
            if self._circuit_is_open(str(channel_row["channel_id"]), now):
                circuit_open += 1
                continue
            usable.append(channel_row)
        if not usable:
            # 全部不可用：通道被删/禁用 → 永久失败；全部熔断 → 延后重试
            if not available:
                for channel_id in missing + disabled:
                    self._write_notify_log(
                        outbox_id,
                        channel_id,
                        notify_channels.SendResult(
                            ok=False, permanent=True, error="通道不存在或已禁用（可能已被删除）"
                        ),
                    )
                self._finish_failed(outbox_id, "通知通道不存在或已禁用")
                return
            delay = self._circuit_wait_seconds(channel_ids, now)
            self._reschedule_pending(outbox_id, now + delay)
            log.info("通知通道处于熔断期，稍后重试：outbox_id=%s delay=%ss", outbox_id, delay)
            return
        # 令牌桶（10 条/分/通道）：先检查再认领，避免空转消耗重试次数
        if any(not self._rate_available(str(item["channel_id"]), now) for item in usable):
            self._reschedule_pending(outbox_id, now + 2)
            return
        if not self._claim_outbox(outbox_id):
            return
        results: List[Tuple[str, notify_channels.SendResult]] = []
        for channel_id in missing + disabled:
            result = notify_channels.SendResult(
                ok=False, permanent=True, error="通道不存在或已禁用（可能已被删除）"
            )
            self._record_circuit(channel_id, result)
            self._write_notify_log(outbox_id, channel_id, result)
            results.append((channel_id, result))
        for channel_row in usable:
            channel_id = str(channel_row["channel_id"])
            try:
                secret = secret_store.unprotect(str(channel_row.get("secret_enc") or ""))
            except secret_store.SecretError:
                result = notify_channels.SendResult(
                    ok=False, permanent=True, error="密钥无法解密（需在设置页重新填写）"
                )
            else:
                self._acquire_rate(channel_id, now)
                config = _json_loads(channel_row.get("config_json"), {})
                try:
                    result = notify_channels.send(
                        {
                            "channel_id": channel_id,
                            "type": str(channel_row.get("type") or ""),
                            "config": config,
                            "secret": secret,
                        },
                        str(row.get("title") or ""),
                        str(row.get("body") or ""),
                    )
                except Exception as exc:  # noqa: BLE001 - send 理论上不抛，防御性兜底
                    result = notify_channels.SendResult(
                        ok=False,
                        permanent=False,
                        error=notify_channels.sanitize_text(f"发送异常：{exc}", secrets=(secret,)),
                    )
            self._record_circuit(channel_id, result)
            self._write_notify_log(outbox_id, channel_id, result)
            results.append((channel_id, result))
        if any(item.ok for _channel_id, item in results):
            self._finish_sent(outbox_id)
            return
        error_text = notify_channels.sanitize_text(
            "; ".join(f"{channel_id}: {item.error}" for channel_id, item in results if item.error),
            secrets=(),
            max_len=500,
        ) or "发送失败"
        attempts = _as_int(row.get("attempts"), 0) + 1
        all_permanent = all(item.permanent for _channel_id, item in results)
        if all_permanent:
            self._finish_failed(outbox_id, error_text)
            return
        if attempts > MAX_RETRIES:
            self._finish_failed(outbox_id, f"重试 {MAX_RETRIES} 次仍失败：{error_text}")
            return
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        self.state.execute(
            "UPDATE notify_outbox SET status = 'pending', attempts = ?, next_attempt_ts = ?, "
            "last_error = ? WHERE outbox_id = ?",
            (attempts, now + delay, error_text, outbox_id),
        )
        log.info(
            "通知发送失败（可重试）：outbox_id=%s attempts=%s next=%ss", outbox_id, attempts, delay
        )

    # ---------------- 通道内存态 --------------------------------------
    def _rate_available(self, channel_id: str, now: Optional[int] = None) -> bool:
        current = int(time.time()) if now is None else int(now)
        with self._lock:
            bucket = self._buckets.setdefault(channel_id, deque())
            self._prune_bucket(bucket, current)
            return len(bucket) < RATE_LIMIT_PER_MINUTE

    def _acquire_rate(self, channel_id: str, now: Optional[int] = None) -> bool:
        current = int(time.time()) if now is None else int(now)
        with self._lock:
            bucket = self._buckets.setdefault(channel_id, deque())
            self._prune_bucket(bucket, current)
            if len(bucket) >= RATE_LIMIT_PER_MINUTE:
                return False
            bucket.append(float(current))
            return True

    def _prune_bucket(self, bucket: Deque[float], now: int) -> None:
        threshold = now - RATE_WINDOW_SECONDS
        while bucket and bucket[0] < threshold:
            bucket.popleft()

    def _record_circuit(self, channel_id: str, result: notify_channels.SendResult) -> None:
        now = int(time.time())
        with self._lock:
            state = self._circuits.setdefault(
                channel_id, {"fails": 0.0, "open_until": 0.0, "last_error": ""}
            )
            if result.ok:
                state["fails"] = 0
                state["open_until"] = 0
                state["last_error"] = ""
                return
            state["fails"] = float(state.get("fails", 0)) + 1
            state["last_error"] = str(result.error or "")
            if state["fails"] >= CIRCUIT_FAIL_THRESHOLD:
                state["open_until"] = float(now + CIRCUIT_OPEN_SECONDS)
                log.warning(
                    "通知通道连续 %s 次失败，熔断 %s 分钟：channel=%s",
                    int(state["fails"]),
                    CIRCUIT_OPEN_SECONDS // 60,
                    channel_id,
                )

    def _circuit_is_open(self, channel_id: str, now: Optional[int] = None) -> bool:
        current = int(time.time()) if now is None else int(now)
        with self._lock:
            state = self._circuits.get(channel_id) or {}
            return float(state.get("open_until") or 0) > current

    def _circuit_wait_seconds(self, channel_ids: List[str], now: int) -> int:
        waits = []
        with self._lock:
            for channel_id in channel_ids:
                state = self._circuits.get(channel_id) or {}
                open_until = int(float(state.get("open_until") or 0))
                if open_until > now:
                    waits.append(open_until - now)
        return max(30, min(300, min(waits) if waits else 30))

    def channel_health(self, channel_id: str) -> Dict[str, Any]:
        now = int(time.time())
        with self._lock:
            state = self._circuits.get(channel_id) or {}
            fails = int(float(state.get("fails") or 0))
            open_until = int(float(state.get("open_until") or 0))
            return {
                "consecutive_fail": fails,
                "circuit_open": open_until > now,
                "circuit_open_until": open_until if open_until > now else None,
                "last_error": str(state.get("last_error") or ""),
            }

    # ---------------- outbox 状态更新 --------------------------------
    def _claim_outbox(self, outbox_id: str) -> bool:
        changed = self.state.execute(
            "UPDATE notify_outbox SET status = 'sending' WHERE outbox_id = ? AND status = 'pending'",
            (outbox_id,),
        )
        return changed > 0

    def _reschedule_pending(self, outbox_id: str, next_attempt_ts: int) -> None:
        self.state.execute(
            "UPDATE notify_outbox SET status = 'pending', next_attempt_ts = ? WHERE outbox_id = ?",
            (int(next_attempt_ts), outbox_id),
        )

    def _finish_sent(self, outbox_id: str) -> None:
        now = int(time.time())
        self.state.execute(
            "UPDATE notify_outbox SET status = 'sent', sent_at = ?, last_error = '' WHERE outbox_id = ?",
            (now, outbox_id),
        )
        self._last_sent_at = now
        log.info("通知发送成功：outbox_id=%s", outbox_id)

    def _finish_failed(self, outbox_id: str, error: str) -> None:
        safe_error = notify_channels.sanitize_text(error, max_len=500) or "发送失败"
        self.state.execute(
            "UPDATE notify_outbox SET status = 'failed', last_error = ? WHERE outbox_id = ?",
            (safe_error, outbox_id),
        )
        log.info("通知最终失败：outbox_id=%s error=%s", outbox_id, safe_error)

    def _write_notify_log(
        self, outbox_id: str, channel_id: str, result: notify_channels.SendResult
    ) -> None:
        try:
            self.state.execute(
                "INSERT INTO notify_log(log_id, outbox_id, channel_id, ok, http_status, "
                "latency_ms, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "lg_" + uuid.uuid4().hex[:12],
                    str(outbox_id),
                    str(channel_id),
                    int(bool(result.ok)),
                    result.http_status,
                    int(result.latency_ms or 0),
                    notify_channels.sanitize_text(result.error, max_len=300),
                    int(time.time()),
                ),
            )
        except Exception:  # noqa: BLE001 - 审计日志失败不影响主流程
            log.exception("写通知发送日志失败（忽略）")

    def _insert_outbox(
        self,
        event_type: str,
        severity: str,
        title: str,
        body: str,
        channel_ids: List[str],
        dedupe_key: str,
        status: str = "pending",
        last_error: str = "",
    ) -> bool:
        now = int(time.time())
        rowcount = self.state.execute(
            "INSERT OR IGNORE INTO notify_outbox(outbox_id, dedupe_key, event_type, severity, "
            "title, body, channel_ids_json, status, attempts, next_attempt_ts, last_error, "
            "created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)",
            (
                "ob_" + uuid.uuid4().hex[:12],
                str(dedupe_key)[:200],
                str(event_type)[:80],
                normalize_severity(severity),
                str(title)[:500],
                str(body)[:8000],
                _json_dumps(channel_ids),
                status,
                now if status == "pending" else None,
                notify_channels.sanitize_text(last_error, max_len=200),
                now,
            ),
        )
        return rowcount > 0

    # ---------------- 内部读取 ---------------------------------------
    def _next_wait_seconds(self) -> float:
        try:
            rows = self.state.query(
                "SELECT MIN(next_attempt_ts) AS next_ts FROM notify_outbox WHERE status = 'pending'"
            )
        except Exception:  # noqa: BLE001
            return 2.0
        next_ts = rows[0].get("next_ts") if rows else None
        if not next_ts:
            return 2.0
        delta = float(next_ts) - time.time()
        return max(0.1, min(2.0, delta))

    def _load_channel_rows(self, channel_ids: List[str]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for channel_id in channel_ids:
            row = self._get_channel_row(channel_id)
            if row is not None:
                rows.append(row)
        return rows

    def _get_channel_row(self, channel_id: Any) -> Optional[Dict[str, Any]]:
        channel_id = str(channel_id or "").strip()
        if not channel_id:
            return None
        rows = self.state.query(
            "SELECT * FROM notify_channels WHERE channel_id = ?", (channel_id,)
        )
        return rows[0] if rows else None

    def _list_channel_rows(self) -> List[Dict[str, Any]]:
        return self.state.query("SELECT * FROM notify_channels ORDER BY created_at ASC, rowid ASC")

    def _channel_public(self, row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """通道脱敏视图：密钥只给 secret_configured + key_masked。"""
        if row is None:
            return {}
        channel_id = str(row.get("channel_id") or "")
        channel_type = str(row.get("type") or "")
        secret_enc = str(row.get("secret_enc") or "")
        key_masked = ""
        if secret_enc:
            try:
                key_masked = secret_store.mask_secret(secret_store.unprotect(secret_enc))
            except secret_store.SecretError:
                key_masked = "****"
        config = _json_loads(row.get("config_json"), {})
        if isinstance(config, dict):
            config = {
                key: value
                for key, value in config.items()
                if str(key).lower() not in ("secret", "password", "token", "key", "url")
            }
        health = self.channel_health(channel_id)
        last_log = self.state.query(
            "SELECT ok, http_status, latency_ms, error, created_at FROM notify_log "
            "WHERE channel_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (channel_id,),
        )
        last_status = ""
        last_error = ""
        last_attempt_at: Optional[int] = None
        if last_log:
            last_status = "ok" if bool(last_log[0].get("ok")) else "failed"
            last_error = str(last_log[0].get("error") or "")
            last_attempt_at = last_log[0].get("created_at")
        meta = notify_channels.CHANNEL_TYPES.get(channel_type, {})
        return {
            "channel_id": channel_id,
            "type": channel_type,
            "type_label": meta.get("label", channel_type),
            "name": str(row.get("name") or ""),
            "enabled": _as_bool(row.get("enabled"), True),
            "secret_configured": bool(secret_enc),
            "key_masked": key_masked,
            "config": config if isinstance(config, dict) else {},
            "quiet_hours": _json_loads(row.get("quiet_hours_json"), []),
            "last_status": last_status,
            "last_error": notify_channels.sanitize_text(last_error, max_len=200),
            "last_attempt_at": last_attempt_at,
            "health": health,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    def _rule_public(self, row: Dict[str, Any]) -> Dict[str, Any]:
        event_type = str(row.get("event_type") or "")
        return {
            "rule_id": str(row.get("rule_id") or ""),
            "event_type": event_type,
            "event_label": EVENT_LABELS.get(event_type, event_type),
            "enabled": _as_bool(row.get("enabled"), True),
            "min_severity": normalize_severity(row.get("min_severity")),
            "channel_ids": [str(item) for item in _json_loads(row.get("channel_ids_json"), []) if item],
            "conditions": _json_loads(row.get("conditions_json"), {}),
            "template": str(row.get("template") or "default"),
            "updated_at": row.get("updated_at"),
        }

    def _outbox_public(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "outbox_id": str(row.get("outbox_id") or ""),
            "event_type": str(row.get("event_type") or ""),
            "event_label": EVENT_LABELS.get(str(row.get("event_type") or ""), ""),
            "severity": normalize_severity(row.get("severity")),
            "title": str(row.get("title") or ""),
            "body": str(row.get("body") or ""),
            "channel_ids": [str(item) for item in _json_loads(row.get("channel_ids_json"), []) if item],
            "status": str(row.get("status") or ""),
            "attempts": _as_int(row.get("attempts"), 0),
            "next_attempt_ts": row.get("next_attempt_ts"),
            "last_error": str(row.get("last_error") or ""),
            "created_at": row.get("created_at"),
            "sent_at": row.get("sent_at"),
        }

    # ---------------- 规则匹配/静默 ----------------------------------
    @staticmethod
    def _rule_id(event_type: str) -> str:
        return "rule_" + re.sub(r"[^a-z0-9]+", "_", event_type.lower()).strip("_")

    def _seed_default_rules(self) -> None:
        now = int(time.time())
        for event_type, severity in DEFAULT_EVENT_RULES:
            self.state.execute(
                "INSERT OR IGNORE INTO notify_rules(rule_id, event_type, enabled, min_severity, "
                "channel_ids_json, conditions_json, template, updated_at) "
                "VALUES (?, ?, 1, ?, '[]', '{}', 'default', ?)",
                (self._rule_id(event_type), event_type, severity, now),
            )

    def _match_rule(
        self, event_type: str, severity: str, event: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if event_type not in EVENT_LABELS and event_type != "notify.test":
            # 未知事件不订阅（避免外部任意文本进入通知）
            return None
        rows = self.state.query(
            "SELECT * FROM notify_rules WHERE event_type = ? ORDER BY rowid ASC",
            (event_type,),
        )
        severity_rank = SEVERITY_RANK.get(normalize_severity(severity), 1)
        for row in rows:
            if not _as_bool(row.get("enabled"), True):
                continue
            min_severity = normalize_severity(row.get("min_severity"), "info")
            if severity_rank < SEVERITY_RANK.get(min_severity, 1):
                continue
            conditions = _json_loads(row.get("conditions_json"), {})
            if conditions and not self._conditions_ok(conditions, event):
                continue
            return row
        return None

    @staticmethod
    def _conditions_ok(conditions: Dict[str, Any], event: Dict[str, Any]) -> bool:
        """最小条件支持：``{"only_if": {"字段": 值}}``；数值字段可用 ``字段_gt/_gte/_lt/_lte``。"""
        data = as_dict(event.get("data"))
        checks = conditions.get("only_if") if isinstance(conditions.get("only_if"), dict) else conditions
        if not isinstance(checks, dict):
            return True
        for raw_key, expected in checks.items():
            key = str(raw_key)
            operator = "eq"
            for suffix, name in (("_gt", "gt"), ("_gte", "gte"), ("_lt", "lt"), ("_lte", "lte")):
                if key.endswith(suffix):
                    key, operator = key[: -len(suffix)], name
                    break
            actual: Any = data.get(key)
            try:
                if operator == "gt" and not (float(actual) > float(expected)):
                    return False
                if operator == "gte" and not (float(actual) >= float(expected)):
                    return False
                if operator == "lt" and not (float(actual) < float(expected)):
                    return False
                if operator == "lte" and not (float(actual) <= float(expected)):
                    return False
            except (TypeError, ValueError):
                return False
            if operator == "eq" and actual != expected:
                return False
        return True

    def _resolve_channels(self, rule: Dict[str, Any]) -> List[Dict[str, Any]]:
        channel_ids = [str(item) for item in _json_loads(rule.get("channel_ids_json"), []) if item]
        rows = [row for row in self._list_channel_rows() if _as_bool(row.get("enabled"), True)]
        if channel_ids:
            allowed = set(channel_ids)
            rows = [row for row in rows if str(row["channel_id"]) in allowed]
        return rows

    # ---------------- 静默时段 ----------------------------------------
    def _quiet_reason(self, severity: str, now: Optional[int] = None) -> str:
        """返回静默原因（``quiet_hours`` / ``muted_until``）或空字符串；error 永远穿透。"""
        severity = normalize_severity(severity)
        if severity == "error":
            return ""
        current = int(time.time()) if now is None else int(now)
        muted_until = self._read_muted_until()
        if muted_until and current < muted_until:
            return "muted_until"
        local = time.localtime(current)
        minute = local.tm_hour * 60 + local.tm_min
        rank = SEVERITY_RANK.get(severity, 1)
        for entry in self._read_quiet_hours():
            start = _parse_hhmm(entry.get("start"))
            end = _parse_hhmm(entry.get("end"))
            if start is None or end is None:
                continue
            cap = SEVERITY_RANK.get(normalize_severity(entry.get("severity")), 1)
            if rank <= cap and _minute_in_window(minute, start, end):
                return "quiet_hours"
        return ""

    # ---------------- 设置读取 ----------------------------------------
    def _get_enabled(self) -> bool:
        try:
            return _as_bool(self.state.get_setting(SETTING_ENABLED, True), True)
        except Exception:  # noqa: BLE001
            return True

    def _get_privacy_mode(self) -> str:
        try:
            mode = str(self.state.get_setting(SETTING_PRIVACY_MODE, DEFAULT_PRIVACY_MODE) or "").lower()
        except Exception:  # noqa: BLE001
            return DEFAULT_PRIVACY_MODE
        return mode if mode in VALID_PRIVACY_MODES else DEFAULT_PRIVACY_MODE

    def _read_quiet_hours(self) -> List[Dict[str, str]]:
        try:
            raw = self.state.get_setting(SETTING_QUIET_HOURS, None)
        except Exception:  # noqa: BLE001
            return list(DEFAULT_QUIET_HOURS)
        if raw is None:
            return list(DEFAULT_QUIET_HOURS)
        try:
            return _normalize_quiet_hours(raw)
        except NotifyError:
            return list(DEFAULT_QUIET_HOURS)

    def _read_muted_until(self) -> int:
        try:
            raw = self.state.get_setting(SETTING_MUTED_UNTIL, 0)
        except Exception:  # noqa: BLE001
            return 0
        try:
            return _normalize_muted_until(raw)
        except NotifyError:
            return 0


__all__ = [
    "BACKOFF_SECONDS",
    "BRAND",
    "CIRCUIT_FAIL_THRESHOLD",
    "CIRCUIT_OPEN_SECONDS",
    "DEFAULT_EVENT_RULES",
    "DEFAULT_QUIET_HOURS",
    "EVENT_LABELS",
    "MAX_RETRIES",
    "NotifyError",
    "NotifyService",
    "NotifierWorker",
    "PLATFORM_LABELS",
    "RATE_LIMIT_PER_MINUTE",
    "SEVERITY_RANK",
    "VALID_PRIVACY_MODES",
    "normalize_severity",
    "render_event",
]

# -*- coding: utf-8 -*-
"""报告中心编排：本地报告任务 + 用户模型 API 直连（开源版舆见 OpinionLens）。

流程（后台线程，不阻塞 UI）::

    POST /api/reports
      → 建本地报告任务（status=queued，落 <data-dir>/reports/<id>/meta.json）
      → worker: queued → building → narrative/local-template → completed/degraded

    已配置模型 API（设置 → 模型 API）：
      1) report_data.build_report_package(...)   本地数据包 + 情感打标（模型结构化输出）
      2) LocalLLMClient.generate_narrative(...)  chat() 直连用户模型，产出 narrative JSON
      3) local_report.render_local_report(...)   本地 report_pipeline 渲染 HTML（数字由代码算）
      4) 本地版本管理 + narrative_v{n}.json

    未配置 / 调用失败 / 输出不合格：
      → local_report 本地模板渲染（status=degraded，source=local-template），不阻塞报告中心。

依赖（可选，未就绪时 API 返回 503 REPORT_DATA_UNAVAILABLE，不阻塞 sidecar 启动）：
``app/sidecar/report_data.py`` 的 ``build_report_package``。

事件：报告终态通过进程内事件总线发布（``report.finished`` / ``report.degraded`` /
``report.failed`` / 取消 / 重启中断），通知服务订阅后推送（见 IMPLEMENTATION_CONTRACT §2.1）。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from engine_runner import PLATFORM_LABELS, ServiceError, SidecarConfig
from events import bus
from llm_client import (
    CODE_NOT_CONFIGURED,
    LLMError,
    LocalLLMClient,
    SCHEMA_VERSION,
    mask_base_url,
)
from local_report import LOCAL_NARRATIVE_GENERATED_BY, render_local_report

log = logging.getLogger("sidecar.reports")

ALL_PLATFORMS: List[str] = ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]
CONTENT_TABLES: List[str] = [
    "xhs_note",
    "douyin_aweme",
    "kuaishou_video",
    "bilibili_video",
    "weibo_note",
    "tieba_note",
    "zhihu_content",
]
DEFAULT_WINDOW_DAYS = 7
MAX_WINDOW_DAYS = 366
MAX_VERSIONS = 10
REPORT_ID_PREFIX = "rp_"
LOCAL_FALLBACK_SOURCE = "local-template"
LOCAL_AI_SOURCE = "local-ai"
_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；\s|]+")


def _default_title(task: Dict[str, Any]) -> str:
    """历史报告标题兜底：从关键词推导，避免 UI 显示 “—”。"""
    keywords = [str(item).strip() for item in (task.get("keywords") or []) if str(item).strip()]
    if keywords:
        return f"{keywords[0]}舆情监测周报"
    return "舆情监测周报"


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        message = str(error.get("message") or "").strip()
        return f"{code}: {message}" if code and message else (message or code)
    return str(error or "").strip()

#: 报告任务状态
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_DEGRADED = "degraded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_DEGRADED, STATUS_FAILED, STATUS_CANCELLED)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp_float(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed or parsed <= 0:  # NaN / 负数
        return default
    return max(low, min(parsed, high))


def _load_report_data_module(module_name: Optional[str] = None) -> Tuple[Optional[Callable], str]:
    """加载 ``build_report_package``（默认 report_data；selftest 可注入替身模块）。"""
    name = (
        module_name
        or os.environ.get("SIDECAR_REPORT_DATA_MODULE")
        or "report_data"
    ).strip()
    if not name:
        name = "report_data"
    try:
        module = importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        return None, f"无法导入 {name}：{type(exc).__name__}: {exc}"
    build_fn = getattr(module, "build_report_package", None)
    if not callable(build_fn):
        return None, f"{name}.build_report_package 不存在或不可调用"
    return build_fn, ""


def _parse_date_value(value: Any, default: date) -> date:
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # 毫秒
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts).date()
        except (OverflowError, OSError, ValueError) as exc:
            raise ServiceError("INVALID_PARAMS", f"时间戳无法解析：{value!r}", 400) from exc
    if isinstance(value, bool):  # bool 是 int 子类，显式拒绝
        raise ServiceError("INVALID_PARAMS", f"日期格式不合法：{value!r}", 400)
    text = str(value).strip()
    if not text:
        return default
    if text.isdigit():
        return _parse_date_value(int(text), default)
    if len(text) == 8 and text.isdigit():
        try:
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        except ValueError as exc:
            raise ServiceError("INVALID_PARAMS", f"日期格式不合法：{value!r}", 400) from exc
    candidates = (text, text.replace("/", "-"))
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
        try:
            return date.fromisoformat(candidate)
        except ValueError:
            continue
    raise ServiceError("INVALID_PARAMS", f"日期格式不合法（支持 YYYY-MM-DD / ISO8601 / 时间戳）：{value!r}", 400)


# ---------------------------------------------------------------------------
# 事件发布（契约 §2.1：报告终态 → 通知服务）
# ---------------------------------------------------------------------------
def _report_event_data(task: Dict[str, Any], *, message: str = "", error: str = "") -> Dict[str, Any]:
    counts = task.get("counts") if isinstance(task.get("counts"), dict) else {}
    try:
        total_items = int(counts.get("total") or 0)
    except (TypeError, ValueError):
        total_items = 0
    return {
        "run_id": "",
        "name": "",
        "keywords_count": len(task.get("keywords") or []),
        "platforms_total": len(task.get("platforms") or []),
        "platforms_ok": 0,
        "platforms_failed": 0,
        "platforms_skipped": 0,
        "saved_items": total_items,
        "duration_s": 0,
        "error": str(error or ""),
        "platform": "",
        "account_id": "",
        "account_alias": "",
        "report_id": str(task.get("report_id") or ""),
        "version": int(task.get("current_version") or task.get("version") or 0),
        "title": str(task.get("title") or ""),
        "message": str(message or ""),
    }


def _publish_report_event(
    event_type: str,
    severity: str,
    dedupe_key: str,
    task: Dict[str, Any],
    *,
    message: str = "",
    error: str = "",
    version: Optional[int] = None,
) -> None:
    """发布报告事件；事件总线异常绝不能影响报告主流程。"""
    try:
        data = _report_event_data(task, message=message, error=error)
        if version is not None:
            data["version"] = int(version)
        bus.publish(
            {
                "type": str(event_type),
                "severity": str(severity),
                "dedupe_key": str(dedupe_key),
                "ts": int(time.time()),
                "data": data,
            }
        )
    except Exception:  # noqa: BLE001 - 通知通道失败不影响报告
        log.warning("报告事件发布失败：type=%s report=%s", event_type, task.get("report_id"))



# ---------------------------------------------------------------------------
# 报告任务
# ---------------------------------------------------------------------------
@dataclass
class _ReportJob:
    kind: str  # "create" | "revise"
    report_id: str
    instruction: str = ""
    base_version: int = 0


class ReportManager:
    """报告中心编排：任务队列 + 后台 worker + meta.json 持久化。"""

    def __init__(
        self,
        cfg: SidecarConfig,
        report_data_loader: Optional[Callable[[], Tuple[Optional[Callable], str]]] = None,
        llm: Any = None,
    ) -> None:
        self.cfg = cfg
        self.llm = llm  # 模型 API 服务（LLMService；未配置时报告走本地模板降级）
        self.reports_dir = Path(cfg.data_dir) / "reports"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        # 报告中心的模型客户端：与旧远端客户端同 duck 接口，内部直连用户配置的 provider
        self.ai = LocalLLMClient(cfg, llm=llm)
        self._loader = report_data_loader or _load_report_data_module
        self._build_fn, self._build_error = self._loader()
        self._lock = threading.RLock()
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._queue: "queue.Queue[_ReportJob]" = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.poll_timeout = _clamp_float(
            os.environ.get("SIDECAR_REPORT_TIMEOUT"), 600.0, 5.0, 3600.0
        )
        self.poll_interval = _clamp_float(
            os.environ.get("SIDECAR_REPORT_POLL_INTERVAL"), 1.5, 0.1, 10.0
        )
        self._load_existing()
        self._start_worker()

    # -- 生命周期 ---------------------------------------------------------
    def _start_worker(self) -> None:
        self._thread = threading.Thread(
            target=self._worker_loop, name="report-worker", daemon=True
        )
        self._thread.start()

    @property
    def configured(self) -> bool:
        """是否已配置默认模型 API（main.py 启动日志用）。"""
        try:
            return bool(self.llm and self.llm.get_default())
        except Exception:  # noqa: BLE001 - 配置读取失败按未配置处理
            return False

    def shutdown(self) -> None:
        self._stop_event.set()
        try:
            self._queue.put_nowait(_ReportJob(kind="__stop__", report_id=""))
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        with self._lock:
            for task in self._tasks.values():
                self._save_task(task)
        try:
            self.ai.close()
        except Exception:  # noqa: BLE001
            pass

    # -- 路径 / 持久化 ----------------------------------------------------
    def _report_dir(self, report_id: str) -> Path:
        return self.reports_dir / report_id

    def _meta_path(self, report_id: str) -> Path:
        return self._report_dir(report_id) / "meta.json"

    def _save_task(self, task: Dict[str, Any]) -> None:
        try:
            directory = self._report_dir(task["report_id"])
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "meta.json"
            tmp = directory / "meta.json.tmp"
            tmp.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            log.exception("报告 meta.json 写入失败：%s", task.get("report_id"))

    def _load_existing(self) -> None:
        if not self.reports_dir.is_dir():
            return
        loaded = 0
        for directory in sorted(self.reports_dir.iterdir()):
            if not directory.is_dir():
                continue
            meta_path = directory / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                task = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                log.warning("报告 meta.json 损坏，忽略：%s", meta_path)
                continue
            if not isinstance(task, dict) or not task.get("report_id"):
                continue
            report_id = str(task["report_id"])
            if report_id != directory.name:
                continue
            status = str(task.get("status") or "")
            if status in (STATUS_QUEUED, STATUS_RUNNING):
                # sidecar 重启：上次生成线程已丢失，标记失败（html 版本保留，可重新生成）
                task["status"] = STATUS_FAILED
                task["stage"] = "interrupted"
                task["progress"] = 100
                task["error"] = {
                    "code": "REPORT_INTERRUPTED",
                    "message": "sidecar 重启，报告生成中断；已有版本仍可查看，可重新生成",
                }
                task["updated_at"] = now_iso()
                self._save_task(task)
            self._tasks[report_id] = task
            if status in (STATUS_QUEUED, STATUS_RUNNING):
                # 重启中断：通知服务提示用户（去重键与 failed 区分，避免误报生成失败）
                _publish_report_event(
                    "report.failed",
                    "warn",
                    f"report:{report_id}:interrupted",
                    task,
                    message="sidecar 重启，报告生成中断；已有版本仍可查看，可重新生成",
                    error="REPORT_INTERRUPTED",
                )
            loaded += 1
        if loaded:
            log.info("已加载 %d 个历史报告任务", loaded)

    # -- 对外：配置 / 列表 / 详情 / html / open ---------------------------
    def _default_provider(self) -> Optional[Dict[str, Any]]:
        """当前默认模型 provider（含解密密钥，仅供内部判断配置状态）。"""
        if self.llm is None:
            return None
        try:
            return self.llm.get_default()
        except Exception:  # noqa: BLE001 - 配置读取失败按未配置处理
            log.warning("读取默认模型 API 配置失败（按未配置处理）", exc_info=True)
            return None

    def config_info(self) -> Dict[str, Any]:
        provider = self._default_provider() or {}
        with self._lock:
            return {
                "configured": bool(provider),
                "provider_id": str(provider.get("provider_id") or ""),
                "provider_name": str(provider.get("name") or ""),
                "kind": str(provider.get("kind") or ""),
                "model": str(provider.get("model") or ""),
                "base_url": mask_base_url(str(provider.get("base_url") or "")),
                "token_configured": bool(provider.get("secret_enc")),
                "degraded_fallback": True,
                "schema_version": SCHEMA_VERSION,
                "report_data_ready": self._build_fn is not None,
                "report_data_error": self._build_error,
                "reports_dir": str(self.reports_dir),
                "timeout_seconds": self.poll_timeout,
                "max_versions": MAX_VERSIONS,
                "platforms": [
                    {"platform": code, "label": PLATFORM_LABELS.get(code, code)}
                    for code in ALL_PLATFORMS
                ],
            }

    def available_keywords(self) -> Dict[str, Any]:
        """报告关键词候选项：只返回采集数据库里真实出现过的 source_keyword。

        UI 用它做多选，避免报告关键词与采集关键词口径不一致导致 0 条数据。
        """
        raw = self._default_keywords()
        tokens: List[str] = []
        for item in raw:
            for part in _KEYWORD_SPLIT_RE.split(str(item or "")):
                token = part.strip()
                if token and token not in tokens:
                    tokens.append(token)
        return {"keywords": tokens[:20]}

    def test_connection(self) -> Dict[str, Any]:
        """设置页「测试连接」：对默认 provider 发 ≤16 token ping（不抛异常）。"""
        return self.ai.test_connection()

    def cancel_report(self, report_id: str) -> Dict[str, Any]:
        """取消生成：本地立即置 cancelled（worker 在各阶段检查状态，不再覆盖 cancelled）。"""
        report_id = str(report_id)
        with self._lock:
            task = self._tasks.get(report_id)
            if task is None:
                raise ServiceError("REPORT_NOT_FOUND", f"报告不存在：{report_id}", 404)
            if str(task.get("status") or "") in TERMINAL_STATUSES:
                return self._public(task)
            task["status"] = STATUS_CANCELLED
            task["stage"] = "cancelled"
            task["progress"] = 100
            task["error"] = None
            task["updated_at"] = now_iso()
            self._save_task(task)
            snapshot = dict(task)
        _publish_report_event(
            "report.cancelled",
            "info",
            f"report:{report_id}:cancelled",
            snapshot,
            message="报告生成已取消",
        )
        log.info("报告 %s 已取消", report_id)
        return self.get_report(report_id)

    def _is_report_cancelled(self, report_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(str(report_id))
            return bool(task and str(task.get("status")) == STATUS_CANCELLED)

    def connection_info(self) -> Dict[str, Any]:
        """连接状态（本地配置视角，200 结构；实际连通性由 /capabilities 测试或生成时验证）。

        报告直连用户模型后不再每次轮询都发网络探测（避免无谓的 token 消耗）：
        ``configured=True`` 表示已配置默认 provider，``connected`` 与之同义；
        真实调用失败会在生成时自动降级本地模板并写入 ``ai_fallback_reason``。
        """
        provider = self._default_provider()
        if not provider:
            error = {
                "code": CODE_NOT_CONFIGURED,
                "message": "未配置模型 API（设置 → 模型 API），报告将使用本地模板生成",
            }
            return {
                "ok": False,
                "configured": False,
                "connected": False,
                "base_url": "",
                "provider_name": "",
                "model": None,
                "latency_ms": 0,
                "narrative_available": True,
                "narrative_endpoint": "local-llm",
                "error": _error_text(error),
                "error_detail": error,
                "local_fallback": True,
            }
        return {
            "ok": True,
            "configured": True,
            "connected": True,
            "base_url": mask_base_url(str(provider.get("base_url") or "")),
            "provider_name": str(provider.get("name") or ""),
            "model": str(provider.get("model") or ""),
            "latency_ms": 0,
            "narrative_available": True,
            "narrative_endpoint": "local-llm",
            "error": "",
            "error_detail": None,
            "local_fallback": True,
        }

    def list_reports(self) -> Dict[str, Any]:
        with self._lock:
            tasks = [self._public(task) for task in self._tasks.values()]
        tasks.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"reports": tasks}

    def get_report(self, report_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(report_id))
            if task is None:
                raise ServiceError("REPORT_NOT_FOUND", f"报告不存在：{report_id}", 404)
            return self._public(task)

    def get_html(self, report_id: str, version: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(report_id))
            if task is None:
                raise ServiceError("REPORT_NOT_FOUND", f"报告不存在：{report_id}", 404)
            versions = list(task.get("versions") or [])
        if not versions:
            raise ServiceError("REPORT_HTML_NOT_READY", "报告尚未生成 HTML", 404)
        target = int(version) if version is not None else int(
            task.get("current_version") or versions[-1].get("version") or 0
        )
        entry = next((item for item in versions if int(item.get("version") or 0) == target), None)
        if entry is None:
            raise ServiceError("REPORT_HTML_NOT_READY", f"报告版本 v{target} 不存在", 404)
        path = self._report_dir(str(report_id)) / str(entry.get("file_name") or f"v{target}.html")
        if not path.is_file():
            raise ServiceError("REPORT_HTML_NOT_READY", f"报告 HTML 文件不存在：{path.name}", 404)
        try:
            html = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ServiceError("REPORT_HTML_NOT_READY", f"报告 HTML 读取失败：{exc}", 500) from exc
        encoded = html.encode("utf-8")
        return {
            "report_id": str(report_id),
            "version": target,
            "html": html,
            "html_sha256": entry.get("html_sha256") or hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }

    def open_report(self, report_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(report_id))
            if task is None:
                raise ServiceError("REPORT_NOT_FOUND", f"报告不存在：{report_id}", 404)
            versions = list(task.get("versions") or [])
        if not versions:
            raise ServiceError("REPORT_HTML_NOT_READY", "报告尚未生成 HTML，无法打开", 404)
        current = int(task.get("current_version") or versions[-1].get("version") or 0)
        entry = next((item for item in versions if int(item.get("version") or 0) == current), versions[-1])
        path = self._report_dir(str(report_id)) / str(entry.get("file_name") or f"v{current}.html")
        if not path.is_file():
            raise ServiceError("REPORT_HTML_NOT_READY", f"报告 HTML 文件不存在：{path.name}", 404)
        return {"ok": True, "file_path": str(path)}

    # -- 对外：创建 / 修订 ------------------------------------------------
    def create_report(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # 模型 API 未配置/不可用不再拒绝，会走本地模板兜底（degraded）
        build_fn = self._ensure_build_fn()
        if build_fn is None:
            raise ServiceError(
                "REPORT_DATA_UNAVAILABLE",
                f"报告数据组件未就绪（report_data.build_report_package 缺失）：{self._build_error}",
                503,
            )
        window_start, window_end = self._parse_window(body)
        keywords = self._parse_keywords(body.get("keywords"))
        platforms = self._parse_platforms(body.get("platforms"))
        report_id = REPORT_ID_PREFIX + uuid.uuid4().hex[:12]
        created = now_iso()
        task: Dict[str, Any] = {
            "report_id": report_id,
            "status": STATUS_QUEUED,
            "stage": "queued",
            "progress": 0,
            "window_start": window_start,
            "window_end": window_end,
            "keywords": keywords,
            "platforms": platforms,
            "created_at": created,
            "updated_at": created,
            "remote_report_id": "",
            "current_version": 0,
            "versions": [],
            "error": None,
            "ai_fallback_reason": "",
            "narrative_generated_by": "",
            "stats_fingerprint": "",
            "counts": {},
            "title": "",
            "source": "",
        }
        with self._lock:
            self._tasks[report_id] = task
            self._save_task(task)
        self._queue.put(_ReportJob(kind="create", report_id=report_id))
        return {
            "report_id": report_id,
            "status": STATUS_QUEUED,
            "window_start": window_start,
            "window_end": window_end,
            "keywords": keywords,
            "platforms": platforms,
        }

    def revise_report(
        self, report_id: str, instruction: Any, base_version: Optional[Any] = None
    ) -> Dict[str, Any]:
        report_id = str(report_id)
        # 本地模板报告也可修订（同 stats 重渲染，不需要模型 API 连接）
        text = str(instruction or "").strip()
        if not text:
            raise ServiceError("INVALID_PARAMS", "instruction 不能为空", 400)
        if len(text) > 4000:
            raise ServiceError("INVALID_PARAMS", "instruction 超过 4000 字", 400)
        with self._lock:
            task = self._tasks.get(report_id)
            if task is None:
                raise ServiceError("REPORT_NOT_FOUND", f"报告不存在：{report_id}", 404)
            if task.get("status") == STATUS_RUNNING:
                raise ServiceError("REPORT_BUSY", "报告正在生成中，请稍后再修订", 409)
            current = int(task.get("current_version") or 0)
            if current <= 0:
                raise ServiceError("REPORT_NOT_READY", "报告尚未生成完成，无法修订", 409)
            if current >= MAX_VERSIONS:
                raise ServiceError("REPORT_VERSION_LIMIT", f"报告版本已达上限 {MAX_VERSIONS}", 409)
            try:
                target_base = int(base_version) if base_version is not None else current
            except (TypeError, ValueError) as exc:
                raise ServiceError("INVALID_PARAMS", "base_version 必须是整数", 400) from exc
            if target_base != current:
                raise ServiceError(
                    "REPORT_VERSION_CONFLICT",
                    f"base_version={target_base} 与当前版本 v{current} 不一致，请刷新后重试",
                    409,
                )
            task["status"] = STATUS_QUEUED
            task["stage"] = "revision_queued"
            task["progress"] = 0
            task["updated_at"] = now_iso()
            self._save_task(task)
        self._queue.put(
            _ReportJob(
                kind="revise", report_id=report_id, instruction=text, base_version=target_base
            )
        )
        return {
            "report_id": report_id,
            "status": STATUS_QUEUED,
            "base_version": target_base,
            "target_version": target_base + 1,
        }

    # -- worker -----------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if job.kind == "__stop__":
                    break
                if job.kind == "create":
                    self._run_create(job.report_id)
                elif job.kind == "revise":
                    self._run_revise(job.report_id, job.instruction, job.base_version)
            except ServiceError as exc:
                self._fail_job(job.report_id, exc.code, exc.message)
            except LLMError as exc:
                self._fail_job(job.report_id, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001 - 后台线程不允许崩
                log.exception("报告任务异常：%s", job.report_id)
                self._fail_job(
                    job.report_id, "REPORT_GENERATION_FAILED", f"{type(exc).__name__}: {exc}"
                )
            finally:
                try:
                    self._queue.task_done()
                except Exception:  # noqa: BLE001
                    pass

    def _run_create(self, report_id: str) -> None:
        task = self._require_task(report_id)
        if self._is_report_cancelled(report_id):
            return
        build_fn = self._ensure_build_fn()
        if build_fn is None:
            raise ServiceError(
                "REPORT_DATA_UNAVAILABLE", f"报告数据组件未就绪：{self._build_error}", 503
            )
        self._update(
            report_id,
            status=STATUS_RUNNING,
            stage="building",
            progress=3,
            error=None,
        )
        started = time.monotonic()
        try:
            package = build_fn(
                db_path=str(self.cfg.media_db),
                window_start=task["window_start"],
                window_end=task["window_end"],
                keywords=list(task.get("keywords") or []),
                platforms=list(task.get("platforms") or []),
                sentiment_analyzer=self.ai.analyze_sentiment,
                report_date=task["window_end"],
                data_dir=str(self.cfg.data_dir),
            )
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ServiceError(
                "REPORT_DATA_FAILED",
                f"报告数据包构建失败：{type(exc).__name__}: {exc}",
                500,
            ) from exc
        if not isinstance(package, dict) or not package:
            raise ServiceError("REPORT_DATA_FAILED", "报告数据包为空或格式错误", 500)
        if self._is_report_cancelled(report_id):
            return
        package_report = package.get("report") if isinstance(package.get("report"), dict) else {}
        title = str(package_report.get("title") or "").strip() or _default_title(task)
        self._save_package_assets(report_id, package)
        self._update(
            report_id,
            stage="probing",
            progress=6,
            title=title,
            counts=(package.get("counts") if isinstance(package.get("counts"), dict) else {}),
            stats_fingerprint=str((package.get("meta") or {}).get("stats_fingerprint") or ""),
            package_schema_version=str(package.get("schema_version") or ""),
        )
        log.info(
            "报告 %s 数据包构建完成（%.1fs，条数=%s），开始生成 AI 叙述…",
            report_id,
            time.monotonic() - started,
            (package.get("counts") or {}).get("total"),
        )
        counts = package.get("counts") if isinstance(package.get("counts"), dict) else {}
        try:
            total_items = int(counts.get("total") or 0)
        except (TypeError, ValueError):
            total_items = 0
        if total_items <= 0:
            message = (
                "所选时间窗口和平台没有匹配数据，未生成报告。"
                "请调整时间范围、平台或关键词后重试。"
            )
            self._update(
                report_id,
                status=STATUS_FAILED,
                stage="no-data",
                progress=100,
                counts=counts,
                error={"code": "REPORT_NO_DATA", "message": message},
            )
            log.warning(
                "报告 %s 未生成：窗口=%s~%s，平台=%s，条数=0",
                report_id,
                task.get("window_start"),
                task.get("window_end"),
                ",".join(task.get("platforms") or []),
            )
            _publish_report_event(
                "report.failed",
                "error",
                f"report:{report_id}:failed",
                dict(task, counts=counts, status=STATUS_FAILED),
                message=message,
                error="REPORT_NO_DATA",
            )
            return
        # 已配置模型 API → 直连生成 narrative，再由本地 report_pipeline 渲染 HTML；
        # 未配置 / 调用失败 / 输出不合格 → 本地模板降级（不标 failed）。
        if self._is_report_cancelled(report_id):
            return
        self._update(report_id, stage="narrative", progress=10)
        try:
            response = self.ai.generate_narrative(package)
            narrative = response.get("narrative") if isinstance(response, dict) else None
            meta = (
                response.get("meta")
                if isinstance(response, dict) and isinstance(response.get("meta"), dict)
                else {}
            )
            if not isinstance(narrative, dict) or not narrative:
                raise LLMError("模型返回的 narrative 为空", code="LLM_BAD_NARRATIVE")
            generated_by = str(meta.get("generated_by") or "local-llm")
            if self._is_report_cancelled(report_id):
                return
            version = self._render_local_fallback(
                report_id,
                package,
                reason="",
                version=1,
                narrative=narrative,
                narrative_generated_by=generated_by,
                source=LOCAL_AI_SOURCE,
                status=STATUS_COMPLETED,
            )
            log.info(
                "报告 %s 已由模型生成叙述并本地渲染（model=%s，v%s）",
                report_id,
                meta.get("model") or (response.get("model") if isinstance(response, dict) else "") or "",
                version,
            )
        except LLMError as exc:
            reason = (
                f"模型 API 调用失败：{exc.code}: {exc.message}；"
                "已使用本地模板生成（无 AI 叙述）"
            )
            log.warning("报告 %s 模型生成失败，回退本地模板：%s", report_id, reason[:300])
            if self._is_report_cancelled(report_id):
                return
            self._render_local_fallback(report_id, package, reason=reason, version=1)
        except Exception as exc:  # noqa: BLE001 - AI 阶段任何异常都回退本地，不标 failed
            reason = (
                f"模型 API 调用异常：{type(exc).__name__}: {exc}；"
                "已使用本地模板生成（无 AI 叙述）"
            )
            log.exception("报告 %s 模型生成异常，回退本地模板", report_id)
            if self._is_report_cancelled(report_id):
                return
            self._render_local_fallback(report_id, package, reason=reason, version=1)

    def _run_revise(self, report_id: str, instruction: str, base_version: int) -> None:
        task = self._require_task(report_id)
        if self._is_report_cancelled(report_id):
            return
        current = int(task.get("current_version") or 0)
        next_version = max(1, current + 1)
        self._update(
            report_id,
            status=STATUS_RUNNING,
            stage="revising",
            progress=5,
            error=None,
        )
        package = self._load_saved_package(report_id)
        if package is None:
            raise ServiceError(
                "REPORT_LOCAL_PACKAGE_MISSING",
                "报告缺少数据包（package.json/stats.json），无法修订；请重新生成报告",
                409,
            )
        if self._is_report_cancelled(report_id):
            return
        self._update(report_id, stage="narrative", progress=10)
        reason = ""
        try:
            previous = self._load_saved_narrative(report_id, int(base_version))
            response = self.ai.generate_narrative(
                package,
                instruction=instruction,
                previous_narrative=previous,
            )
            narrative = response.get("narrative") if isinstance(response, dict) else None
            meta = (
                response.get("meta")
                if isinstance(response, dict) and isinstance(response.get("meta"), dict)
                else {}
            )
            if not isinstance(narrative, dict) or not narrative:
                raise LLMError("模型返回的 narrative 为空", code="LLM_BAD_NARRATIVE")
            if self._is_report_cancelled(report_id):
                return
            self._render_local_fallback(
                report_id,
                package,
                reason="",
                version=next_version,
                instruction=instruction,
                narrative=narrative,
                narrative_generated_by=str(meta.get("generated_by") or "local-llm"),
                source=LOCAL_AI_SOURCE,
                status=STATUS_COMPLETED,
            )
            return
        except LLMError as exc:
            reason = f"模型 API 修订失败：{exc.code}: {exc.message}；已使用本地模板重渲染"
            log.warning("报告 %s 模型修订失败：%s", report_id, reason[:300])
        except Exception as exc:  # noqa: BLE001 - 模型异常不阻塞修订
            reason = f"模型 API 修订异常：{type(exc).__name__}: {exc}；已使用本地模板重渲染"
            log.exception("报告 %s 模型修订异常", report_id)
        if self._is_report_cancelled(report_id):
            return
        self._render_local_fallback(
            report_id,
            package,
            reason=reason or "本地模板重渲染（无 AI 叙述）",
            version=next_version,
            instruction=instruction,
        )

    # -- 本地模板兜底 / 数据包持久化 --------------------------------------
    def _render_local_fallback(
        self,
        report_id: str,
        package: Dict[str, Any],
        *,
        reason: str,
        version: int,
        instruction: str = "",
        narrative: Optional[Dict[str, Any]] = None,
        narrative_generated_by: str = LOCAL_NARRATIVE_GENERATED_BY,
        source: str = LOCAL_FALLBACK_SOURCE,
        status: str = STATUS_DEGRADED,
    ) -> int:
        """本地渲染并落盘 v{n}.html；AI 成功时 status=completed/source=local-ai。"""
        task = self._require_task(report_id)
        version = max(1, int(version or 1))
        title = str(task.get("title") or "").strip() or _default_title(task)
        result = render_local_report(
            package,
            title=title,
            version=version,
            instruction=instruction,
            reason=reason,
            narrative=narrative,
            narrative_generated_by=narrative_generated_by,
        )
        html = str(result.get("html") or "")
        if not html.strip():
            raise ServiceError(
                "REPORT_LOCAL_TEMPLATE_FAILED", "本地模板渲染结果为空", 500
            )
        # POC 模板不可用时内置模板无法完整渲染 AI narrative，不能标 completed。
        if status == STATUS_COMPLETED and narrative and result.get("engine") != "poc":
            status = STATUS_DEGRADED
            reason = reason or "AI 叙述已获取，但本地 POC 模板不可用，使用内置模板"
        encoded = html.encode("utf-8")
        directory = self._report_dir(report_id)
        directory.mkdir(parents=True, exist_ok=True)
        file_name = f"v{version}.html"
        path = directory / file_name
        tmp = directory / (file_name + ".tmp")
        tmp.write_text(html, encoding="utf-8")
        os.replace(tmp, path)
        entry = {
            "version": version,
            "file_name": file_name,
            "bytes": len(encoded),
            "html_sha256": hashlib.sha256(encoded).hexdigest(),
            "created_at": now_iso(),
            "ai_fallback_reason": str(reason),
            "narrative_generated_by": str(result.get("narrative_generated_by") or narrative_generated_by),
            "engine": str(result.get("engine") or "local"),
            "template_version": str(result.get("template_version") or ""),
        }
        if isinstance(narrative, dict) and narrative:
            try:
                (directory / f"narrative_v{version}.json").write_text(
                    json.dumps(narrative, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                log.warning("报告 %s narrative 落盘失败（忽略）", report_id, exc_info=True)
        with self._lock:
            current_task = self._tasks.get(report_id)
            if current_task is None:
                raise ServiceError("REPORT_NOT_FOUND", f"报告不存在：{report_id}", 404)
            versions = [
                item
                for item in (current_task.get("versions") or [])
                if int(item.get("version") or 0) != version
            ]
            versions.append(entry)
            versions.sort(key=lambda item: int(item.get("version") or 0))
            current_task["versions"] = versions[-MAX_VERSIONS:]
            current_task.update(
                {
                    "status": status,
                    "stage": "completed",
                    "progress": 100,
                    "source": source,
                    "narrative_generated_by": entry["narrative_generated_by"],
                    "ai_fallback_reason": str(reason),
                    "title": title,
                    "current_version": version,
                    "version": version,
                    "error": None,
                    "local_engine": str(result.get("engine") or ""),
                    "template_version": str(result.get("template_version") or ""),
                    "updated_at": now_iso(),
                }
            )
            self._save_task(current_task)
        snapshot = dict(task)
        snapshot.update(
            {
                "status": status,
                "source": source,
                "title": title,
                "current_version": version,
                "version": version,
                "narrative_generated_by": entry["narrative_generated_by"],
                "ai_fallback_reason": str(reason),
                "progress": 100,
                "stage": "completed",
                "error": None,
            }
        )
        if status == STATUS_COMPLETED:
            _publish_report_event(
                "report.finished",
                "info",
                f"report:{report_id}:v{version}:finished",
                snapshot,
                message=f"报告已生成：{title}（v{version}）",
                version=version,
            )
        else:
            _publish_report_event(
                "report.degraded",
                "warn",
                f"report:{report_id}:v{version}:degraded",
                snapshot,
                message=f"报告已生成（本地模板降级）：{title}（v{version}）",
                error=str(reason or "")[:500],
                version=version,
            )
        return version

    def _save_package_assets(self, report_id: str, package: Dict[str, Any]) -> None:
        """保存数据包/统计，供本地模板修订重渲染。"""
        try:
            directory = self._report_dir(report_id)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "package.json").write_text(
                json.dumps(package, ensure_ascii=False), encoding="utf-8"
            )
            stats = package.get("stats") if isinstance(package.get("stats"), dict) else {}
            (directory / "stats.json").write_text(
                json.dumps(stats, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            log.exception("报告数据包落盘失败：%s", report_id)

    def _load_saved_package(self, report_id: str) -> Optional[Dict[str, Any]]:
        directory = self._report_dir(report_id)
        package_path = directory / "package.json"
        if package_path.is_file():
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
                if isinstance(package, dict) and package:
                    return package
            except Exception:  # noqa: BLE001
                log.warning("package.json 解析失败：%s", package_path)
        stats_path = directory / "stats.json"
        task = self._tasks.get(report_id) or {}
        if stats_path.is_file():
            try:
                stats = json.loads(stats_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                return None
            report = {
                "title": task.get("title") or _default_title(task),
                "window": {
                    "start": str(task.get("window_start") or ""),
                    "end": str(task.get("window_end") or ""),
                    "timezone": "Asia/Shanghai",
                },
                "keywords": list(task.get("keywords") or []),
                "platforms": list(task.get("platforms") or []),
            }
            return {
                "report": report,
                "stats": stats if isinstance(stats, dict) else {},
                "counts": dict(task.get("counts") or {}),
                "meta": {"stats_fingerprint": str(task.get("stats_fingerprint") or "")},
            }
        return None

    def _load_saved_narrative(
        self, report_id: str, version: int
    ) -> Optional[Dict[str, Any]]:
        """读取本地 AI 报告版本对应的 narrative，供下一版修订使用。"""
        path = self._report_dir(report_id) / f"narrative_v{max(1, int(version or 1))}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            log.warning("narrative 解析失败：%s", path)
            return None

    # -- 内部 -------------------------------------------------------------
    def _ensure_build_fn(self) -> Optional[Callable]:
        if self._build_fn is not None:
            return self._build_fn
        # 懒加载：report_data.py 可能稍后就绪（开发期）
        self._build_fn, self._build_error = self._loader()
        return self._build_fn

    def _require_task(self, report_id: str) -> Dict[str, Any]:
        with self._lock:
            task = self._tasks.get(str(report_id))
            if task is None:
                raise ServiceError("REPORT_NOT_FOUND", f"报告不存在：{report_id}", 404)
            return dict(task)

    def _update(self, report_id: str, **fields: Any) -> None:
        with self._lock:
            task = self._tasks.get(str(report_id))
            if task is None:
                return
            # 已取消的任务不再被后台线程的状态/进度写回覆盖
            if str(task.get("status")) == STATUS_CANCELLED and str(fields.get("status") or "") != STATUS_CANCELLED:
                return
            task.update(fields)
            task["updated_at"] = now_iso()
            self._save_task(task)

    def _fail_job(self, report_id: str, code: str, message: str) -> None:
        try:
            with self._lock:
                task = self._tasks.get(str(report_id))
                if task is None:
                    return
                if str(task.get("status")) == STATUS_CANCELLED:
                    return
                error = {"code": str(code), "message": str(message)[:1000]}
                task["status"] = STATUS_FAILED
                task["stage"] = "failed"
                task["progress"] = 100
                task["error"] = error
                task["updated_at"] = now_iso()
                self._save_task(task)
                snapshot = dict(task)
            log.warning("报告任务失败 %s：%s %s", report_id, code, message[:300])
            _publish_report_event(
                "report.failed",
                "error",
                f"report:{report_id}:failed",
                snapshot,
                message=f"报告生成失败：{message[:300]}",
                error=f"{code}: {message}"[:500],
            )
        except Exception:  # noqa: BLE001
            log.exception("标记报告失败状态异常：%s", report_id)

    def _public(self, task: Dict[str, Any]) -> Dict[str, Any]:
        report_id = str(task.get("report_id"))
        versions = []
        for item in task.get("versions") or []:
            target = int(item.get("version") or 0)
            path = self._report_dir(report_id) / str(item.get("file_name") or f"v{target}.html")
            versions.append(
                {
                    "version": target,
                    "bytes": int(item.get("bytes") or 0),
                    "html_sha256": item.get("html_sha256"),
                    "created_at": item.get("created_at"),
                    "ai_fallback_reason": item.get("ai_fallback_reason") or "",
                    "narrative_generated_by": item.get("narrative_generated_by") or "",
                    "engine": item.get("engine") or "",
                    "template_version": item.get("template_version") or "",
                    "html_available": path.is_file(),
                }
            )
        versions.sort(key=lambda item: int(item.get("version") or 0))
        latest_version = int(task.get("current_version") or (versions[-1]["version"] if versions else 0))
        latest = next((item for item in versions if item["version"] == latest_version), None)
        file_path: Optional[str] = None
        if latest_version > 0:
            # 公开 versions 不含内部 file_name，统一按 v{n}.html 定位本地最新版 HTML
            candidate = self._report_dir(report_id) / f"v{latest_version}.html"
            if candidate.is_file():
                file_path = str(candidate.resolve())
        # 历史/详情统一返回 title + meta（旧 meta.json 缺字段也能推导）
        title = str(task.get("title") or "").strip() or _default_title(task)
        source = str(task.get("source") or "").strip()
        if not source:
            # 兼容历史任务：按叙述来源推断（旧版远端报告统一视为 AI 报告）
            if (
                str(task.get("narrative_generated_by") or "") == LOCAL_NARRATIVE_GENERATED_BY
                or str(task.get("local_engine") or "")
            ):
                source = LOCAL_FALLBACK_SOURCE
            elif task.get("remote_report_id") or str(task.get("narrative_generated_by") or "") == "local-llm":
                source = LOCAL_AI_SOURCE
            else:
                source = LOCAL_FALLBACK_SOURCE
        window = {
            "start": str(task.get("window_start") or ""),
            "end": str(task.get("window_end") or ""),
        }
        meta = {
            "window": window,
            "keywords": list(task.get("keywords") or []),
            "platforms": list(task.get("platforms") or []),
            "counts": dict(task.get("counts") or {}),
            "narrative_generated_by": str(task.get("narrative_generated_by") or ""),
            "ai_fallback_reason": str(task.get("ai_fallback_reason") or ""),
            "source": source,
            "remote_report_id": str(task.get("remote_report_id") or ""),
            "stats_fingerprint": str(task.get("stats_fingerprint") or ""),
            "version": latest_version,
            "local_engine": str(task.get("local_engine") or ""),
            "template_version": str(task.get("template_version") or ""),
        }
        return {
            "report_id": report_id,
            "title": title,
            "status": str(task.get("status") or ""),
            "stage": str(task.get("stage") or ""),
            "progress": int(task.get("progress") or 0),
            "window_start": str(task.get("window_start") or ""),
            "window_end": str(task.get("window_end") or ""),
            "keywords": list(task.get("keywords") or []),
            "platforms": list(task.get("platforms") or []),
            "platform_labels": {
                code: PLATFORM_LABELS.get(code, code) for code in task.get("platforms") or []
            },
            "created_at": str(task.get("created_at") or ""),
            "updated_at": str(task.get("updated_at") or ""),
            "remote_report_id": str(task.get("remote_report_id") or ""),
            "current_version": latest_version,
            "version": latest_version,
            "versions": versions,
            "html_available": bool(latest and latest.get("html_available")),
            "file_path": file_path,
            "ai_fallback_reason": str(task.get("ai_fallback_reason") or ""),
            "narrative_generated_by": str(task.get("narrative_generated_by") or ""),
            "source": source,
            "meta": meta,
            "stats_fingerprint": str(task.get("stats_fingerprint") or ""),
            "counts": dict(task.get("counts") or {}),
            "error": task.get("error") if isinstance(task.get("error"), dict) else None,
        }


    # -- 参数解析 ---------------------------------------------------------
    def _parse_window(self, body: Dict[str, Any]) -> Tuple[str, str]:
        today = datetime.now().date()
        end = _parse_date_value(body.get("window_end"), today)
        start_raw = body.get("window_start")
        if start_raw is None or (isinstance(start_raw, str) and not start_raw.strip()):
            start = end - timedelta(days=DEFAULT_WINDOW_DAYS - 1)
        else:
            start = _parse_date_value(start_raw, end)
        if start > end:
            raise ServiceError(
                "INVALID_PARAMS",
                f"window_start（{start.isoformat()}）不能晚于 window_end（{end.isoformat()}）",
                400,
            )
        if (end - start).days + 1 > MAX_WINDOW_DAYS:
            raise ServiceError(
                "INVALID_PARAMS", f"时间窗口不能超过 {MAX_WINDOW_DAYS} 天", 400
            )
        return start.isoformat(), end.isoformat()

    def _parse_keywords(self, value: Any) -> List[str]:
        if value is None:
            return self._default_keywords()
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ServiceError("INVALID_PARAMS", "keywords 必须是字符串数组", 400)
        keywords: List[str] = []
        for item in value:
            text = str(item or "").strip()
            if text and text not in keywords:
                keywords.append(text)
        if not keywords:
            return self._default_keywords()
        if len(keywords) > 20:
            raise ServiceError("INVALID_PARAMS", "keywords 最多 20 个", 400)
        return keywords

    def _parse_platforms(self, value: Any) -> List[str]:
        if value is None or (isinstance(value, (list, tuple)) and not value):
            return list(ALL_PLATFORMS)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            raise ServiceError("INVALID_PARAMS", "platforms 必须是字符串数组", 400)
        platforms: List[str] = []
        for item in value:
            code = str(item or "").strip()
            if not code:
                continue
            if code not in ALL_PLATFORMS:
                raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{code}", 400)
            if code not in platforms:
                platforms.append(code)
        return platforms or list(ALL_PLATFORMS)

    def _default_keywords(self) -> List[str]:
        """默认关键词：取 media_crawler.db 内容表中 distinct 非空 source_keyword（≤20）。

        report_data 按 source_keyword 过滤；多关键词采集时同一内容的 source_keyword
        只会保留一个，所以必须把库里出现过的 source_keyword 都带上，否则会漏数据。
        读不到时返回 ``[]``（表示全部，由 report_data 按全量统计）。
        """
        media_db = Path(self.cfg.media_db)
        if not media_db.is_file():
            return []
        keywords: List[str] = []
        try:
            conn = sqlite3.connect(f"file:{media_db}?mode=ro", uri=True, timeout=2.0)
            try:
                for table in CONTENT_TABLES:
                    try:
                        rows = conn.execute(
                            f"SELECT DISTINCT source_keyword FROM {table} "
                            "WHERE source_keyword IS NOT NULL AND TRIM(source_keyword) <> ''"
                        ).fetchall()
                    except sqlite3.Error:
                        continue
                    for row in rows:
                        text = str(row[0] or "").strip()
                        if text and text not in keywords:
                            keywords.append(text)
                        if len(keywords) >= 20:
                            break
                    if len(keywords) >= 20:
                        break
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - 默认关键词失败不阻断
            log.debug("读取默认关键词失败，按全部数据统计", exc_info=True)
            return []
        return keywords[:20]


__all__ = ["ReportManager", "ALL_PLATFORMS", "TERMINAL_STATUSES"]

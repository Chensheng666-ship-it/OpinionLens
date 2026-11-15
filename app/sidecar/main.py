# -*- coding: utf-8 -*-
"""舆见 OpinionLens · 桌面端本地 sidecar（FastAPI + uvicorn）。

启动方式（契约 §2.1）::

    python app/sidecar/main.py --port 0 --token <random> --data-dir <abs>

启动成功后 stdout 只打印一行::

    ##SIDECAR_READY##{"port": 51234}

约定：
* 只监听 ``127.0.0.1``；
* ``/health`` 免鉴权，``/api/*`` 全部要求 ``Authorization: Bearer <token>``；
* CORS 全开（Electron renderer 的 Origin 是 ``file://`` → ``null``）；
* 错误统一 ``{"error":{"code":"...","message":"..."}}``；
* 业务能力全部在 ``engine_runner.EngineService``，本文件只做 HTTP 适配。

接口清单见 ``app/CONTRACT.md`` §2.2，字段名不可改。
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

# 允许 `python -m app.sidecar.main` 等启动方式
_SIDECAR_DIR = Path(__file__).resolve().parent
if str(_SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(_SIDECAR_DIR))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from engine_runner import (  # noqa: E402
    PLATFORM_LABELS,
    SIDECAR_VERSION,
    EngineService,
    ServiceError,
    SidecarConfig,
)
from crawl_options_loader import crawl_options  # noqa: E402  (采集高级参数单一来源：app/engine/crawl_options.py)
from events import bus  # noqa: E402  (进程内事件总线：采集/报告/账号事件 → 通知服务)
from llm_client import LLMService  # noqa: E402  (模型 API 配置）
from notify_service import NotifyService  # noqa: E402  (通知推送）
from report_manager import ReportManager  # noqa: E402
from api_notify import router as notify_router  # noqa: E402
from api_pool import router as pool_router  # noqa: E402
from api_providers import router as providers_router  # noqa: E402

log = logging.getLogger("sidecar")

READY_PREFIX = "##SIDECAR_READY##"
DEFAULT_HOST = "127.0.0.1"


# ---------------------------------------------------------------------------
# 请求模型（字段与契约 §2.2 对齐）
# ---------------------------------------------------------------------------
class OpenFileRequest(BaseModel):
    path: str = ""


class RunCreateRequest(BaseModel):
    keywords: List[str] = Field(default_factory=list)
    platforms: List[str] = Field(default_factory=list)
    max_notes: int = 15
    comments: bool = True
    max_comments: int = 20
    name: Optional[str] = None
    # 采集高级参数（可选；键与范围见 app/engine/crawl_options.py OPTION_SPECS）
    options: Optional[Dict[str, Any]] = None


class RunResumeRequest(BaseModel):
    """断点续采请求体；可整体省略（不传 body / 传 {} / platforms 为空 → 默认续跑未完成平台）。"""

    platforms: Optional[List[str]] = None


class ReportCreateRequest(BaseModel):
    """报告中心：生成报告（窗口默认最近 7 天、平台默认全部、关键词默认取最近采集任务）。"""

    window_start: Optional[Any] = None
    window_end: Optional[Any] = None
    keywords: Optional[List[str]] = None
    platforms: Optional[List[str]] = None


class ReportReviseRequest(BaseModel):
    """报告中心：修订（instruction 必填；base_version 缺省 = 当前版本）。"""

    instruction: str = ""
    base_version: Optional[int] = None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


_DEFAULT_HTTP_CODES = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "INVALID_PARAMS",
    500: "INTERNAL",
    503: "SERVICE_UNAVAILABLE",
}


def _require_platform(platform: str) -> str:
    platform = str(platform or "").strip()
    if platform not in PLATFORM_LABELS:
        raise ServiceError("PLATFORM_NOT_FOUND", f"不支持的平台：{platform}", 404)
    return platform


def _optional_cursor_param(raw: Optional[str], name: str) -> Optional[int]:
    """查询参数 → 可选非负整数游标（``since_seq`` / ``after_line``）。

    * 不传 / 空串 → ``None``（= 首屏 / 尾部语义，行为与改造前一致）；
    * 负数、非整数（``1.5``/``abc``）→ 400 ``invalid_params``（不落 422，便于前端识别）。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text, 10)
    except (TypeError, ValueError) as exc:
        raise ServiceError(
            "invalid_params", f"{name} 必须是非负整数：{text[:40]}", 400
        ) from exc
    if value < 0:
        raise ServiceError("invalid_params", f"{name} 不能为负数：{value}", 400)
    return value


def _normalize_run_request(payload: RunCreateRequest) -> Dict[str, Any]:
    """把请求体规范化为内部 params（keywords 支持中英文逗号拆分/去重）。"""
    keywords: List[str] = []
    for raw in payload.keywords or []:
        for part in str(raw).replace("，", ",").split(","):
            keyword = part.strip()
            if keyword and keyword not in keywords:
                keywords.append(keyword)
    if not keywords:
        raise ServiceError("INVALID_PARAMS", "keywords 不能为空", 400)

    platforms: List[str] = []
    for raw in payload.platforms or []:
        platform = str(raw or "").strip()
        if not platform:
            continue
        if platform not in PLATFORM_LABELS:
            raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
        if platform not in platforms:
            platforms.append(platform)
    if not platforms:
        raise ServiceError("INVALID_PARAMS", "platforms 不能为空", 400)

    try:
        max_notes = int(payload.max_notes)
    except (TypeError, ValueError):
        max_notes = 0
    if max_notes <= 0:
        raise ServiceError("INVALID_PARAMS", "max_notes 必须为正整数", 400)
    max_notes = min(max_notes, 1000)

    try:
        max_comments = int(payload.max_comments)
    except (TypeError, ValueError):
        max_comments = 0
    max_comments = max(0, min(max_comments, 1000))
    if not payload.comments:
        max_comments = 0

    params: Dict[str, Any] = {
        "keywords": keywords,
        "platforms": platforms,
        "max_notes": max_notes,
        "comments": bool(payload.comments),
        "max_comments": max_comments,
    }
    name = (payload.name or "").strip()
    if name:
        params["name"] = name
    try:
        options = crawl_options.normalize_options(payload.options)
    except ValueError as exc:
        raise ServiceError("INVALID_PARAMS", f"options 无效：{exc}", 400) from exc
    if options:
        # 只在显式给出时写入，旧任务 params 不变（续采不会被新默认值悄悄改掉）
        params["options"] = options
    return params


def _open_path(path: Path) -> None:
    """用系统默认程序打开文件（selftest 可用 SIDECAR_DISABLE_OPEN=1 跳过）。"""
    if os.environ.get("SIDECAR_DISABLE_OPEN") == "1":
        log.info("SIDECAR_DISABLE_OPEN=1，跳过实际打开：%s", path)
        return
    if os.name == "nt":
        os.startfile(str(path))
        return
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
        return
    subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
def create_app(cfg: SidecarConfig, token: str) -> FastAPI:
    service = EngineService(cfg)
    # 模型 API：用户自己的 OpenAI 兼容 / DeepSeek / Anthropic 配置（密钥 DPAPI 加密存储）
    llm_service = LLMService(cfg, state=service.state)
    # 通知推送：订阅进程内事件总线，outbox 落库后由后台线程发送
    notify_service = NotifyService(cfg, state=service.state)
    bus.subscribe(notify_service.on_event)
    # 报告中心：数据包 → 用户配置的模型（未配置时本地模板降级）+ 本地报告任务队列
    report_manager = ReportManager(cfg, llm=llm_service)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        notify_service.start()
        log.info(
            "sidecar 就绪：data_dir=%s mc_root=%s python=%s engine=%s notify=%s reports=%s",
            cfg.data_dir,
            cfg.mc_root,
            cfg.python,
            cfg.engine_entry,
            "on" if getattr(notify_service, "enabled", True) else "off",
            "configured" if getattr(report_manager, "configured", False) else "not_configured",
        )
        # 模型 API 连接详情只写日志，不占用启动时间/不打扰界面（UI 只说“未配置模型 API”）
        def _probe_report_connection() -> None:
            try:
                info = report_manager.connection_info()
                if info.get("connected"):
                    log.info(
                        "[reports] 模型 API 连接检测：已连接 base_url=%s model=%s latency_ms=%s",
                        info.get("base_url") or "",
                        info.get("model") or "",
                        info.get("latency_ms"),
                    )
                else:
                    log.warning(
                        "[reports] 模型 API 连接检测：未配置/不可用 base_url=%s reason=%s；报告将使用本地模板",
                        info.get("base_url") or "",
                        info.get("error") or "",
                    )
            except Exception:  # noqa: BLE001 - 连接检测失败不影响 sidecar
                log.exception("[reports] 模型 API 连接检测异常（忽略）")

        threading.Thread(
            target=_probe_report_connection, name="report-conn-probe", daemon=True
        ).start()
        try:
            yield
        finally:
            # 先停事件消费/通知与报告后台线程，最后关业务服务（StateStore）
            bus.unsubscribe(notify_service.on_event)
            try:
                notify_service.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("关闭通知服务失败")
            try:
                report_manager.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("关闭报告中心失败")
            service.shutdown()

    app = FastAPI(
        title="舆见 OpinionLens Sidecar",
        version=SIDECAR_VERSION,
        lifespan=lifespan,
        # 本地服务不需要 OpenAPI 文档面（且这些路径不在 /api 鉴权下，直接关闭）
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.service = service
    app.state.cfg = cfg
    app.state.token = token
    app.state.notify = notify_service
    app.state.llm = llm_service
    app.state.report_manager = report_manager

    # ---- 鉴权中间件（先注册，CORS 后注册 → CORS 在最外层，401 也带 CORS 头）
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path == "/health" or not path.startswith("/api"):
            return await call_next(request)
        header = request.headers.get("authorization", "")
        expected = f"Bearer {token}"
        try:
            authorized = bool(token) and hmac.compare_digest(
                header.encode("utf-8"), expected.encode("utf-8")
            )
        except (TypeError, ValueError):
            authorized = False
        if not authorized:
            return _error_response(401, "UNAUTHORIZED", "缺少或无效的访问令牌")
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
        max_age=600,
    )

    # ---- 统一错误体 ----
    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError):
        return _error_response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        errors = exc.errors() or []
        first = errors[0] if errors else {}
        loc = ".".join(str(item) for item in (first.get("loc") or []) if item != "body")
        message = f"参数校验失败：{loc or 'body'} {first.get('msg') or ''}".strip()
        return _error_response(422, "INVALID_PARAMS", message)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            return _error_response(int(exc.status_code), str(detail["code"]), str(detail["message"]))
        code = _DEFAULT_HTTP_CODES.get(int(exc.status_code), "HTTP_ERROR")
        return _error_response(int(exc.status_code), code, str(detail))

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        log.exception("未处理异常：%s %s", request.method, request.url.path)
        return _error_response(500, "INTERNAL", f"服务内部错误：{exc}")

    # ---- 健康检查 / 基础信息 ----
    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {"ok": True}

    @app.get("/api/config")
    def get_config() -> Dict[str, Any]:
        return service.config_info()

    @app.get("/api/system/info")
    def system_info() -> Dict[str, Any]:
        return service.system_info()

    @app.post("/api/system/open-file")
    def system_open_file(payload: OpenFileRequest) -> Dict[str, Any]:
        raw = (payload.path or "").strip()
        if not raw:
            raise ServiceError("INVALID_PARAMS", "path 不能为空", 400)
        target = Path(raw)
        if not target.is_absolute():
            target = cfg.data_dir / target
        try:
            resolved = target.resolve()
        except OSError:
            resolved = Path(os.path.abspath(str(target)))
        exports_root = cfg.exports_dir.resolve()
        try:
            resolved.relative_to(exports_root)
        except ValueError as exc:
            raise ServiceError(
                "FORBIDDEN", "仅允许打开导出目录（<data-dir>/exports）下的文件", 403
            ) from exc
        if not resolved.is_file():
            raise ServiceError("FILE_NOT_FOUND", f"文件不存在：{resolved}", 404)
        _open_path(resolved)
        return {"ok": True}

    # ---- 平台账号 ----
    @app.get("/api/accounts")
    def list_accounts() -> Dict[str, Any]:
        return {"accounts": service.list_accounts()}

    @app.post("/api/accounts/{platform}/login")
    def start_login(platform: str) -> Dict[str, Any]:
        platform = _require_platform(platform)
        return service.start_login(platform)

    @app.get("/api/accounts/{platform}/login/{login_id}")
    def get_login(platform: str, login_id: str) -> Dict[str, Any]:
        platform = _require_platform(platform)
        return service.get_login(platform, login_id)

    @app.post("/api/accounts/{platform}/login/{login_id}/cancel")
    def cancel_login(platform: str, login_id: str) -> Dict[str, Any]:
        platform = _require_platform(platform)
        return service.cancel_login(platform, login_id)

    @app.post("/api/accounts/{platform}/test")
    def test_account(platform: str) -> Dict[str, Any]:
        platform = _require_platform(platform)
        return service.test_connection(platform)

    @app.delete("/api/accounts/{platform}")
    def delete_account(platform: str) -> Dict[str, Any]:
        platform = _require_platform(platform)
        return service.delete_account(platform)

    # ---- 采集任务 ----
    @app.post("/api/runs")
    def create_run(payload: RunCreateRequest) -> Dict[str, Any]:
        params = _normalize_run_request(payload)
        return service.create_run(params)

    @app.get("/api/runs/options")
    def run_option_catalog() -> Dict[str, Any]:
        """采集高级参数目录（前端据此渲染表单；与后端校验同源）。"""
        return {"options": crawl_options.option_catalog()}

    @app.get("/api/runs")
    def list_runs() -> Dict[str, Any]:
        return {"runs": service.list_runs()}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> Dict[str, Any]:
        return service.get_run(run_id)

    @app.post("/api/runs/{run_id}/resume")
    def resume_run(run_id: str, payload: Optional[RunResumeRequest] = None) -> Dict[str, Any]:
        # body 可为空（前端「继续未完成平台」可不传 body；「继续此平台」传 {"platforms":[p]}）
        platforms = payload.platforms if payload is not None else None
        return service.resume_run(run_id, platforms)

    @app.get("/api/runs/{run_id}/logs")
    def get_run_logs(
        run_id: str, tail: int = 200, after_line: Optional[str] = None
    ) -> Dict[str, Any]:
        # 增量（M5a）：after_line = 0 基行号，只返回该行之后的新行；响应 {lines, next_line}
        tail_int = max(1, min(int(tail or 200), 5000))
        cursor = _optional_cursor_param(after_line, "after_line")
        return service.get_run_logs(run_id, tail_int, cursor)

    @app.post("/api/runs/{run_id}/stop")
    def stop_run(run_id: str) -> Dict[str, Any]:
        return service.stop_run(run_id)

    @app.post("/api/runs/{run_id}/export")
    def export_run(run_id: str) -> Dict[str, Any]:
        return service.export_run(run_id)

    @app.get("/api/runs/{run_id}/data")
    def preview_run(run_id: str, limit: int = 20) -> Dict[str, Any]:
        limit = max(1, min(int(limit or 20), 200))
        return service.preview_run(run_id, limit)

    @app.get("/api/runs/{run_id}/feed")
    def get_run_feed(
        run_id: str, limit: int = 30, comments: int = 3, since_seq: Optional[str] = None
    ) -> Dict[str, Any]:
        # 「采集动态」内容流：limit 1–100（默认 30）/ comments 0–10（默认 3），越界由服务层 clamp
        # 增量（M5a）：since_seq = 上次响应的 next_seq，只返回 seq 更大的新条目 + next_seq
        cursor = _optional_cursor_param(since_seq, "since_seq")
        return service.get_run_feed(run_id, limit, comments, cursor)

    # ---- 报告中心 -------------------------------------------
    @app.get("/api/reports/config")
    def reports_config() -> Dict[str, Any]:
        return report_manager.config_info()

    @app.get("/api/reports/keywords")
    def reports_keywords() -> Dict[str, Any]:
        return report_manager.available_keywords()

    @app.get("/api/reports/capabilities")
    def reports_capabilities() -> Dict[str, Any]:
        # 设置页「测试连接」：不抛异常，始终 200 + {ok,error?}
        return report_manager.test_connection()

    @app.get("/api/reports/connection")
    def reports_connection() -> Dict[str, Any]:
        # 自动连接检测：短超时（4s），未配置/未连接也 200，UI 可自动轮询
        return report_manager.connection_info()

    @app.post("/api/reports", status_code=202)
    def create_report(payload: Optional[ReportCreateRequest] = None) -> Dict[str, Any]:
        body = payload.model_dump(exclude_none=True) if payload is not None else {}
        return report_manager.create_report(body)

    @app.get("/api/reports")
    def list_reports() -> Dict[str, Any]:
        return report_manager.list_reports()

    @app.get("/api/reports/{report_id}")
    def get_report(report_id: str) -> Dict[str, Any]:
        return report_manager.get_report(report_id)

    @app.get("/api/reports/{report_id}/html")
    def get_report_html(report_id: str, version: Optional[int] = None) -> Dict[str, Any]:
        return report_manager.get_html(report_id, version)

    @app.post("/api/reports/{report_id}/cancel")
    def cancel_report(report_id: str) -> Dict[str, Any]:
        return report_manager.cancel_report(report_id)

    @app.post("/api/reports/{report_id}/revise", status_code=202)
    def revise_report(report_id: str, payload: ReportReviseRequest) -> Dict[str, Any]:
        return report_manager.revise_report(
            report_id, payload.instruction, payload.base_version
        )

    @app.post("/api/reports/{report_id}/open")
    def open_report(report_id: str) -> Dict[str, Any]:
        return report_manager.open_report(report_id)

    # ---- 设置中心：通用信息 / 通知 / 模型 摘要（设置页首屏用） ----
    @app.get("/api/settings")
    def get_settings_summary() -> Dict[str, Any]:
        return {
            "app": {
                "version": SIDECAR_VERSION,
                "data_dir": str(cfg.data_dir),
                "platforms": PLATFORM_LABELS,
                "login_headless": bool(getattr(cfg, "login_headless", False)),
            },
            "notify": notify_service.config_summary(),
            "llm": llm_service.config_summary(),
            "reports": report_manager.config_info(),
        }

    # ---- 设置中心：模型 API / 通知推送 / 多账号号池（新功能路由） ----
    app.include_router(providers_router)
    app.include_router(notify_router)
    app.include_router(pool_router)

    return app


# ---------------------------------------------------------------------------
# uvicorn 封装：绑定成功后打印 ##SIDECAR_READY##
# ---------------------------------------------------------------------------
class SidecarServer(uvicorn.Server):
    """在 socket 真正绑定成功后回调 ``on_ready(port)``，保证 ready 行里是真实端口。"""

    def __init__(self, config: uvicorn.Config, on_ready) -> None:
        super().__init__(config)
        self._on_ready = on_ready
        self._ready_reported = False

    async def startup(self, sockets=None) -> None:
        await super().startup(sockets=sockets)
        if not self.started or self._ready_reported:
            return
        self._ready_reported = True
        self._on_ready(self._bound_port())

    def _bound_port(self) -> int:
        try:
            for server in getattr(self, "servers", []) or []:
                for sock in server.sockets or []:
                    return int(sock.getsockname()[1])
        except Exception:  # noqa: BLE001 - 取端口失败时回退配置端口
            log.exception("读取监听端口失败")
        return int(getattr(self.config, "port", 0) or 0)


def _emit_ready(port: int) -> None:
    try:
        sys.stdout.write(READY_PREFIX + json.dumps({"port": int(port)}) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - ready 行失败不应影响服务
        log.exception("输出 SIDECAR_READY 失败")


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="舆见 OpinionLens sidecar（FastAPI 本地服务）"
    )
    parser.add_argument("--port", type=int, default=0, help="监听端口，0=自动分配（契约默认）")
    parser.add_argument("--token", required=True, help="Bearer 鉴权 token（由 Electron 主进程随机生成）")
    parser.add_argument("--data-dir", required=True, help="运行期数据目录（绝对路径，契约约定）")
    parser.add_argument("--mc-root", default=None, help="覆盖 MediaCrawler 根目录（默认 <project>/MediaCrawler）")
    parser.add_argument("--log-level", default="INFO", help="sidecar 日志级别，默认 INFO")
    return parser.parse_args(argv)


def _reconfigure_stdio() -> None:
    """Windows 控制台默认 GBK：统一改为 UTF-8，避免中文日志 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            # sys.stdout/stderr 的静态类型是 TextIO（没有 reconfigure）；非标准流由 except 兜底
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]  # 原因：sys.stdout/stderr 静态类型 TextIO 没有 reconfigure，非标准流由 except 兜底
        except Exception:  # noqa: BLE001 - 非标准流（如被重定向为 BytesIO）时忽略
            pass


def setup_logging(cfg: SidecarConfig, level: str = "INFO") -> None:
    root = logging.getLogger()
    if getattr(root, "_sidecar_configured", False):  # 避免重复添加 handler
        return
    root.setLevel(getattr(logging, str(level or "INFO").upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setFormatter(fmt)
    root.addHandler(stream_handler)
    try:
        file_handler = logging.FileHandler(
            str(cfg.logs_dir / "sidecar.log"), encoding="utf-8", delay=True
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass
    # uvicorn 自身日志压到 WARNING，避免 ready 行前后噪音；访问日志关闭
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    root._sidecar_configured = True  # type: ignore[attr-defined]  # 原因：给 logging root 挂去重标记（Logger 无该属性声明）


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    _reconfigure_stdio()
    data_dir = Path(args.data_dir).expanduser().resolve()
    cfg = SidecarConfig.from_env(data_dir, mc_root=args.mc_root)
    cfg.ensure_dirs()
    setup_logging(cfg, args.log_level)

    token = (args.token or "").strip()
    if not token:
        log.error("--token 不能为空（全部 /api/* 接口依赖 Bearer 鉴权）")
        return 2

    log.info(
        "启动 sidecar 版本 %s：port=%s data_dir=%s mc_root=%s",
        SIDECAR_VERSION,
        args.port,
        cfg.data_dir,
        cfg.mc_root,
    )
    app = create_app(cfg, token)
    config = uvicorn.Config(
        app,
        host=DEFAULT_HOST,
        port=int(args.port),
        log_level="warning",
        log_config=None,  # 日志由 setup_logging 统一管理
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=3,
    )
    server = SidecarServer(config, _emit_ready)
    exit_code = 0
    try:
        server.run()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，sidecar 退出")
    except SystemExit as exc:  # uvicorn 绑定端口失败时 sys.exit(1)
        exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    except Exception:  # noqa: BLE001
        log.exception("sidecar 运行异常")
        exit_code = 1
    finally:
        try:
            app.state.notify.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("关闭通知服务失败")
        try:
            app.state.report_manager.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("关闭报告中心失败")
        try:
            app.state.service.shutdown()
        except Exception:  # noqa: BLE001
            log.exception("关闭 sidecar 失败")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""多账号号池 REST 路由（Phase C3 实现）。

路径：``/api/accounts/pool/*`` 与 ``/api/accounts/schedule/preview``；
旧 ``/api/accounts/*`` 路由保持兼容，由 main.py 原样保留（这里不重复注册）。

约定（契约 IMPLEMENTATION_CONTRACT §2.4 / §6.2）：
* ``request.app.state.service`` 是 ``engine_runner.EngineService``；
* 全局 Bearer 中间件已覆盖 ``/api/*``，路由内不做鉴权；
* 业务错误统一 ``raise ServiceError(code, message, status)``，错误体由 main.py 的
  exception handler 统一转 ``{"error":{"code","message"}}``；
* 路由全部同步 def（FastAPI 线程池执行），test 连接等阻塞动作不卡事件循环。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from typing_utils import as_list

from fastapi import APIRouter, Request
from pydantic import BaseModel

from engine_runner import PLATFORM_LABELS, ServiceError

log = logging.getLogger("sidecar.api_pool")

router = APIRouter()


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class AccountCreateRequest(BaseModel):
    platform: str = ""
    alias: str = ""
    proxy_url: Optional[str] = None
    is_default: Optional[bool] = None


class AccountPatchRequest(BaseModel):
    """PATCH 支持两种启用/禁用写法：``enabled: bool`` 或 ``status: "active|disabled"``。"""

    alias: Optional[str] = None
    status: Optional[str] = None
    enabled: Optional[bool] = None
    risk_note: Optional[str] = None
    risk_level: Optional[str] = None
    is_default: Optional[bool] = None
    proxy_url: Optional[str] = None
    health_score: Optional[int] = None


class LoginCancelRequest(BaseModel):
    login_id: str = ""


class PoolTestRequest(BaseModel):
    account_ids: Optional[List[str]] = None


class PoolSettingsRequest(BaseModel):
    default_strategy: Optional[str] = None
    limits: Optional[Dict[str, int]] = None
    platform_min_interval_s: Optional[int] = None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _service(request: Request) -> Any:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise ServiceError("SERVICE_UNAVAILABLE", "sidecar 服务未就绪", 503)
    return service


def _accounts(request: Request) -> Any:
    service = _service(request)
    manager = getattr(service, "accounts", None)
    if manager is None:
        raise ServiceError("SERVICE_UNAVAILABLE", "账号池模块未就绪", 503)
    return manager


def _require_platform(platform: str) -> str:
    platform = str(platform or "").strip()
    if platform not in PLATFORM_LABELS:
        raise ServiceError("INVALID_PLATFORM", f"不支持的平台：{platform}", 400)
    return platform


def _resolve_account(request: Request, account_id: str) -> Dict[str, Any]:
    return _accounts(request).get(account_id)


# ---------------------------------------------------------------------------
# 账号 CRUD / 列表
# ---------------------------------------------------------------------------
@router.get("/api/accounts/pool")
def list_pool_accounts(request: Request, platform: Optional[str] = None) -> Dict[str, Any]:
    """账号池列表（含配额/健康度/冷却；不含 archived）。"""
    manager = _accounts(request)
    platform_id = _require_platform(platform) if platform else ""
    accounts = manager.list(platform_id or None)
    settings = manager.settings()
    return {
        "accounts": accounts,
        "total": len(accounts),
        "default_strategy": settings["default_strategy"],
        "limits": settings["limits"],
        "platform_min_interval_s": settings["platform_min_interval_s"],
        "platforms": dict(PLATFORM_LABELS),
    }


@router.post("/api/accounts/pool", status_code=201)
def create_pool_account(request: Request, payload: AccountCreateRequest) -> Dict[str, Any]:
    """新建账号（默认 v2 独立 profile；该平台第一个账号自动成为默认）。"""
    manager = _accounts(request)
    platform = _require_platform(payload.platform)
    return manager.create(
        platform,
        alias=payload.alias or "",
        proxy_url=payload.proxy_url,
        is_default=payload.is_default,
    )


@router.get("/api/accounts/pool/settings")
def get_pool_settings(request: Request) -> Dict[str, Any]:
    return _accounts(request).settings()


@router.put("/api/accounts/pool/settings")
def update_pool_settings(request: Request, payload: PoolSettingsRequest) -> Dict[str, Any]:
    manager = _accounts(request)
    body = payload.model_dump(exclude_none=True)
    return manager.update_settings(body)


@router.post("/api/accounts/pool/test")
def batch_test_pool_accounts(
    request: Request, payload: Optional[PoolTestRequest] = None
) -> Dict[str, Any]:
    """批量检测（服务端串行；UI 可自行并发 2 个）。缺省=全部未归档账号。"""
    service = _service(request)
    manager = _accounts(request)
    wanted = [str(item) for item in (as_list(payload.account_ids) if payload else [])]
    accounts = manager.list(include_archived=False)
    if wanted:
        wanted_set = set(wanted)
        accounts = [item for item in accounts if item["account_id"] in wanted_set]
        missing = wanted_set - {item["account_id"] for item in accounts}
        if missing:
            raise ServiceError(
                "ACCOUNT_NOT_FOUND", "账号不存在：" + ",".join(sorted(missing)), 404
            )
    results: List[Dict[str, Any]] = []
    for account in accounts:
        item: Dict[str, Any] = {
            "account_id": account["account_id"],
            "account_alias": account.get("display_alias") or account.get("alias") or "",
            "platform": account.get("platform") or "",
        }
        try:
            item.update(
                service.test_connection(account["platform"], account["account_id"])
            )
        except ServiceError as exc:
            item.update(
                {
                    "ok": False,
                    "message": exc.message,
                    "error_code": exc.code,
                    "duration_ms": 0,
                }
            )
        results.append(item)
    ok_count = sum(1 for item in results if item.get("ok"))
    return {
        "results": results,
        "ok_count": ok_count,
        "fail_count": len(results) - ok_count,
        "total": len(results),
    }


@router.get("/api/accounts/schedule/preview")
def preview_schedule(
    request: Request, platform: Optional[str] = None, strategy: Optional[str] = None
) -> Dict[str, Any]:
    """干跑选号（不写库/不占锁）：``?platform=&strategy=``。"""
    manager = _accounts(request)
    platform_id = _require_platform(platform) if platform else None
    return manager.preview(platform_id, strategy)


@router.get("/api/accounts/pool/{account_id}")
def get_pool_account(request: Request, account_id: str) -> Dict[str, Any]:
    return _resolve_account(request, account_id)


@router.patch("/api/accounts/pool/{account_id}")
def update_pool_account(
    request: Request, account_id: str, payload: AccountPatchRequest
) -> Dict[str, Any]:
    """更新账号（alias/enabled|status/is_default/risk_note/proxy_url/health_score）。"""
    manager = _accounts(request)
    fields = payload.model_dump(exclude_unset=True)
    # 兼容两种启用/禁用写法：enabled:bool 优先于 status
    if "enabled" in fields and fields["enabled"] is not None:
        fields["status"] = "active" if fields["enabled"] else "disabled"
        fields.pop("enabled", None)
    return manager.update(account_id, **fields)


@router.delete("/api/accounts/pool/{account_id}")
def delete_pool_account(
    request: Request, account_id: str, purge: bool = False
) -> Dict[str, Any]:
    """软删账号（archived，保留 run 历史）；``?purge=true`` 同时删除 profile 目录。"""
    manager = _accounts(request)
    return manager.archive(account_id, purge_profile=bool(purge))


# ---------------------------------------------------------------------------
# 账号级登录
# ---------------------------------------------------------------------------
@router.post("/api/accounts/pool/{account_id}/login")
def start_pool_login(request: Request, account_id: str) -> Dict[str, Any]:
    service = _service(request)
    account = _resolve_account(request, account_id)
    result = dict(service.start_login(account["platform"], account_id) or {})
    result.setdefault("account_id", account_id)
    result.setdefault("platform", account["platform"])
    result.setdefault("account_alias", account.get("display_alias") or "")
    return result


@router.get("/api/accounts/pool/{account_id}/login/{login_id}")
def get_pool_login(request: Request, account_id: str, login_id: str) -> Dict[str, Any]:
    service = _service(request)
    account = _resolve_account(request, account_id)
    session = service.get_login(account["platform"], login_id)
    if session.get("account_id") and session.get("account_id") != account_id:
        raise ServiceError("LOGIN_NOT_FOUND", f"登录会话不属于该账号：{login_id}", 404)
    result = dict(session)
    result.setdefault("account_id", account_id)
    result.setdefault("account_alias", account.get("display_alias") or "")
    return result


@router.post("/api/accounts/pool/{account_id}/login/{login_id}/cancel")
def cancel_pool_login(request: Request, account_id: str, login_id: str) -> Dict[str, Any]:
    service = _service(request)
    account = _resolve_account(request, account_id)
    service.get_login(account["platform"], login_id)  # 校验归属（不存在抛 404）
    return service.cancel_login(account["platform"], login_id)


@router.post("/api/accounts/pool/{account_id}/cancel")
def cancel_pool_login_by_body(
    request: Request,
    account_id: str,
    payload: Optional[LoginCancelRequest] = None,
    login_id: Optional[str] = None,
) -> Dict[str, Any]:
    """便捷取消：``body{login_id}`` 或 ``?login_id=``。"""
    resolved = str(login_id or (payload.login_id if payload else "") or "").strip()
    if not resolved:
        raise ServiceError("INVALID_PARAMS", "login_id 不能为空", 400)
    return cancel_pool_login(request, account_id, resolved)


# ---------------------------------------------------------------------------
# 账号级测试 / 旧 profile 迁移
# ---------------------------------------------------------------------------
@router.post("/api/accounts/pool/{account_id}/test")
def test_pool_account(request: Request, account_id: str) -> Dict[str, Any]:
    service = _service(request)
    account = _resolve_account(request, account_id)
    return service.test_connection(account["platform"], account_id)


@router.post("/api/accounts/pool/{account_id}/adopt")
def adopt_pool_account(request: Request, account_id: str) -> Dict[str, Any]:
    """把旧单账号 profile 迁移到 v2 独立目录（幂等；仅空闲时可执行）。"""
    manager = _accounts(request)
    account = _resolve_account(request, account_id)
    return manager.adopt_legacy(platform=account["platform"], account_id=account_id)


# ---------------------------------------------------------------------------
# run 账号尝试明细（渲染层任务详情用；path 在 /api/runs 下，由本 router 提供）
# ---------------------------------------------------------------------------
@router.get("/api/runs/{run_id}/accounts")
def get_run_accounts(request: Request, run_id: str) -> Dict[str, Any]:
    service = _service(request)
    manager = _accounts(request)
    run = service.runs.get_run_raw(run_id) if hasattr(service, "runs") else None
    if run is None:
        raise ServiceError("RUN_NOT_FOUND", f"采集任务不存在：{run_id}", 404)
    attempts = manager.run_accounts(run_id)
    aliases = manager.aliases()
    platforms: List[Dict[str, Any]] = []
    for item in run.get("platforms") or []:
        account_id = str(item.get("account_id") or "")
        platforms.append(
            {
                "platform": item.get("platform"),
                "account_id": account_id,
                "account_alias": aliases.get(account_id, ""),
                "account_attempts": int(item.get("account_attempts") or 0),
                "status": item.get("status"),
            }
        )
    return {"run_id": run_id, "attempts": attempts, "platforms": platforms}

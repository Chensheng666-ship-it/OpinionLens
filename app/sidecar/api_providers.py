# -*- coding: utf-8 -*-
"""模型 API 配置 REST 路由（``/api/llm/providers*``）。

约定（与渲染层对齐）：
* 全局 ``/api/*`` Bearer 中间件已鉴权，本模块不再做鉴权；
* API Key 只写不读：列表/详情只返回 ``api_key_masked`` / ``secret_configured``；
* ``PATCH`` 不传 ``api_key`` = 保留原密钥；``clear_secret: true`` 显式清空；
* 错误统一 ``ServiceError`` → ``{"error":{"code","message"}}``。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Request

from engine_runner import ServiceError
from llm_client import LLMError, LLMService

router = APIRouter()


def _llm(request: Request) -> LLMService:
    service = getattr(request.app.state, "llm", None)
    if service is None:
        raise ServiceError("LLM_UNAVAILABLE", "模型 API 服务未就绪", 503)
    return service


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001 - 请求体不是合法 JSON
        raise ServiceError("INVALID_PARAMS", f"请求体不是合法 JSON：{exc}", 400) from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ServiceError("INVALID_PARAMS", "请求体必须是 JSON 对象", 400)
    return payload


def _raise_service_error(exc: LLMError) -> None:
    raise ServiceError(exc.code, exc.message, int(exc.status_code or 400))


@router.get("/api/llm/providers")
def list_providers(request: Request) -> Dict[str, Any]:
    llm = _llm(request)
    try:
        providers = llm.list_providers(masked=True)
        default = llm.get_default()
    except LLMError as exc:
        _raise_service_error(exc)
        raise  # pragma: no cover - _raise_service_error 必然抛出
    return {
        "providers": providers,
        "default_provider_id": str((default or {}).get("provider_id") or ""),
        "kinds": [
            {"kind": "openai_compatible", "label": "OpenAI 兼容"},
            {"kind": "deepseek", "label": "DeepSeek"},
            {"kind": "anthropic", "label": "Anthropic"},
        ],
        "degraded_fallback": True,
    }


@router.post("/api/llm/providers")
async def create_provider(request: Request) -> Dict[str, Any]:
    llm = _llm(request)
    payload = await _json_body(request)
    try:
        provider = llm.create_provider(payload)
    except LLMError as exc:
        _raise_service_error(exc)
        raise  # pragma: no cover
    return {"ok": True, "provider": provider}


@router.patch("/api/llm/providers/{provider_id}")
async def update_provider(provider_id: str, request: Request) -> Dict[str, Any]:
    llm = _llm(request)
    payload = await _json_body(request)
    try:
        provider = llm.update_provider(provider_id, payload)
    except LLMError as exc:
        _raise_service_error(exc)
        raise  # pragma: no cover
    return {"ok": True, "provider": provider}


@router.delete("/api/llm/providers/{provider_id}")
def delete_provider(provider_id: str, request: Request) -> Dict[str, Any]:
    llm = _llm(request)
    try:
        result = llm.delete_provider(provider_id)
    except LLMError as exc:
        _raise_service_error(exc)
        raise  # pragma: no cover
    return result


@router.post("/api/llm/providers/{provider_id}/test")
def test_provider(provider_id: str, request: Request) -> Dict[str, Any]:
    """同步测试连接（≤16 token ping）；失败也返回 200 结构化结果。"""
    llm = _llm(request)
    try:
        result = llm.test_provider(provider_id)
    except LLMError as exc:
        _raise_service_error(exc)
        raise  # pragma: no cover
    return result


@router.post("/api/llm/providers/{provider_id}/default")
def set_default_provider(provider_id: str, request: Request) -> Dict[str, Any]:
    llm = _llm(request)
    try:
        provider = llm.set_default(provider_id)
    except LLMError as exc:
        _raise_service_error(exc)
        raise  # pragma: no cover
    return {"ok": True, "provider": provider}


__all__ = ["router"]

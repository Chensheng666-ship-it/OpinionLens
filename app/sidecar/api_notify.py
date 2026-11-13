# -*- coding: utf-8 -*-
"""通知推送 REST 路由（舆见 OpinionLens）。

约定（契约 §2.4 / §3）：
* 全局 ``/api/*`` Bearer 中间件已鉴权，这里**不再处理 token**；
* 业务错误抛 ``engine_runner.ServiceError(code, message, status)``，由 main.py 统一转
  ``{"error":{"code","message"}}``；测试发送类接口“失败但不算服务错误”，统一 200 +
  ``{"ok": false, "error": ...}``，不抛 500；
* 密钥字段只写不读：响应只回 ``secret_configured`` + ``key_masked``，绝不回明文；
* 服务实例从 ``request.app.state.notify`` 取（main.py 中接线），持久层仍由
  ``request.app.state.service.state``（StateStore）持有。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request

from engine_runner import ServiceError
from notify_service import DEFAULT_EVENT_RULES, EVENT_LABELS, NotifyError

router = APIRouter()


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _notify(request: Request) -> Any:
    notify = getattr(request.app.state, "notify", None)
    if notify is None:
        raise ServiceError("NOTIFY_UNAVAILABLE", "通知服务尚未就绪，请稍后重试", 503)
    return notify


def _call(function: Any, *args: Any, **kwargs: Any) -> Any:
    """把服务层的 NotifyError 转成统一 HTTP 错误体异常。"""
    try:
        return function(*args, **kwargs)
    except NotifyError as exc:
        raise ServiceError(exc.code, exc.message, exc.status_code) from exc


def _event_type_catalog() -> List[Dict[str, Any]]:
    return [
        {
            "type": event_type,
            "label": EVENT_LABELS.get(event_type, event_type),
            "default_severity": severity,
        }
        for event_type, severity in DEFAULT_EVENT_RULES
    ]


def _test_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    """把多通道测试结果整理成单通道同构结构（顶层也带 ok/latency_ms/http_status/error）。"""
    items = result.get("results") if isinstance(result.get("results"), list) else []
    latencies = [int(item.get("latency_ms") or 0) for item in items]
    statuses = [item.get("http_status") for item in items if item.get("http_status") is not None]
    errors = [str(item.get("error") or "") for item in items if item.get("error")]
    error_text = "；".join(errors) or str(result.get("error") or "")
    return {
        "ok": bool(result.get("ok")),
        "sent": int(result.get("sent") or 0),
        "latency_ms": max(latencies) if latencies else 0,
        "http_status": statuses[0] if statuses else None,
        "error": error_text[:500],
        "results": items,
    }


# ---------------------------------------------------------------------------
# 配置摘要
# ---------------------------------------------------------------------------
@router.get("/api/notify/config")
def get_notify_config(request: Request) -> Dict[str, Any]:
    notify = _notify(request)
    return _call(notify.config_summary)


# ---------------------------------------------------------------------------
# 通道 CRUD + 测试发送
# ---------------------------------------------------------------------------
@router.post("/api/notify/channels")
def create_notify_channel(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    notify = _notify(request)
    channel = _call(notify.create_channel, payload)
    return {"ok": True, "channel": channel}


@router.patch("/api/notify/channels/{channel_id}")
def update_notify_channel(
    request: Request, channel_id: str, payload: Dict[str, Any]
) -> Dict[str, Any]:
    notify = _notify(request)
    channel = _call(notify.update_channel, channel_id, payload)
    return {"ok": True, "channel": channel}


@router.delete("/api/notify/channels/{channel_id}")
def delete_notify_channel(request: Request, channel_id: str) -> Dict[str, Any]:
    notify = _notify(request)
    return _call(notify.delete_channel, channel_id)


@router.post("/api/notify/channels/{channel_id}/test")
def test_notify_channel(request: Request, channel_id: str) -> Dict[str, Any]:
    # 同步发送（≤10s）；失败也是 200 + ok=false，不抛 500
    notify = _notify(request)
    return _call(notify.test_channel, channel_id)


@router.post("/api/notify/test")
def test_all_notify_channels(request: Request) -> Dict[str, Any]:
    notify = _notify(request)
    result = _call(notify.test_all_channels)
    return _test_summary(result)


# ---------------------------------------------------------------------------
# 事件订阅规则
# ---------------------------------------------------------------------------
@router.get("/api/notify/rules")
def get_notify_rules(request: Request) -> Dict[str, Any]:
    notify = _notify(request)
    rules = _call(notify.list_rules)
    return {"ok": True, "rules": rules, "event_types": _event_type_catalog()}


@router.put("/api/notify/rules")
def put_notify_rules(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    notify = _notify(request)
    rules = _call(notify.update_rules, payload)
    return {"ok": True, "rules": rules}


# ---------------------------------------------------------------------------
# 发送队列 / 发送记录
# ---------------------------------------------------------------------------
@router.get("/api/notify/outbox")
def get_notify_outbox(
    request: Request, status: Optional[str] = None, limit: int = 100
) -> Dict[str, Any]:
    notify = _notify(request)
    items = _call(notify.list_outbox, status, limit)
    # items / outbox 双键：兼容不同前端包裹约定
    return {"ok": True, "items": items, "outbox": items, "count": len(items)}


@router.post("/api/notify/outbox/{outbox_id}/retry")
def retry_notify_outbox(request: Request, outbox_id: str) -> Dict[str, Any]:
    notify = _notify(request)
    return _call(notify.retry_outbox, outbox_id)


@router.get("/api/notify/logs")
def get_notify_logs(request: Request, limit: int = 100) -> Dict[str, Any]:
    notify = _notify(request)
    items = _call(notify.list_logs, limit)
    return {"ok": True, "items": items, "logs": items, "count": len(items)}


# ---------------------------------------------------------------------------
# 总设置
# ---------------------------------------------------------------------------
@router.put("/api/notify/settings")
def put_notify_settings(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    notify = _notify(request)
    # 返回与 GET /api/notify/config 同构的完整配置（前端可直接刷新页面状态）
    return _call(notify.update_settings, payload)


__all__ = ["router"]

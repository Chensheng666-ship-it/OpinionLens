# -*- coding: utf-8 -*-
"""进程内事件总线（开源版舆见 OpinionLens）。

业务模块（EngineService / ReportManager / AccountManager）在关键节点 ``bus.publish(event)``，
通知服务（notify_service.NotifyService）订阅后负责规则匹配、落 outbox、异步发送。

事件字典结构（所有字段容忍缺失，通知服务自行兜底）::

    {
        "type": "run.finished",            # 事件类型，见 CONTRACT §2.1
        "severity": "info",                # info | warn | error
        "dedupe_key": "run:<id>:finished", # 幂等键（通知去重用，全局唯一）
        "ts": 1762930000,
        "data": {...},                     # 事件明细（已脱敏的最小字段集）
    }

设计要点：
* 同步分发、单个订阅者异常不影响其它订阅者与业务主流程（通知绝不能拖垮采集）；
* 异步发送由 NotifyService 自己的 worker 线程负责，本模块不做队列；
* 事件对象不落日志（可能含本地路径等），只记录 type。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List

log = logging.getLogger("sidecar.events")

Handler = Callable[[Dict[str, Any]], None]


class EventBus:
    """极简发布/订阅总线（线程安全）。"""

    def __init__(self) -> None:
        self._handlers: List[Handler] = []
        self._lock = threading.RLock()

    def subscribe(self, handler: Handler) -> None:
        if not callable(handler):
            raise TypeError("handler 必须可调用")
        with self._lock:
            if handler not in self._handlers:
                self._handlers.append(handler)

    def unsubscribe(self, handler: Handler) -> None:
        with self._lock:
            if handler in self._handlers:
                self._handlers.remove(handler)

    def publish(self, event: Dict[str, Any]) -> None:
        """同步发布事件；返回前所有 handler 已执行完毕（不做重活）。"""
        if not isinstance(event, dict):
            log.warning("忽略非法事件（不是 dict）：%r", type(event).__name__)
            return
        event.setdefault("ts", int(time.time()))
        event.setdefault("severity", "info")
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            try:
                handler(event)
            except Exception:  # noqa: BLE001 - 通知失败不能影响业务
                log.exception("事件处理失败：type=%s", event.get("type"))

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()


bus = EventBus()

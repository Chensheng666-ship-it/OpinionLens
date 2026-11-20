# -*- coding: utf-8 -*-
"""离线自测专用：假模型 API 服务（OpenAI / Anthropic 兼容，不联网）。

启动一个进程内 ``ThreadingHTTPServer``（随机端口），同时实现：

* ``POST /v1/chat/completions``：OpenAI 兼容（``choices[0].message.content``）；
* ``POST /v1/messages``：Anthropic 兼容（``content[0].text`` + ``x-api-key``）。

按系统提示词区分三类调用（不看用户 JSON 里偶然出现的「情感分析」字样）：

1. 连接测试（ping）→ 返回 ``pong``；
2. 情感分析（系统提示词含「情感分析」）→ 解析 user JSON 的 items，按文本关键词给
   positive/neutral/negative，记录每批条数；
3. 报告叙述（系统提示词含「叙述层」）→ 返回一份合法 narrative JSON（字段见
   ``app/report_pipeline/narrative.schema.json`` 的宽松子集），记录收到的数据包。

故障/脏输出开关 ``mode``：
``ok`` / ``fail``（HTTP 500）/ ``auth_fail``（HTTP 401）/ ``timeout``（延迟超过客户端超时）/
``dirty``（JSON 外套 Markdown 围栏与前后文字）/ ``invalid_json`` / ``missing_fields`` / ``empty``。

仅供测试使用，不参与生产路径。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

FAKE_MODEL = "fake-model"
FAKE_API_KEY = "sk-fake-1234"

_DEFAULT_MODES = (
    "ok",
    "fail",
    "auth_fail",
    "timeout",
    "dirty",
    "invalid_json",
    "missing_fields",
    "empty",
    "http_garbage",
)


def _text_of_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        )
    return ""


def _sentiment_of(text: str) -> str:
    negative_tokens = ("差", "负面", "投诉", "慢", "失望", "糟糕", "垃圾")
    positive_tokens = ("好", "正面", "积极", "支持", "满意", "优秀", "点赞")
    if any(token in text for token in negative_tokens):
        return "negative"
    if any(token in text for token in positive_tokens):
        return "positive"
    return "neutral"


def _extract_first_json(text: str) -> Any:
    """从 user 消息中解析 JSON（llm_client 的 user message 就是纯 JSON）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except ValueError:
            return None
    return None


class FakeLLMServer:
    """本地假 OpenAI/Anthropic 服务（线程安全、随机端口、可注入故障模式）。"""

    def __init__(self) -> None:
        self.mode = "ok"
        self.timeout_delay = 1.2
        self.ping_reply = "pong"
        self.narrative_override: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._lock = threading.Lock()
        self._requests: List[Dict[str, Any]] = []
        self.sentiment_batches: List[List[Dict[str, Any]]] = []
        self.narrative_requests: List[Dict[str, Any]] = []
        self.ping_count = 0
        self.auth_headers: List[Dict[str, str]] = []

        outer = self

        class Handler(BaseHTTPRequestHandler):
            # -- 工具 ------------------------------------------------------
            def _json(self, status: int, data: Any) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:  # noqa: BLE001 - 客户端已断开（超时用例）
                    pass

            def _read_json(self) -> Any:
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except (TypeError, ValueError):
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:  # noqa: BLE001
                    return None

            def _path_only(self) -> str:
                return self.path.split("?", 1)[0].rstrip("/")

            def _record(self, path: str, body: Any) -> None:
                with outer._lock:
                    outer._requests.append(
                        {
                            "path": path,
                            "headers": {key.lower(): value for key, value in self.headers.items()},
                            "body": body if isinstance(body, dict) else {"raw": body},
                        }
                    )
                    outer.auth_headers.append(
                        {
                            "authorization": self.headers.get("Authorization") or "",
                            "x-api-key": self.headers.get("x-api-key") or "",
                            "anthropic-version": self.headers.get("anthropic-version") or "",
                        }
                    )

            # -- 回复构造 --------------------------------------------------
            @staticmethod
            def _collect_text(body: Dict[str, Any]) -> str:
                parts: List[str] = []
                messages = body.get("messages")
                if isinstance(messages, list):
                    for item in messages:
                        if isinstance(item, dict):
                            parts.append(_text_of_content(item.get("content")))
                system = body.get("system")
                if system is not None:
                    parts.append(_text_of_content(system))
                return "\n".join(parts)

            @staticmethod
            def _last_user_text(body: Dict[str, Any]) -> str:
                messages = body.get("messages")
                if not isinstance(messages, list):
                    return ""
                for item in reversed(messages):
                    if isinstance(item, dict) and str(item.get("role") or "") == "user":
                        return _text_of_content(item.get("content"))
                return ""

            def _narrative_doc(self, payload: Dict[str, Any]) -> Dict[str, Any]:
                stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
                risk = stats.get("risk") if isinstance(stats.get("risk"), dict) else {}
                negative_events = stats.get("negative_events")
                negative_events = negative_events if isinstance(negative_events, list) else []
                positive_events = stats.get("positive_events")
                positive_events = positive_events if isinstance(positive_events, list) else []
                instruction = str(payload.get("instruction") or "")
                previous = payload.get("previous_narrative")
                headline = "修订版：按意见调整文字表述" if previous else "本期舆情总体平稳，风险可控"
                measures = [
                    {
                        "event_id": str(event.get("event_id") or f"NEG-{index + 1:02d}"),
                        "measure": f"建议持续关注并复核该事件（{str(event.get('platform') or '')}）。",
                        "status": str(event.get("handling_status") or "待评估"),
                    }
                    for index, event in enumerate(negative_events[:5])
                    if isinstance(event, dict)
                ]
                follow_ups = [
                    {
                        "event_id": str(event.get("event_id") or f"POS-{index + 1:02d}"),
                        "follow_up": "建议整理为品牌素材，评估二次传播价值。",
                        "worth_amplifying": True,
                    }
                    for index, event in enumerate(positive_events[:5])
                    if isinstance(event, dict)
                ]
                suggestions = [
                    "维持主阵地平台每日监测频次，重点复盘负面占比最高的平台。",
                    "对历史存量负面建立季度复核机制，明确回应口径与责任人。",
                    "围绕高热度正面内容策划二次传播，沉淀品牌素材库。",
                ]
                if instruction:
                    suggestions.append(f"按修订意见执行：{instruction[:80]}")
                return {
                    "schema_version": "1.0",
                    "generated_by": "fake-llm",
                    "model": FAKE_MODEL,
                    "overview": {
                        "headline": headline,
                        "one_line_summary": (
                            "本期数据由演示数据包构成：整体以中性为主，负面以历史存量为主，"
                            f"风险等级「{str(risk.get('level') or '低')}」，建议保持常规监测节奏。"
                        ),
                        "scope_note": "数据来源：演示数据包；统计口径以报告页脚说明为准。",
                    },
                    "trend": {
                        "insight": "窗口内声量分布平稳，无明显突发峰值，发布节奏以日常讨论为主。"
                    },
                    "sentiment": {
                        "insight": "情感结构以中性为主，正面与负面占比接近，评论区情绪需要持续观察。"
                    },
                    "platform": {
                        "insight": "声量集中在头部平台，建议对负面占比较高的平台提高复核频次。"
                    },
                    "negative": {
                        "intro": "本期识别到的负面内容以历史存量为主，未见新增高热度事件。",
                        "note": "「当前状态」由数据规则自动标注，仅表示处置建议，不代表已处理。",
                        "measures": measures,
                    },
                    "positive": {
                        "intro": "本期正面内容以媒体报道与产品科普为主，传播范围有限。",
                        "follow_ups": follow_ups,
                    },
                    "suggestions": suggestions,
                    "suggestions_note": "以上建议由统计结果驱动生成，请结合业务实际调整执行优先级。",
                    "risk": {
                        "level": str(risk.get("level") or "低"),
                        "assessment": "整体舆情以中性/正面为主，风险可控，建议保持常规监测节奏。",
                    },
                    "disclaimer": "本报告统计数字由程序计算，模型仅负责文字叙述。",
                }

            def _system_text(self, body: Dict[str, Any]) -> str:
                parts: List[str] = []
                system = body.get("system")
                if system is not None:
                    parts.append(_text_of_content(system))
                messages = body.get("messages")
                if isinstance(messages, list):
                    for item in messages:
                        if isinstance(item, dict) and str(item.get("role") or "") == "system":
                            parts.append(_text_of_content(item.get("content")))
                return "\n".join(parts)

            def _build_reply(self, body: Dict[str, Any]) -> str:
                combined = self._collect_text(body)
                system_text = self._system_text(body)
                user_text = self._last_user_text(body)
                parsed = _extract_first_json(user_text)
                # 任务以系统提示词为准。叙述请求的用户 JSON 会带上统计字段
                # 「多平台采集与情感分析」，不能因此被当成情感打标。
                narrative_task = "叙述层" in system_text or "narrative@v1" in system_text
                if not narrative_task and "情感分析" in (system_text or combined):
                    items = []
                    if isinstance(parsed, dict):
                        items = parsed.get("items") if isinstance(parsed.get("items"), list) else []
                    elif isinstance(parsed, list):
                        items = parsed
                    rows = []
                    with outer._lock:
                        outer.sentiment_batches.append(
                            [dict(item) for item in items if isinstance(item, dict)]
                        )
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        text = str(item.get("text") or "")
                        rows.append(
                            {
                                "id": str(item.get("id") or ""),
                                "sentiment": _sentiment_of(text),
                                "confidence": 0.9,
                                "reason": "fake 情感判断",
                            }
                        )
                    return json.dumps({"items": rows}, ensure_ascii=False)
                if "叙述层" in combined or "narrative" in combined:
                    payload = parsed if isinstance(parsed, dict) else {}
                    with outer._lock:
                        outer.narrative_requests.append(dict(payload))
                    override = outer.narrative_override
                    if callable(override):
                        value = override(payload)
                        return value if isinstance(value, str) else json.dumps(
                            value, ensure_ascii=False
                        )
                    return json.dumps(self._narrative_doc(payload), ensure_ascii=False)
                with outer._lock:
                    outer.ping_count += 1
                return str(outer.ping_reply or "pong")

            # -- 故障 / 脏输出 --------------------------------------------
            def _handle_error_mode(self) -> bool:
                mode = outer.mode
                if mode == "fail":
                    self._json(
                        500,
                        {
                            "error": {
                                "message": "fake model failure",
                                "type": "server_error",
                            }
                        },
                    )
                    return True
                if mode == "auth_fail":
                    self._json(
                        401,
                        {"error": {"message": "fake invalid api key", "type": "auth_error"}},
                    )
                    return True
                if mode == "timeout":
                    time.sleep(outer.timeout_delay)
                    return False
                return False

            def _decorate_reply(self, reply: str) -> str:
                mode = outer.mode
                if mode == "dirty":
                    return "以下是模型输出：\n```json\n" + reply + "\n```\n希望对你有帮助。"
                if mode == "invalid_json":
                    return "抱歉，本次无法生成结构化报告内容。"
                if mode == "missing_fields":
                    return json.dumps({"overview": {"headline": "缺少必填字段"}}, ensure_ascii=False)
                if mode == "empty":
                    return ""
                return reply

            # -- POST -------------------------------------------------------
            def do_POST(self) -> None:  # noqa: N802
                path = self._path_only()
                body = self._read_json()
                body = body if isinstance(body, dict) else {}
                self._record(self.path, body)
                if not (
                    path.endswith("/chat/completions")
                    or path.endswith("/v1/messages")
                    or path.endswith("/messages")
                ):
                    self._json(404, {"error": {"message": f"fake 未实现：{path}"}})
                    return
                if self._handle_error_mode():
                    return
                if outer.mode == "http_garbage":
                    # HTTP 200 但响应体不是 JSON（模拟 base_url 指向普通网页/网关 HTML）
                    body = b"<html><body>not a model api</body></html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except Exception:  # noqa: BLE001
                        pass
                    return
                text = self._build_reply(body)
                text = self._decorate_reply(text)
                if path.endswith("/v1/messages") or path.endswith("/messages"):
                    self._json(
                        200,
                        {
                            "id": "msg_fake_0001",
                            "type": "message",
                            "role": "assistant",
                            "model": str(body.get("model") or FAKE_MODEL),
                            "content": [{"type": "text", "text": text}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": len(text)},
                        },
                    )
                    return
                self._json(
                    200,
                    {
                        "id": "chatcmpl-fake-0001",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": str(body.get("model") or FAKE_MODEL),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": len(text),
                            "total_tokens": 10 + len(text),
                        },
                    },
                )

            def log_message(self, *_args: Any) -> None:  # noqa: D401 - 静默
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-llm", daemon=True
        )

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> "FakeLLMServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        try:
            self._server.shutdown()
            self._server.server_close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    # -- 断言辅助 ---------------------------------------------------------
    def provider_data(
        self,
        *,
        kind: str = "openai_compatible",
        name: str = "fake provider",
        model: str = FAKE_MODEL,
        api_key: str = FAKE_API_KEY,
        base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """可直接传给 ``LLMService.create_provider`` 的 provider 数据。"""
        return {
            "name": name,
            "kind": kind,
            "base_url": base_url or self.base_url,
            "model": model,
            "api_key": api_key,
        }

    def records(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._requests]

    def count(self, predicate: Optional[Callable[[Dict[str, Any]], bool]] = None) -> int:
        with self._lock:
            records = [dict(item) for item in self._requests]
        if predicate is None:
            return len(records)
        return sum(1 for item in records if predicate(item))

    def wait_for(
        self,
        predicate: Callable[[Dict[str, Any]], bool],
        timeout: float = 15.0,
        description: str = "fake LLM 请求",
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for item in self.records():
                try:
                    matched = bool(predicate(item))
                except Exception:  # noqa: BLE001 - 断言函数异常视为不匹配
                    matched = False
                if matched:
                    return item
            time.sleep(0.02)
        raise TimeoutError(f"等待{description}超时（{timeout}s）")

    def wait_for_narrative(self, timeout: float = 15.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                payloads = [dict(item) for item in self.narrative_requests]
            if payloads:
                return payloads[-1]
            time.sleep(0.02)
        raise TimeoutError(f"等待 narrative 请求超时（{timeout}s）")

    def sentiment_batch_sizes(self) -> List[int]:
        with self._lock:
            return [len(batch) for batch in self.sentiment_batches]

    def reset(self) -> None:
        with self._lock:
            self.mode = "ok"
            self.narrative_override = None
            self._requests.clear()
            self.sentiment_batches.clear()
            self.narrative_requests.clear()
            self.ping_count = 0
            self.auth_headers.clear()

    def reset_records(self) -> None:
        """只清空请求记录，保留 mode / override。"""
        with self._lock:
            self._requests.clear()
            self.sentiment_batches.clear()
            self.narrative_requests.clear()
            self.ping_count = 0
            self.auth_headers.clear()


__all__ = ["FakeLLMServer", "FAKE_MODEL", "FAKE_API_KEY"]

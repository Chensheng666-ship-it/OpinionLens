# -*- coding: utf-8 -*-
"""
report_sentiment.py — 情感批处理 / 缓存 / neutral 兜底（P1-1 拆分自 report_data.py）

职责：
  1. `sentiment_cache` 表（`<data-dir>/app.db`）的建表 / 读写 / upsert；
  2. 标签归一化：中文/英文 → `positive|neutral|negative`（`normalize_sentiment_label`）；
  3. `analyze_dataset_sentiment()`：文本去重 → 读缓存（同 `text_hash` 命中）→ 唯一文本按
     `batch_size` 分批调用 analyzer → 写缓存 → 写回 dataset.contents/comments；
  4. 兜底：analyzer 未配置 / 抛异常 / 返回缺失 → 对应项 `sentiment_scored=False`（展示占位 neutral），
     **不写缓存**（下次可重试），也不计入情感占比与风险等级分母；原因记进返回值与 warnings（报告继续生成）。

情感回调契约（与 app-sidecar-dev 对齐）：
  analyzer(texts: list[{"id": str, "text": str}]) -> list[{"id": str,
                                                          "sentiment": "positive|neutral|negative|正面|中性|负面",
                                                          "confidence": number, "reason": str}]
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from report_common import (
    _SENTIMENT_ALIASES,
    _is_blank,
    _text,
    _truncate,
)

SENTIMENT_CACHE_TABLE = "sentiment_cache"
# text_hash 覆盖打分方案 + 文本：旧方案（多条文本共用一次模型请求）写下的标签不再命中复用。
SENTIMENT_SCORING_SCHEME = "single-text@v2"
SENTIMENT_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sentiment_cache (
    platform   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    text_hash  TEXT NOT NULL,
    sentiment  TEXT NOT NULL,
    reason     TEXT DEFAULT '',
    model      TEXT DEFAULT '',
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (platform, kind, item_id, text_hash)
);
CREATE INDEX IF NOT EXISTS idx_sentiment_cache_lookup
    ON sentiment_cache(platform, kind, item_id);
"""


def normalize_sentiment_label(value: Any) -> Optional[str]:
    """中文/英文情感标签 → positive|neutral|negative；未知返回 None。"""
    if _is_blank(value):
        return None
    text = str(value).strip().lower()
    text = re.sub(r"[\[\]【】()（）\s]", "", text)
    return _SENTIMENT_ALIASES.get(text)


# ---------------------------------------------------------------------------
# 情感缓存（app.db 独立连接，不依赖 state.py）
# ---------------------------------------------------------------------------
def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def connect_app_db(app_db_path: str, *, create: bool = True) -> sqlite3.Connection:
    """打开 app.db；create=True 时建 sentiment_cache 表（WAL + busy_timeout）。"""
    if create:
        _ensure_parent_dir(app_db_path)
    conn = sqlite3.connect(app_db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SENTIMENT_CACHE_SCHEMA)
            conn.commit()
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def load_sentiment_cache(
    app_db_path: str,
    *,
    platforms: Optional[Sequence[str]] = None,
    kinds: Optional[Sequence[str]] = None,
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """
    读取情感缓存：key = (platform_code, kind, item_id) → {text_hash, sentiment, reason, model, updated_ts}。

    app.db / 表不存在时返回 {}（不抛错，方便未分析环境导出 Excel）。
    """
    if not app_db_path or not os.path.isfile(app_db_path):
        return {}
    try:
        conn = connect_app_db(app_db_path, create=False)
    except sqlite3.Error:
        return {}
    try:
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (SENTIMENT_CACHE_TABLE,)
        ).fetchone():
            return {}
        sql = "SELECT platform, kind, item_id, text_hash, sentiment, reason, model, updated_ts FROM sentiment_cache"
        clauses = []
        params: List[Any] = []
        if platforms:
            clauses.append(f"platform IN ({','.join('?' * len(platforms))})")
            params.extend(list(platforms))
        if kinds:
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(list(kinds))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        result: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for row in conn.execute(sql, params):
            key = (row["platform"], row["kind"], str(row["item_id"]))
            candidate = {
                "text_hash": row["text_hash"],
                "sentiment": row["sentiment"],
                "reason": row["reason"] or "",
                "model": row["model"] or "",
                "updated_ts": int(row["updated_ts"] or 0),
            }
            existing = result.get(key)
            # 同一 item 可能存在多个历史 text_hash 缓存：取 updated_ts 最新的一条
            if existing is None or candidate["updated_ts"] >= int(existing.get("updated_ts") or 0):
                result[key] = candidate
        return result
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def upsert_sentiments(app_db_path: str, entries: Iterable[Dict[str, Any]], *, model: str = "") -> int:
    """
    批量写入情感缓存；entries 字段：platform, kind, item_id, text_hash, sentiment, reason。

    返回写入行数；写入失败抛 sqlite3.Error（调用方可降级为 warning）。
    """
    rows = []
    now = int(time.time())
    for entry in entries:
        sentiment = normalize_sentiment_label(entry.get("sentiment"))
        if not sentiment:
            continue
        rows.append((
            str(entry["platform"]),
            str(entry["kind"]),
            str(entry["item_id"]),
            str(entry["text_hash"]),
            sentiment,
            _truncate(entry.get("reason") or "", 500),
            model or str(entry.get("model") or ""),
            now,
        ))
    if not rows:
        return 0
    conn = connect_app_db(app_db_path, create=True)
    try:
        conn.executemany(
            f"INSERT INTO {SENTIMENT_CACHE_TABLE} "
            "(platform, kind, item_id, text_hash, sentiment, reason, model, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(platform, kind, item_id, text_hash) DO UPDATE SET "
            "sentiment=excluded.sentiment, reason=excluded.reason, "
            "model=excluded.model, updated_ts=excluded.updated_ts",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def upsert_sentiment(app_db_path: str, *, platform: str, kind: str, item_id: str, text_hash: str,
                     sentiment: str, reason: str = "", model: str = "") -> int:
    """写入单条情感缓存（供外部/测试使用）。"""
    return upsert_sentiments(app_db_path, [{
        "platform": platform, "kind": kind, "item_id": item_id, "text_hash": text_hash,
        "sentiment": sentiment, "reason": reason, "model": model,
    }], model=model)


# ---------------------------------------------------------------------------
# 情感分析执行
# ---------------------------------------------------------------------------
def _sentiment_text(item: Dict[str, Any]) -> str:
    if item["kind"] == "content":
        title = item.get("title") or ""
        text = item.get("text") or ""
        combined = f"{title}\n{text}".strip() if title and title not in text else (text or title)
        return _truncate(combined, 1200)
    return _truncate(item.get("text") or "", 800)


def _text_hash(text: str) -> str:
    return hashlib.sha256(f"{SENTIMENT_SCORING_SCHEME}\n{text}".encode("utf-8")).hexdigest()


def _mark_unscored(item: Dict[str, Any]) -> None:
    # sentiment 只是展示/schema 占位；sentiment_scored=False 的条目不进入情感占比与风险分母。
    item["sentiment"] = "neutral"
    item["sentiment_scored"] = False
    item["sentiment_raw"] = ""
    item["_sentiment_reason"] = ""


def analyze_dataset_sentiment(
    dataset: Any,
    sentiment_analyzer: Optional[Callable[[List[Dict[str, Any]]], Any]],
    *,
    app_db_path: str,
    batch_size: int = 50,
    model: str = "",
    warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    对 dataset.contents/comments 批量情感分析并写回 sentiment（positive/neutral/negative）。

    流程：文本去重 → 读缓存（同 text_hash 命中）→ 唯一文本分批调用 analyzer → 写缓存 → 写回 dataset。
    analyzer 为 None / 抛异常 / 返回缺失 → 对应文本 `sentiment_scored=False`（不写缓存，下次可重试；
    compute_stats 不把它们计入情感占比 / 风险等级）。
    返回统计信息（写入 stats.data_quality.sentiment）。
    """
    warnings = warnings if warnings is not None else []
    batch_size = max(1, min(int(batch_size or 50), 200))
    info: Dict[str, Any] = {
        "analyzer_configured": callable(sentiment_analyzer),
        "model": model,
        "batch_size": batch_size,
        "items": 0,
        "cache_hits": 0,
        "analyzer_texts": 0,
        "analyzed_items": 0,
        "cache_written": 0,
        "fallback_neutral_items": 0,
        "fallback_neutral_texts": 0,
        "batches": 0,
        "errors": [],
        "cache_error": "",
    }

    items = list(dataset.contents) + list(dataset.comments)
    info["items"] = len(items)
    if not items:
        return info

    app_db_path = app_db_path or ""
    cache = load_sentiment_cache(
        app_db_path,
        platforms=sorted({item["_platform_code"] for item in items}),
        kinds=["content", "comment"],
    )

    # 1) 计算 text_hash / 收集需要分析的唯一文本
    pending_texts: Dict[str, str] = {}
    item_entries: List[Tuple[Dict[str, Any], str, str]] = []  # (item, text_hash, canonical sentiment)
    for item in items:
        item_id = str(item.get("content_id") if item["kind"] == "content" else item.get("comment_id"))
        text = _sentiment_text(item)
        text_hash = _text_hash(text)
        item["_sentiment_text_hash"] = text_hash
        platform_code = item["_platform_code"]
        cached = cache.get((platform_code, item["kind"], item_id))
        cached_label = normalize_sentiment_label(cached.get("sentiment")) if cached else None
        if cached and cached_label and cached.get("text_hash") == text_hash:
            item["sentiment"] = cached_label
            item["sentiment_scored"] = True
            item["sentiment_raw"] = cached.get("sentiment") or ""
            item["_sentiment_reason"] = cached.get("reason") or ""
            info["cache_hits"] += 1
            info["analyzed_items"] += 1
            continue
        pending_texts[text_hash] = text
        item_entries.append((item, text_hash, ""))

    # 2) 无 analyzer / 无待分析：全部标为未打分（无缓存时）
    if not item_entries:
        return info
    if not callable(sentiment_analyzer):
        for item, text_hash, _ in item_entries:
            _mark_unscored(item)
        info["fallback_neutral_items"] = len(item_entries)
        info["fallback_neutral_texts"] = len(pending_texts)
        warnings.append("sentiment_analyzer 未配置：情感未打分（不计入情感占比与风险等级，未写缓存）")
        return info

    # 3) 分批调用 analyzer（唯一文本）
    texts = list(pending_texts.items())  # [(hash, text)]
    results: Dict[str, Dict[str, Any]] = {}
    info["analyzer_texts"] = len(texts)
    for start in range(0, len(texts), batch_size):
        chunk = texts[start:start + batch_size]
        info["batches"] += 1
        try:
            response = sentiment_analyzer([{"id": text_hash, "text": text} for text_hash, text in chunk])
        except Exception as exc:  # noqa: BLE001 - 分析失败不能阻断报告生成
            message = f"情感分析批次失败（batch {info['batches']}）：{type(exc).__name__}: {exc}"
            info["errors"].append(message)
            warnings.append(message)
            continue
        entries: Any = response
        if isinstance(response, dict):
            entries = response.get("results") or response.get("data") or response.get("items") or []
        if not isinstance(entries, list):
            message = f"情感分析返回格式非法（batch {info['batches']}）：{type(response).__name__}"
            info["errors"].append(message)
            warnings.append(message)
            continue
        chunk_ids = {text_hash for text_hash, _ in chunk}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or "")
            if entry_id in chunk_ids:
                results[entry_id] = entry

    # 4) 写回 dataset + 写缓存
    cache_entries: List[Dict[str, Any]] = []
    for item, text_hash, _ in item_entries:
        entry = results.get(text_hash)
        label = normalize_sentiment_label(entry.get("sentiment")) if entry else None
        if entry is None or label is None:
            _mark_unscored(item)
            info["fallback_neutral_items"] += 1
            continue
        reason = _truncate(entry.get("reason") or "", 500)
        item["sentiment"] = label
        item["sentiment_scored"] = True
        item["sentiment_raw"] = _text(entry.get("sentiment"))
        item["_sentiment_reason"] = reason
        info["analyzed_items"] += 1
        item_id = str(item.get("content_id") if item["kind"] == "content" else item.get("comment_id"))
        cache_entries.append({
            "platform": item["_platform_code"],
            "kind": item["kind"],
            "item_id": item_id,
            "text_hash": text_hash,
            "sentiment": label,
            "reason": reason,
        })
    scored_texts = {
        text_hash for text_hash, entry in results.items()
        if normalize_sentiment_label(entry.get("sentiment"))
    }
    fallback_texts = {text_hash for text_hash, _ in texts} - scored_texts
    info["fallback_neutral_texts"] = len(fallback_texts)
    if fallback_texts:
        warnings.append(
            f"情感分析有 {len(fallback_texts)} 条文本未得到有效结果，未打分（不计入情感占比与风险等级）"
        )
    if info["errors"]:
        warnings.append(f"情感分析失败 {len(info['errors'])} 个批次，失败批次内文本未打分")

    if cache_entries:
        try:
            info["cache_written"] = upsert_sentiments(app_db_path, cache_entries, model=model)
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响报告
            info["cache_error"] = f"{type(exc).__name__}: {exc}"
            warnings.append(f"情感缓存写入失败（报告继续）：{info['cache_error']}")
    return info


__all__ = [
    "SENTIMENT_CACHE_SCHEMA",
    "SENTIMENT_CACHE_TABLE",
    "SENTIMENT_SCORING_SCHEME",
    "_sentiment_text",
    "analyze_dataset_sentiment",
    "connect_app_db",
    "load_sentiment_cache",
    "normalize_sentiment_label",
    "upsert_sentiment",
    "upsert_sentiments",
]

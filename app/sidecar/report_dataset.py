# -*- coding: utf-8 -*-
"""
report_dataset.py — SQLite → `NormalizedDataset`（P1-1 拆分自 report_data.py）

职责（只做「读库 + 归一」，不碰统计/情感/数据包）：
  1. 按**发布时间窗口**（`window_start` 00:00:00 ~ `window_end` 23:59:59，Asia/Shanghai）读取
     7 平台内容/评论，复用 `app/report_pipeline` 的 `NormalizedDataset` 字段约定
     （不复制 POC 代码）；
  2. 关键词召回（`source_keyword` token 交集）+ 文本相关度（title/desc/正文/nickname）过滤，
     `off_brand` 行不进入 dataset；
  3. 去重（内容按平台 ID、评论按评论 ID，保留 `last_modify_ts` 最新的一行）；
  4. 评论只取「窗口内内容的评论」；评论时间异常时回退父内容发布时间并计数；
  5. 产出 `data_quality`（行数/去重/时间解析失败/窗口过滤明细）。

统计与数据包组装见 `report_package.py`，情感批处理见 `report_sentiment.py`。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from report_common import (
    COMMENT_SPECS,
    CONTENT_SPECS,
    DEFAULT_PLATFORMS,
    PLATFORM_LABELS,
    TIMEZONE,
    TZ,
    _file_sha256,
    _fmt_dt,
    _is_blank,
    _normalize_platform,
    _read_rows,
    _table_exists,
    _text,
    _to_epoch_ms,
    _to_int,
    _truncate,
    _unique,
    keyword_tokens,
    load_poc_modules,
    open_media_db,
    parse_publish_time,
    parse_window,
)


# ---------------------------------------------------------------------------
# 行 → 字段（各平台列名差异在这里吸收）
# ---------------------------------------------------------------------------
def _haystack(row: Dict[str, Any], content_spec: Dict[str, Any]) -> str:
    parts = []
    for column in ("title", "desc", "content", "content_text"):
        if column in row and not _is_blank(row[column]):
            parts.append(_text(row[column]))
    for column in content_spec.get("author_cols", []):
        if column in row and not _is_blank(row[column]):
            parts.append(_text(row[column]))
    for column in ("nickname", "user_nickname"):
        if column in row and not _is_blank(row[column]):
            parts.append(_text(row[column]))
    return "\n".join(parts).lower()


def _row_time(row: Dict[str, Any], columns: Sequence[str]) -> Optional[datetime]:
    for column in columns:
        if column in row and not _is_blank(row[column]):
            parsed = parse_publish_time(row[column])
            if parsed is not None:
                return parsed
    return None


def _row_title(row: Dict[str, Any], spec: Dict[str, Any]) -> str:
    for column in spec.get("title_cols", []):
        if column in row and not _is_blank(row[column]):
            return _text(row[column])
    return ""


def _row_text(row: Dict[str, Any], spec: Dict[str, Any]) -> str:
    parts: List[str] = []
    for column in spec.get("text_cols", []):
        if column in row and not _is_blank(row[column]):
            value = _text(row[column])
            if value and value not in parts:
                parts.append(value)
    return "\n".join(parts).strip()


def _row_author(row: Dict[str, Any], spec: Dict[str, Any]) -> str:
    for column in spec.get("author_cols", []):
        if column in row and not _is_blank(row[column]):
            return _text(row[column])
    return ""


def _row_url(row: Dict[str, Any], spec: Dict[str, Any]) -> str:
    column = spec.get("url_col")
    if column and column in row and not _is_blank(row[column]):
        url = _text(row[column])
        if url.startswith(("http://", "https://")):
            return url
    return ""


def _row_interactions(row: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, int]:
    interactions: Dict[str, int] = {}
    for key, column in spec.get("interactions", {}).items():
        if column and column in row:
            value = _to_int(row.get(column))
            if value:
                interactions[key] = value
    return interactions


# ---------------------------------------------------------------------------
# 关键词召回 / 相关度 / 去重
# ---------------------------------------------------------------------------
def _keyword_selected(row: Dict[str, Any], requested_tokens: Sequence[str]) -> bool:
    """内容是否被选中关键词召回（source_keyword token 与请求 token 交集）。

    支持“报告关键词是采集关键词的子集”：请求 ``示例`` 可命中采集关键词
    ``示例科技``；同时保留精确匹配。实际文本相关性仍由 ``_keyword_relevant`` 判断。
    """
    if not requested_tokens:
        return True
    row_tokens = {token.lower() for token in keyword_tokens(row.get("source_keyword"))}
    if not row_tokens:
        return True  # 无 source_keyword 时交由文本相关性判断
    requests = [token.lower() for token in requested_tokens if str(token).strip()]
    for request in requests:
        for row_token in row_tokens:
            if request in row_token or row_token in request:
                return True
    return False


def _keyword_relevant(row: Dict[str, Any], spec: Dict[str, Any], requested_tokens: Sequence[str],
                      row_tokens: Sequence[str]) -> bool:
    """文本相关度：请求关键词 token（无请求时用本行 source_keyword token）命中 title/desc/正文/nickname。"""
    haystack = _haystack(row, spec)
    if not haystack:
        return False
    tokens = requested_tokens or row_tokens
    return any(token.lower() in haystack for token in tokens)


def _dedupe_latest(rows: List[Dict[str, Any]], key_columns: Sequence[str]) -> Tuple[List[Dict[str, Any]], int]:
    """按 key 去重，保留 last_modify_ts（回退 add_ts）最新的一行。"""
    best: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    removed = 0
    for row in rows:
        key = tuple(_text(row.get(column)) for column in key_columns)
        if any(not part for part in key):
            continue
        if key in best:
            current_ms = _to_epoch_ms(best[key].get("last_modify_ts")) or _to_epoch_ms(best[key].get("add_ts")) or 0
            new_ms = _to_epoch_ms(row.get("last_modify_ts")) or _to_epoch_ms(row.get("add_ts")) or 0
            if new_ms >= current_ms:
                best[key] = row
            removed += 1
        else:
            best[key] = row
    return list(best.values()), removed


# ---------------------------------------------------------------------------
# 读取窗口数据 → POC NormalizedDataset
# ---------------------------------------------------------------------------
def load_report_dataset(
    db_path: str,
    *,
    window_start: str,
    window_end: str,
    keywords: Sequence[str] = (),
    platforms: Sequence[str] = DEFAULT_PLATFORMS,
) -> Tuple[Any, Dict[str, Any]]:
    """
    读取发布时间窗口内的数据并构造 POC `NormalizedDataset`。

    返回 `(dataset, quality)`；dataset 的 contents/comments 已经是“窗口 + 关键词相关 + 去重”后的数据，
    sentiment 暂时全部为 neutral（由后续情感分析写回）。
    """
    poc = load_poc_modules()
    NormalizedDataset = poc["NormalizedDataset"]
    window_start_dt, window_end_dt = parse_window(window_start, window_end)

    requested_tokens = _unique(
        token for keyword in keywords for token in keyword_tokens(keyword)
    )
    platform_codes = [code for code in (_normalize_platform(p) for p in platforms or DEFAULT_PLATFORMS) if code]
    platform_codes = _unique(platform_codes)

    dataset = NormalizedDataset()
    contents: List[Dict[str, Any]] = []
    comments: List[Dict[str, Any]] = []
    warnings: List[str] = []
    table_row_counts: Dict[str, int] = {}
    missing_time = 0
    missing_comment_time = 0
    off_brand_filtered = 0
    outside_window = 0
    keyword_filtered = 0
    duplicate_contents = 0
    duplicate_comments = 0
    comments_unlinked = 0
    missing_url = 0
    content_id_set: Dict[str, set] = {}
    content_time_by_platform: Dict[str, Dict[str, datetime]] = {}

    conn = open_media_db(db_path)
    try:
        # ---------------- 内容 ----------------
        for code in platform_codes:
            spec = CONTENT_SPECS[code]
            table = spec["table"]
            if not _table_exists(conn, table):
                warnings.append(f"内容表缺失：{table}（平台 {code} 跳过）")
                continue
            rows = _read_rows(conn, table)
            table_row_counts[table] = len(rows)
            deduped, removed = _dedupe_latest(rows, [spec["id_col"]])
            duplicate_contents += removed
            kept_ids: set = set()
            time_map: Dict[str, datetime] = {}
            for row in deduped:
                content_id = _text(row.get(spec["id_col"]))
                if not content_id:
                    continue
                published_at = _row_time(row, spec["time_cols"])
                if published_at is None:
                    missing_time += 1
                    continue
                if not (window_start_dt <= published_at <= window_end_dt):
                    outside_window += 1
                    continue
                if not _keyword_selected(row, requested_tokens):
                    keyword_filtered += 1
                    continue
                if not _keyword_relevant(row, spec, requested_tokens,
                                         keyword_tokens(row.get("source_keyword"))):
                    off_brand_filtered += 1
                    continue
                title = _row_title(row, spec)
                text = _row_text(row, spec)
                url = _row_url(row, spec)
                if not url:
                    missing_url += 1
                item = {
                    "kind": "content",
                    "platform": PLATFORM_LABELS[code],
                    "content_id": content_id,
                    "raw_id": content_id,
                    "url": url,
                    "published_at": _fmt_dt(published_at),
                    "published_date": published_at.strftime("%Y-%m-%d"),
                    "title": title,
                    "text": text,
                    "summary": _truncate(text or title, 120),
                    "author": _row_author(row, spec),
                    "sentiment": "neutral",
                    "sentiment_raw": "",
                    "interactions": _row_interactions(row, spec),
                    "id_precision_fixed": False,
                    "sheet": table,
                    "row_index": 0,
                    "_platform_code": code,
                    "_source_keyword": _text(row.get("source_keyword")),
                }
                contents.append(item)
                kept_ids.add(content_id)
                time_map[content_id] = published_at
            content_id_set[code] = kept_ids
            content_time_by_platform[code] = time_map

        # ---------------- 评论（父内容在窗口内即可） ----------------
        for code in platform_codes:
            spec = COMMENT_SPECS[code]
            table = spec["table"]
            if not _table_exists(conn, table):
                warnings.append(f"评论表缺失：{table}（平台 {code} 跳过）")
                continue
            parent_ids = content_id_set.get(code, set())
            if not parent_ids:
                continue
            rows = _read_rows(conn, table)
            table_row_counts[table] = len(rows)
            deduped, removed = _dedupe_latest(rows, [spec["id_col"]])
            duplicate_comments += removed
            parent_times = content_time_by_platform.get(code, {})
            for row in deduped:
                comment_id = _text(row.get(spec["id_col"]))
                parent_id = _text(row.get(spec["parent_col"]))
                if not comment_id or not parent_id or parent_id not in parent_ids:
                    comments_unlinked += 1
                    continue
                published_at = _row_time(row, spec["time_cols"])
                if published_at is None:
                    # 评论时间异常：回退父内容发布时间（保证统计可用），并计数
                    published_at = parent_times.get(parent_id)
                    if published_at is None:
                        missing_comment_time += 1
                        continue
                    missing_comment_time += 1
                text = _text(row.get(spec["text_col"]))
                like_col = spec.get("like_col")
                likes = _to_int(row.get(like_col)) if like_col else 0
                comments.append(
                    {
                        "kind": "comment",
                        "platform": PLATFORM_LABELS[code],
                        "comment_id": comment_id,
                        "parent_raw_id": parent_id,
                        "content_id": parent_id,
                        "published_at": _fmt_dt(published_at),
                        "published_date": published_at.strftime("%Y-%m-%d"),
                        "text": text,
                        "summary": _truncate(text, 80),
                        "likes": likes,
                        "sentiment": "neutral",
                        "sentiment_raw": "",
                        "id_precision_risk": False,
                        "sheet": table,
                        "row_index": 0,
                        "_platform_code": code,
                        "_source_keyword": "",
                    }
                )
    finally:
        conn.close()

    # 稳定排序（便于测试/复现）：按发布时间倒序、平台、ID
    contents.sort(key=lambda item: (item["published_at"], item["platform"], item["content_id"]), reverse=True)
    comments.sort(key=lambda item: (item["published_at"], item["platform"], item["comment_id"]), reverse=True)
    dataset.contents = contents
    dataset.comments = comments
    dataset.warnings = warnings
    dataset.source = {
        "excel_path": os.path.abspath(db_path),
        "excel_sha256": _file_sha256(db_path),
        "excel_size_bytes": os.path.getsize(db_path) if os.path.isfile(db_path) else 0,
        "sheets": sorted(table_row_counts),
        "read_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "db_path": os.path.abspath(db_path),
        "window": {"start": window_start, "end": window_end, "timezone": TIMEZONE},
    }
    matched = len(contents)
    content_tables = {spec["table"] for spec in CONTENT_SPECS.values()}
    comment_tables = {spec["table"] for spec in COMMENT_SPECS.values()}
    dataset.data_quality = {
        "sheet_row_counts": table_row_counts,
        "content_rows": sum(count for table, count in table_row_counts.items() if table in content_tables),
        "comment_rows": sum(count for table, count in table_row_counts.items() if table in comment_tables),
        "content_items": len(contents),
        "comment_items": len(comments),
        "duplicate_contents_removed": duplicate_contents,
        "duplicate_comments_removed": duplicate_comments,
        "content_id_precision_fixed": 0,
        "comment_ids_numeric_risk": 0,
        "time_parse_failures": missing_time + missing_comment_time,
        "time_parse_failure_examples": [],
        "comments_unlinked": comments_unlinked,
        "missing_url": missing_url,
        "relevance": {
            "brand_matched_content": matched,
            "unmatched_content": 0,
            "brand_match_rate": 1.0 if matched else 0.0,
            "note": "报告窗口内仅保留关键词相关（source_keyword + 文本命中）内容",
        },
        "window_filter": {
            "mode": "publish_time",
            "start": window_start,
            "end": window_end,
            "timezone": TIMEZONE,
            "missing_time": missing_time,
            "invalid_or_missing_time": missing_time,
            "outside_window": outside_window,
            "keyword_filtered": keyword_filtered,
            "off_brand_filtered": off_brand_filtered,
            "missing_comment_time_fallback": missing_comment_time,
            "comments_unlinked": comments_unlinked,
        },
    }
    return dataset, dataset.data_quality


__all__ = [
    "_dedupe_latest",
    "_haystack",
    "_keyword_relevant",
    "_keyword_selected",
    "_row_author",
    "_row_interactions",
    "_row_text",
    "_row_time",
    "_row_title",
    "_row_url",
    "load_report_dataset",
]

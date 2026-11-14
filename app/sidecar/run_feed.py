# -*- coding: utf-8 -*-
"""run_feed.py — 「采集动态」内容流数据层（sidecar ``GET /api/runs/{run_id}/feed``）

职责：从 MediaCrawler SQLite 读取**本次采集任务（run）新增/更新的内容与评论**，
组装为桌面端「采集动态」直接渲染的内容流。本模块是**纯函数模块**，不启动服务、
不写库，调用方（engine_runner.EngineService.get_run_feed）只负责取 run 元数据。

契约见 ``app/CONTRACT.md`` §2.2；过滤口径与 ``exporter._collect`` 完全一致：

* 内容表：``last_modify_ts >= run.started_ts``（毫秒归一；缺失/0 无法解析时回退 ``add_ts``），
  且 ``source_keyword ∈ run.keywords``（表无该列时不过滤，兼容旧库）；
* 评论表：与内容同一时间口径，且父内容 ID 属于**本 run 内容集合**；
* ``started_ts`` 是秒级时间戳（与 exporter 的约定一致），本模块内部统一归一为毫秒比较。

容错（桌面端轮询不能因为脏数据 500）：

* 表不存在 / 列不存在 → 该平台直接跳过（不报错）；
* 时间、计数列可能是秒/毫秒/字符串/空/``-1`` 等脏值 → 逐行容错，无法解析按缺失处理；
* MediaCrawler 的计数列是 TEXT（如 ``"35"``）→ 统一转 int；不可用时 ``None``；
* 文本清洗控制字符、折叠空白、按契约截断（正文 ≤200 字、评论 ≤120 字）；
* DB 文件不存在 / 不是 sqlite / 被写锁短暂占用 → 返回空内容流（items=[]，counts=0），不抛错；
* 返回值只含 str/int/bool/None/list/dict，可直接 JSON 序列化（无 NaN）。

对外 API::

    build_run_feed(db_path, *, run_id="", status="", keywords=(), started_ts=None,
                   limit=30, comments=3, since_seq=None) -> dict

增量（M5a）：``since_seq`` 为上次响应的 ``next_seq``。

* 不传 ``since_seq`` → 首屏语义（``items`` 按采集时间倒序，``counts`` 全量）；
* 传 ``since_seq`` → 只返回 ``seq > since_seq`` 的新条目（按 ``seq`` 升序）＋ ``next_seq``；
  ``counts`` / ``comment_total`` 仍是全量；``limit`` 同样生效，超限时 ``truncated=True``
  且 ``next_seq`` 只推进到已返回的最后一条；``since_seq`` 大于当前最大 seq 时返回空数组
  且 ``next_seq`` 原样回传（不回退）。
* 每条 item 带**稳定递增**的 ``seq``（按采集时间升序 / 平台顺序 / ID 分配）。

返回结构（字段名不可改；``next_seq`` 与 item 的 ``seq`` 是 M5a 新增的加法字段）::

    {"run_id","status","keywords",
     "counts": {"content": int, "comments": int,
                "by_platform": {"xhs": {"content": int, "comments": int}, ...}},
     "items": [{"platform","platform_label","kind":"content","id","seq","title","text",
                "author","url","published_at","collected_at",
                "metrics": {"likes","comments","collects","shares","plays"},
                "comment_total": int,
                "comments": [{"id","author","text","likes","published_at"}]}],
     "truncated": bool,
     "next_seq": int}
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote

from report_data import (
    COMMENT_SPECS,
    CONTENT_SPECS,
    DEFAULT_PLATFORMS,
    PLATFORM_LABELS,
    TZ,
    _is_blank,
    _text,
    _to_int,
    parse_publish_time,
)

DEFAULT_LIMIT = 30
MAX_LIMIT = 100
DEFAULT_COMMENTS = 3
MAX_COMMENTS = 10
TITLE_LIMIT = 200
TEXT_LIMIT = 200
COMMENT_TEXT_LIMIT = 120
AUTHOR_LIMIT = 100
TIME_FORMAT = "%Y-%m-%d %H:%M"
METRIC_KEYS: Tuple[str, ...] = ("likes", "comments", "collects", "shares", "plays")

# 评论表没有 CONTENT_SPECS 那样统一的 author_cols，按常见列名回退
_COMMENT_AUTHOR_COLUMNS: Tuple[str, ...] = ("nickname", "user_nickname")
_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；\n]+")
_WHITESPACE_RE = re.compile(r"\s+")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_PLATFORM_ORDER: Dict[str, int] = {code: index for index, code in enumerate(DEFAULT_PLATFORMS)}

_EXCEL_SERIAL_MIN = 20000   # Excel 日期序列号下界（1954 年后）
_EXCEL_SERIAL_MAX = 80000   # Excel 日期序列号上界（2119 年前）
_SEC_MIN = 1_000_000_000    # 合理秒级时间戳下界

# SQL 预过滤：内容/评论表在 Python 精确判断前先粗筛掉“肯定不属于本 run”的行。
# 表达式保证是 Python 侧 ``_row_in_run`` 的**超集**（只多不少），兼容秒/毫秒/微秒/
# Excel 序列号/纯数字文本/日期字符串等混合格式；粗筛命中后再逐行精判。
_MS_GE_THRESHOLD = 100_000_000_000  # >=1e11 视为毫秒（秒级 ~1.8e9，毫秒级 ~1.8e12）
_PARENT_CHUNK = 400                 # 评论表父内容 IN (...) 分块大小（避开 SQLite 变量上限）


# ---------------------------------------------------------------------------
# 小工具：clamp / 关键词 / 文本 / 时间
# ---------------------------------------------------------------------------
def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    """任意值 → [low, high] 整数；无法转换时用 default（default 也 clamp）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        try:
            number = int(float(str(value).strip()))
        except (TypeError, ValueError):
            number = int(default)
    return max(low, min(number, high))


def _normalize_keywords(keywords: Any) -> List[str]:
    """run.keywords → 去空白、去重、拆分中英文逗号/顿号/分号（与 exporter 同口径）。"""
    if keywords is None:
        return []
    if isinstance(keywords, str):
        raw: Iterable[Any] = [keywords]
    else:
        try:
            raw = list(keywords)
        except TypeError:
            raw = [keywords]
    result: List[str] = []
    for item in raw:
        if item is None:
            continue
        for part in _KEYWORD_SPLIT_RE.split(str(item)):
            text = part.strip()
            if text and text not in result:
                result.append(text)
    return result


def _clean(value: Any, limit: int) -> str:
    """清洗文本：去非法控制字符 / 代理字符，折叠空白，超长截断加省略号。"""
    if _is_blank(value):
        return ""
    text = _SURROGATE_RE.sub("", _text(value))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if limit > 0 and len(text) > limit:
        return text[: max(limit - 1, 0)].rstrip() + "…"
    return text


def _format_datetime(value: Optional[datetime]) -> str:
    return value.strftime(TIME_FORMAT) if value is not None else ""


def _numeric_to_epoch_ms(number: Any) -> Optional[int]:
    """数值 → 毫秒时间戳（自动识别 Excel 序列号 / 微秒 / 毫秒 / 秒；非法返回 None）。"""
    try:
        num = float(number)
    except (TypeError, ValueError):
        return None
    if num != num or num <= 0:  # NaN / 非正数
        return None
    if _EXCEL_SERIAL_MIN <= num <= _EXCEL_SERIAL_MAX:
        try:
            serial = datetime(1899, 12, 30) + timedelta(days=num)
        except (OverflowError, ValueError):
            return None
        return int(round(serial.replace(tzinfo=TZ).timestamp() * 1000))
    if num >= 1e15:            # 微秒 → 秒
        num /= 1_000_000.0
    elif num >= 1e12:          # 毫秒 → 秒
        num /= 1_000.0
    if num < _SEC_MIN:         # 非合理时间戳
        return None
    return int(round(num * 1000.0))


def _to_epoch_ms(value: Any) -> Optional[int]:
    """任意时间表示 → 毫秒时间戳；无法识别 / 空 / 0 / 负数返回 None（口径同 exporter）。"""
    if value is None or value == "" or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=TZ)
        return int(round(aware.timestamp() * 1000))
    if isinstance(value, date):
        return int(round(datetime(value.year, value.month, value.day, tzinfo=TZ).timestamp() * 1000))
    if isinstance(value, (int, float)):
        return _numeric_to_epoch_ms(value)
    parsed = parse_publish_time(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TZ).replace(tzinfo=None)
    return int(round(parsed.replace(tzinfo=TZ).timestamp() * 1000))


def _format_epoch_ms(ms: Optional[int]) -> str:
    if ms is None:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000.0, TZ).strftime(TIME_FORMAT)
    except (OverflowError, OSError, ValueError):
        return ""


def _row_time(row: Dict[str, Any], columns: Sequence[str]) -> Optional[datetime]:
    for column in columns or ():
        if column in row and not _is_blank(row.get(column)):
            parsed = parse_publish_time(row.get(column))
            if parsed is not None:
                return parsed
    return None


# ---------------------------------------------------------------------------
# 行 → 展示字段（按 CONTENT_SPECS / COMMENT_SPECS 映射，缺列自动跳过）
# ---------------------------------------------------------------------------
def _row_title(row: Dict[str, Any], spec: Dict[str, Any]) -> str:
    for column in spec.get("title_cols", ()):
        if column in row and not _is_blank(row.get(column)):
            return _clean(row.get(column), TITLE_LIMIT)
    return ""


def _row_text(row: Dict[str, Any], spec: Dict[str, Any]) -> str:
    """正文摘要：取 text_cols 中除标题列外的正文列（避免标题在 title/text 重复）。"""
    title_cols = set(spec.get("title_cols", ()))
    parts: List[str] = []
    for column in spec.get("text_cols", ()):
        if column in title_cols:
            continue
        if column in row and not _is_blank(row.get(column)):
            value = _clean(row.get(column), TEXT_LIMIT)
            if value and value not in parts:
                parts.append(value)
    return _clean(" ".join(parts), TEXT_LIMIT)


def _row_author(row: Dict[str, Any], columns: Sequence[str]) -> str:
    for column in columns or ():
        if column in row and not _is_blank(row.get(column)):
            return _clean(row.get(column), AUTHOR_LIMIT)
    return ""


def _row_url(row: Dict[str, Any], spec: Dict[str, Any]) -> str:
    column = spec.get("url_col")
    if column and column in row and not _is_blank(row.get(column)):
        url = _text(row.get(column)).strip()
        if url.startswith(("http://", "https://")):
            return url[:1000]
    return ""


def _row_metrics(row: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """契约 metrics：5 个键固定存在；DB 无对应计数列时该键为 null。"""
    interactions = spec.get("interactions") or {}
    metrics: Dict[str, Optional[int]] = {}
    for key in METRIC_KEYS:
        column = interactions.get(key)
        if column and column in row:
            metrics[key] = _to_int(row.get(column))
        else:
            metrics[key] = None
    return metrics


def _comment_author(row: Dict[str, Any]) -> str:
    return _row_author(row, _COMMENT_AUTHOR_COLUMNS)


def _row_updated_in_run(row: Dict[str, Any], started_ms: Optional[int]) -> bool:
    """行是否属于本 run 新增/更新（与 exporter._collect 的 _row_updated_in_run 等价）。"""
    if started_ms is None:
        return True
    if "last_modify_ts" in row:
        modified_ms = _to_epoch_ms(row.get("last_modify_ts"))
        if modified_ms is not None:
            return modified_ms >= started_ms
    if "add_ts" in row:
        added_ms = _to_epoch_ms(row.get("add_ts"))
        if added_ms is None:
            return False
        return added_ms >= started_ms
    return True


def _row_sort_ms(row: Dict[str, Any]) -> int:
    """行“采集时间”毫秒（排序用）：last_modify_ts 优先，缺失/0 回退 add_ts。"""
    for column in ("last_modify_ts", "add_ts"):
        if column in row:
            value = _to_epoch_ms(row.get(column))
            if value is not None:
                return value
    return 0


# ---------------------------------------------------------------------------
# SQLite 只读读取 + SQL 粗筛
# ---------------------------------------------------------------------------
def _open_readonly(db_path: str) -> sqlite3.Connection:
    """只读打开 MediaCrawler sqlite（mode=ro，不触发写/建库）。"""
    absolute = Path(str(db_path)).expanduser().resolve().as_posix()
    conn = sqlite3.connect(
        f"file:{quote(absolute, safe='/:')}?mode=ro", uri=True, timeout=3.0
    )
    conn.row_factory = sqlite3.Row
    return conn


def _quote_identifier(name: Any) -> str:
    """SQLite 标识符加双引号（列名来自 PRAGMA/spec，仍做转义防意外）。"""
    return '"' + str(name).replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    """表列名；表不存在 / 打不开时返回 []（调用方据此跳过该平台）。"""
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return []
    return [str(row["name"]) for row in rows]


def _fetch_rows(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any]
) -> List[Dict[str, Any]]:
    """执行只读查询并返回 dict 行；SQL/锁错误按“无数据”容错。"""
    try:
        return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


def _select_columns(spec: Dict[str, Any], existing: Sequence[str], kind: str) -> List[str]:
    """按 spec 组装 SELECT 列（只选需要的列；缺列自动跳过）。"""
    candidates: List[Any] = []
    if kind == "content":
        candidates = (
            [spec.get("id_col")]
            + list(spec.get("time_cols") or ())
            + list(spec.get("title_cols") or ())
            + list(spec.get("text_cols") or ())
            + list(spec.get("author_cols") or ())
            + [spec.get("url_col")]
            + list((spec.get("interactions") or {}).values())
            + ["last_modify_ts", "add_ts", "source_keyword"]
        )
    else:
        candidates = (
            [spec.get("id_col"), spec.get("parent_col")]
            + list(spec.get("time_cols") or ())
            + [spec.get("text_col"), spec.get("like_col")]
            + list(_COMMENT_AUTHOR_COLUMNS)
            + ["last_modify_ts", "add_ts"]
        )
    existing_set = set(existing)
    selected: List[str] = []
    for column in candidates:
        text = str(column) if column else ""
        if text and text in existing_set and text not in selected:
            selected.append(text)
    return selected


def _time_prefilter(columns: Sequence[str], started_ms: Optional[int]) -> Tuple[str, List[Any]]:
    """SQL 侧时间粗筛（Python 精判的超集）；返回 (where 片段, 参数)。"""
    if started_ms is None:
        return "", []
    has_modified = "last_modify_ts" in columns
    has_added = "add_ts" in columns
    if not has_modified and not has_added:
        return "", []
    if has_modified and has_added:
        value = "COALESCE(NULLIF(last_modify_ts, 0), NULLIF(add_ts, 0))"
    elif has_modified:
        value = "NULLIF(last_modify_ts, 0)"
    else:
        value = "NULLIF(add_ts, 0)"
    where = (
        f"({value} IS NULL"
        f" OR typeof({value}) NOT IN ('integer', 'real')"
        f" OR (CASE WHEN ({value}) >= {_MS_GE_THRESHOLD}"
        f" THEN ({value}) ELSE ({value}) * 1000 END) >= ?"
        f" OR ({value} BETWEEN {_EXCEL_SERIAL_MIN} AND {_EXCEL_SERIAL_MAX}))"
    )
    return where, [int(started_ms)]


def _where_sql(parts: Sequence[Any], params: Sequence[Any]) -> Tuple[str, List[Any]]:
    clauses = [str(part) for part in parts if part]
    if not clauses:
        return "", list(params)
    return " WHERE " + " AND ".join(clauses), list(params)


def _parent_ids_clause(parent_col: str, parent_ids: Sequence[str]) -> Tuple[str, List[Any]]:
    """评论表父内容过滤：``parent_col IN (chunk...)``，按块拼接避免变量数上限。"""
    ids = list(parent_ids)
    parts: List[str] = []
    params: List[Any] = []
    for start in range(0, len(ids), _PARENT_CHUNK):
        chunk = ids[start:start + _PARENT_CHUNK]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        parts.append(f'"{parent_col}" IN ({placeholders})')
        params.extend(chunk)
    if not parts:
        return "", []
    return "(" + " OR ".join(parts) + ")", params


# ---------------------------------------------------------------------------
# 内容收集
# ---------------------------------------------------------------------------
def _collect_contents(
    conn: sqlite3.Connection, keywords: Sequence[str], started_ms: Optional[int]
) -> Tuple[List[Dict[str, Any]], Dict[str, Set[str]]]:
    """读取本 run 的内容行 → (内容记录列表, 平台→内容 ID 集合)。"""
    records: List[Dict[str, Any]] = []
    ids_by_platform: Dict[str, Set[str]] = {}
    keyword_list = list(keywords)

    for code in DEFAULT_PLATFORMS:
        spec = CONTENT_SPECS.get(code)
        if not spec:
            continue
        table = str(spec.get("table") or "")
        id_col = spec.get("id_col")
        if not table or not id_col:
            continue
        columns = _table_columns(conn, table)
        if not columns or id_col not in columns:
            continue  # 缺表 / 缺主 ID 列：该平台无法展示，跳过

        selected = _select_columns(spec, columns, "content")
        time_where, time_params = _time_prefilter(columns, started_ms)
        parts: List[Any] = [time_where]
        params: List[Any] = list(time_params)
        # source_keyword ∈ run.keywords（与 exporter 同口径：整值匹配、去空白）
        if keyword_list and "source_keyword" in columns:
            parts.append(f"TRIM(source_keyword) IN ({','.join('?' for _ in keyword_list)})")
            params.extend(keyword_list)
        where_sql, params = _where_sql(parts, params)
        select_sql = ", ".join(_quote_identifier(column) for column in selected)
        sql = f'SELECT {select_sql} FROM "{table}"{where_sql}'
        rows = _fetch_rows(conn, sql, params)

        best: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            content_id = _text(row.get(id_col))
            if not content_id:
                continue
            if not _row_updated_in_run(row, started_ms):
                continue
            sort_ms = _row_sort_ms(row)
            record = {
                "platform": code,
                "id": content_id,
                "title": _row_title(row, spec),
                "text": _row_text(row, spec),
                "author": _row_author(row, spec.get("author_cols") or ()),
                "url": _row_url(row, spec),
                "published_at": _format_datetime(_row_time(row, spec.get("time_cols") or ())),
                "collected_at": _format_epoch_ms(sort_ms),
                "metrics": _row_metrics(row, spec),
                "_sort_ms": sort_ms,
            }
            # 真实库主键唯一，正常无重复；万一重复保留采集时间最新的一行，避免 UI 重复卡片
            existing = best.get(content_id)
            if existing is None or record["_sort_ms"] >= existing["_sort_ms"]:
                best[content_id] = record

        if best:
            ids_by_platform[code] = set(best)
            records.extend(best.values())

    return records, ids_by_platform


# ---------------------------------------------------------------------------
# 评论收集
# ---------------------------------------------------------------------------
def _collect_comments(
    conn: sqlite3.Connection,
    content_ids: Dict[str, Set[str]],
    visible_ids: Dict[str, Set[str]],
    started_ms: Optional[int],
) -> Tuple[Dict[Tuple[str, str], List[Dict[str, Any]]], Dict[str, Dict[str, int]]]:
    """读取本 run 评论 → (可见内容的评论明细, 平台→父内容→评论总数)。

    ``comment_total`` 统计本 run 全部父内容的评论数；评论明细只保留可见（items 内）
    父内容，避免把整页之外的大量子行塞进内存/响应。
    """
    records: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    totals: Dict[str, Dict[str, int]] = {}

    for code in DEFAULT_PLATFORMS:
        all_parents = content_ids.get(code) or set()
        if not all_parents:
            continue
        spec = COMMENT_SPECS.get(code)
        if not spec:
            continue
        table = str(spec.get("table") or "")
        id_col = spec.get("id_col")
        parent_col = spec.get("parent_col")
        if not table or not id_col or not parent_col:
            continue
        columns = _table_columns(conn, table)
        if not columns or id_col not in columns or parent_col not in columns:
            continue  # 缺表 / 缺关键列：该平台无评论明细

        selected = _select_columns(spec, columns, "comment")
        time_where, time_params = _time_prefilter(columns, started_ms)
        parent_where, parent_params = _parent_ids_clause(parent_col, sorted(all_parents))
        if not parent_where:
            continue
        parts: List[Any] = [time_where, parent_where]
        params: List[Any] = list(time_params) + list(parent_params)
        where_sql, params = _where_sql(parts, params)
        select_sql = ", ".join(_quote_identifier(column) for column in selected)
        sql = f'SELECT {select_sql} FROM "{table}"{where_sql}'
        rows = _fetch_rows(conn, sql, params)

        shown_parents = visible_ids.get(code) or set()
        platform_totals: Dict[str, int] = {}
        platform_records: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for row in rows:
            parent_id = _text(row.get(parent_col))
            if not parent_id or parent_id not in all_parents:
                continue
            if not _row_updated_in_run(row, started_ms):
                continue
            platform_totals[parent_id] = platform_totals.get(parent_id, 0) + 1
            if parent_id not in shown_parents:
                continue
            like_col = spec.get("like_col")
            sort_ms = _row_sort_ms(row)
            platform_records.setdefault((code, parent_id), []).append(
                {
                    "id": _text(row.get(id_col)),
                    "author": _comment_author(row),
                    "text": _clean(row.get(spec.get("text_col")), COMMENT_TEXT_LIMIT),
                    "likes": _to_int(row.get(like_col)) if like_col and like_col in row else 0,
                    "published_at": _format_datetime(_row_time(row, spec.get("time_cols") or ())),
                    "_sort_ms": sort_ms,
                }
            )
        if platform_totals:
            totals[code] = platform_totals
        records.update(platform_records)

    return records, totals


# ---------------------------------------------------------------------------
# 组装响应
# ---------------------------------------------------------------------------
def _empty_feed(run_id: str, status: str, keywords: Sequence[str]) -> Dict[str, Any]:
    return {
        "run_id": str(run_id or ""),
        "status": str(status or ""),
        "keywords": list(keywords),
        "counts": {"content": 0, "comments": 0, "by_platform": {}},
        "items": [],
        "truncated": False,
        "next_seq": 0,
    }


def _seq_sort_key(record: Dict[str, Any]) -> Tuple[int, int, str]:
    """seq 分配口径：采集时间**升序** → 平台顺序 → ID（稳定，新行只会拿到更大的 seq）。"""
    return (
        int(record.get("_sort_ms") or 0),
        _PLATFORM_ORDER.get(str(record.get("platform") or ""), 99),
        str(record.get("id") or ""),
    )


def _feed_sort_key(record: Dict[str, Any]) -> Tuple[int, int, str]:
    """首屏排序口径（与改造前逐字节一致）：采集时间**倒序** → 平台顺序 → ID。"""
    return (
        -int(record.get("_sort_ms") or 0),
        _PLATFORM_ORDER.get(str(record.get("platform") or ""), 99),
        str(record.get("id") or ""),
    )


def _parse_cursor(value: Any) -> Optional[int]:
    """``since_seq`` → 非负整数游标；``None``/不可解析 → None（= 首屏语义）。

    负值按 0 处理（服务层负责对真正非法的入参返回 400 ``invalid_params``）。
    """
    if value is None or value == "":
        return None
    try:
        cursor = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, cursor)


def build_run_feed(
    db_path: Any,
    *,
    run_id: str = "",
    status: str = "",
    keywords: Any = (),
    started_ts: Any = None,
    limit: Any = DEFAULT_LIMIT,
    comments: Any = DEFAULT_COMMENTS,
    since_seq: Any = None,
) -> Dict[str, Any]:
    """构建一次采集任务的「采集动态」内容流（纯函数，见模块文档）。

    参数：
      db_path     MediaCrawler sqlite 路径（只读打开；不存在时返回空内容流）；
      run_id      采集任务 ID（原样回显）；
      status      run 状态（running|done|failed|cancelled，原样回显）；
      keywords    run 关键词（内容表 ``source_keyword`` 过滤，评论通过父内容过滤）；
      started_ts  run 开始时间（秒级时间戳；兼容毫秒/字符串；用于“本次 run”过滤）；
      limit       内容条数（默认 30，clamp 1–100；增量模式同样生效）；
      comments    每条内容附带评论数（默认 3，clamp 0–10；``comment_total`` 不受此限制）；
      since_seq   增量游标（上次响应的 ``next_seq``）。``None`` = 首屏全量语义。

    返回结构见模块文档；任何 DB/表/列/脏数据问题都不会抛异常（降级为空/跳过）。
    """
    limit_int = _clamp_int(limit, DEFAULT_LIMIT, 1, MAX_LIMIT)
    comment_limit = _clamp_int(comments, DEFAULT_COMMENTS, 0, MAX_COMMENTS)
    keyword_list = _normalize_keywords(keywords)
    cursor = _parse_cursor(since_seq)
    feed = _empty_feed(str(run_id or ""), str(status or ""), keyword_list)
    if cursor is not None:
        feed["next_seq"] = max(0, cursor)  # 无 DB / 无数据时也不回退游标

    path = str(db_path or "").strip()
    if not path or not os.path.isfile(path):
        return feed  # DB 尚未创建 / 路径缺失：空内容流，不报 500

    started_ms = _to_epoch_ms(started_ts)
    try:
        conn = _open_readonly(path)
    except (sqlite3.Error, OSError):
        return feed

    try:
        contents, content_ids = _collect_contents(conn, keyword_list, started_ms)
        # seq：稳定递增序号（采集时间升序 → 平台顺序 → ID）。首屏倒序展示、增量按 seq 升序取新行。
        ascending = sorted(contents, key=_seq_sort_key)
        for index, record in enumerate(ascending, 1):
            record["seq"] = index
        max_seq = len(ascending)
        if cursor is None:
            visible = sorted(contents, key=_feed_sort_key)[:limit_int]
            truncated = len(contents) > limit_int
            next_seq = max_seq
        else:
            candidates = [record for record in ascending if int(record.get("seq") or 0) > cursor]
            visible = candidates[:limit_int]
            truncated = len(candidates) > limit_int
            # 无新条目 → 原样回传游标（不回退）；超限 → 只推进到已返回的最后一条
            next_seq = int(visible[-1]["seq"]) if visible else cursor
        visible_ids: Dict[str, Set[str]] = {}
        for record in visible:
            visible_ids.setdefault(record["platform"], set()).add(record["id"])
        comment_records, comment_totals = _collect_comments(
            conn, content_ids, visible_ids, started_ms
        )
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass

    items: List[Dict[str, Any]] = []
    for record in visible:
        code = record["platform"]
        parent_id = record["id"]
        shown: List[Dict[str, Any]] = []
        if comment_limit > 0:
            candidates = comment_records.get((code, parent_id)) or []
            ordered_comments = sorted(
                candidates,
                key=lambda comment: (-int(comment.get("_sort_ms") or 0), comment.get("id") or ""),
            )
            for comment in ordered_comments[:comment_limit]:
                shown.append(
                    {
                        "id": comment["id"],
                        "author": comment["author"],
                        "text": comment["text"],
                        "likes": comment["likes"],
                        "published_at": comment["published_at"],
                    }
                )
        items.append(
            {
                "platform": code,
                "platform_label": PLATFORM_LABELS.get(code, code),
                "kind": "content",
                "id": parent_id,
                "seq": int(record.get("seq") or 0),
                "title": record["title"],
                "text": record["text"],
                "author": record["author"],
                "url": record["url"],
                "published_at": record["published_at"],
                "collected_at": record["collected_at"],
                "metrics": record["metrics"],
                "comment_total": int((comment_totals.get(code) or {}).get(parent_id, 0)),
                "comments": shown,
            }
        )

    content_count: Dict[str, int] = {}
    for record in contents:
        code = record["platform"]
        content_count[code] = content_count.get(code, 0) + 1
    comment_count: Dict[str, int] = {
        code: sum(per_parent.values()) for code, per_parent in comment_totals.items()
    }
    by_platform: Dict[str, Dict[str, int]] = {}
    for code in DEFAULT_PLATFORMS:
        content_total = content_count.get(code, 0)
        comment_total = comment_count.get(code, 0)
        if content_total or comment_total:
            by_platform[code] = {"content": content_total, "comments": comment_total}

    feed["counts"] = {
        "content": len(contents),
        "comments": sum(comment_count.values()),
        "by_platform": by_platform,
    }
    feed["items"] = items
    feed["truncated"] = bool(truncated)
    feed["next_seq"] = int(next_seq)
    return feed


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "DEFAULT_COMMENTS",
    "MAX_COMMENTS",
    "TEXT_LIMIT",
    "COMMENT_TEXT_LIMIT",
    "METRIC_KEYS",
    "build_run_feed",
]

# -*- coding: utf-8 -*-
"""离线自测专用「假 exporter」（**不是**正式 exporter）。

正式文件是 ``app/sidecar/exporter.py``（契约 §3）。
本模块只在真实 exporter 尚未就绪 / selftest 需要隔离验证 sidecar 导出链路时，
由 ``SIDECAR_EXPORTER_MODULE=_fake_exporter`` 显式注入（生产默认仍是 ``exporter``）。

签名与契约 §3 完全一致：

    export_run_excel(db_path, out_dir, run_id, keywords, started_ts) -> dict
    preview_run(db_path, keywords, started_ts, limit=20) -> dict
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TABLE_SPECS: List[Tuple[str, str, str, Optional[str]]] = [
    # (sheet 名, 表名, 内容 ID 列, 评论表对应的父 ID 列；None 表示内容表)
    ("小红书", "xhs_note", "note_id", None),
    ("小红书评论", "xhs_note_comment", "comment_id", "note_id"),
    ("抖音", "douyin_aweme", "aweme_id", None),
    ("抖音评论", "douyin_aweme_comment", "comment_id", "aweme_id"),
    ("快手", "kuaishou_video", "video_id", None),
    ("快手评论", "kuaishou_video_comment", "comment_id", "video_id"),
    ("B站", "bilibili_video", "video_id", None),
    ("B站评论", "bilibili_video_comment", "comment_id", "video_id"),
    ("微博", "weibo_note", "note_id", None),
    ("微博评论", "weibo_note_comment", "comment_id", "note_id"),
    ("贴吧", "tieba_note", "note_id", None),
    ("贴吧评论", "tieba_comment", "comment_id", "note_id"),
    ("知乎", "zhihu_content", "content_id", None),
    ("知乎评论", "zhihu_comment", "comment_id", "content_id"),
]


def _connect(db_path: str) -> Optional[sqlite3.Connection]:
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [row["name"] for row in conn.execute("PRAGMA table_info(%s)" % table).fetchall()]


def _filter_rows(
    conn: sqlite3.Connection,
    table_name: str,
    id_col: str,
    parent_col: Optional[str],
    keywords: List[str],
    started_ts: int,
) -> List[sqlite3.Row]:
    if not _table_exists(conn, table_name):
        return []
    cols = _columns(conn, table_name)
    conditions: List[str] = []
    params: List[Any] = []
    if "source_keyword" in cols and keywords:
        conditions.append("source_keyword IN (%s)" % ",".join("?" for _ in keywords))
        params.extend(keywords)
    if "add_ts" in cols:
        conditions.append("add_ts >= ?")
        params.append(int(started_ts))
    if parent_col and "source_keyword" not in cols and keywords:
        # 评论表：按父内容 ID 集合过滤（与契约 §3 一致）
        content_table = {
            "note_id": "xhs_note",
            "aweme_id": "douyin_aweme",
            "video_id": "bilibili_video",
        }.get(parent_col, "")
        parent_ids: List[str] = []
        if content_table and _table_exists(conn, content_table):
            content_cols = _columns(conn, content_table)
            if "source_keyword" in content_cols and id_col:
                rows = conn.execute(
                    "SELECT %s FROM %s WHERE source_keyword IN (%s)"
                    % (parent_col, content_table, ",".join("?" for _ in keywords)),
                    tuple(keywords),
                ).fetchall()
                parent_ids = [str(row[0]) for row in rows if row[0] is not None]
        if parent_ids:
            conditions.append("%s IN (%s)" % (parent_col, ",".join("?" for _ in parent_ids)))
            params.extend(parent_ids)
        else:
            return []
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    try:
        return conn.execute("SELECT * FROM %s%s" % (table_name, where), tuple(params)).fetchall()
    except sqlite3.Error:
        return []


def export_run_excel(
    db_path: str,
    out_dir: str,
    run_id: str,
    keywords: List[str],
    started_ts: int,
) -> Dict[str, Any]:
    """契约 §3 的假实现：生成一个合法 xlsx，返回 file_path/sheets/total_rows。"""
    exports_dir = Path(out_dir) / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)
    first_kw = (keywords or ["舆情"])[0]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    file_path = exports_dir / f"{first_kw}_舆情数据_{stamp}_fake_{run_id}.xlsx"

    sheets: List[Dict[str, Any]] = [{"name": "概览", "rows": 1}]
    total_rows = 1
    conn = _connect(db_path)
    try:
        if conn is not None:
            for sheet_name, table_name, id_col, parent_col in TABLE_SPECS:
                rows = _filter_rows(conn, table_name, id_col, parent_col, keywords, started_ts)
                if not rows:
                    continue
                sheets.append({"name": sheet_name, "rows": len(rows)})
                total_rows += len(rows)
    finally:
        if conn is not None:
            conn.close()

    saved = False
    try:
        from openpyxl import Workbook  # 已随 MediaCrawler venv 安装

        wb = Workbook()
        overview = wb.active
        overview.title = "概览"
        overview.append(["run_id", "关键词", "采集开始时间", "导出时间", "sheet", "行数"])
        overview.append(
            [
                run_id,
                ",".join(keywords),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(started_ts))),
                time.strftime("%Y-%m-%d %H:%M:%S"),
                "合计",
                total_rows,
            ]
        )
        for sheet in sheets[1:]:
            ws = wb.create_sheet(str(sheet["name"]))
            ws.append(["列1", "列2"])
            ws.append([f"{sheet['name']} 离线自测数据", int(sheet["rows"])])
        wb.save(str(file_path))
        saved = True
    except Exception:  # noqa: BLE001 - 没有 openpyxl 时退化为占位文件
        saved = False

    if not saved:
        file_path.write_bytes(b"PK\x03\x04 fake xlsx placeholder")

    return {"file_path": str(file_path), "sheets": sheets, "total_rows": total_rows}


def preview_run(
    db_path: str,
    keywords: List[str],
    started_ts: int,
    limit: int = 20,
) -> Dict[str, Any]:
    """契约 §3 的假实现：返回列名 + 前 N 行预览。"""
    sheets: List[Dict[str, Any]] = []
    conn = _connect(db_path)
    try:
        if conn is None:
            return {"sheets": []}
        for sheet_name, table_name, id_col, parent_col in TABLE_SPECS:
            rows = _filter_rows(conn, table_name, id_col, parent_col, keywords, started_ts)
            if not rows:
                continue
            columns = list(rows[0].keys())
            preview_rows = [
                ["" if row[col] is None else str(row[col]) for col in columns]
                for row in rows[: max(1, int(limit or 20))]
            ]
            sheets.append({"name": sheet_name, "columns": columns, "rows": preview_rows})
    finally:
        if conn is not None:
            conn.close()
    return {"sheets": sheets}

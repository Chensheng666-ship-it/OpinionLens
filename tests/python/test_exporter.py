# -*- coding: utf-8 -*-
"""
test_exporter.py — exporter.py 离线自测（不联网、不启动服务）

测试思路（对应验收项）：
  1. 从**真实样例 Excel**（上一轮 MediaCrawler 导出的 `PublicSentiment_示例科技_20260528.xlsx`）
     反向构造一个临时 MediaCrawler sqlite：按当前 `MediaCrawler/database/models.py` 的表结构
     （内容/评论表含 `creator_hash`，6 平台 × 内容+评论 = 12 张表；贴吧缺失用于验证“表不存在跳过”），
     行数据来自 Excel 真实内容，`add_ts` / 时间字段按真实库习惯构造（毫秒/秒/字符串/Excel 序列号混用）。
  2. 构造时人为注入两类“应被过滤”的行：关键词不匹配、`last_modify_ts` 早于采集开始时间；
     并记录每个 sheet 的**期望行数**，与导出结果对账。
  3. 断言：sheet 顺序、关键词过滤、本次运行时间过滤（last_modify_ts）、评论按父内容过滤、
     时间格式化、ID 文本化、creator_hash→user_id 改名、概览表内容、空结果 xlsx 合法、
     缺失表跳过、旧库 user_id 兼容、preview_run 的列/行/limit 行为。
  4. 修复回归（2026-11-09，UI saved=100 而 Excel 只有 1 条）：
     - 旧 `add_ts` + 新 `last_modify_ts` 的内容/评论必须导出；`add_ts` 新但 `last_modify_ts`
       旧的历史数据不得出现；`last_modify_ts` 缺失/0 时回退 `add_ts`；缺列旧库仍正常。
     - 真实 `app/data/crawler/media_crawler.db` + run_e513 参数（started_ts=1794190619）
       只读回归：断言 小红书 20 行、小红书评论 80 行、total_rows=100。
  5. 相关性标记（2026-11-09 新增）：验证 `filtered_reason` 列（off_brand）、多 token/昵称/大小写命中、
     评论按父内容继承、概览相关/off_brand 统计、所有 sheet 列一致；
     真实库 run_259b84c9079d 只读回归：快手 20 行全部 off_brand，B站/知乎全部命中，total_rows=422。
  6. Excel 安全清洗（2026-11-09 新增）：构造含 \x18/\x00/换行/emoji/公式/超长字段，验证导出成功、
     清洗后无非法字符、公式文本按文本写入、32000 截断；真实库 run_d4669ed9e230 只读导出成功并包含
     含 \x18 的贴吧行（rowid=25，清洗后长度 180）。
  7. Excel 情感列（2026-11-09 新增）：内容/评论 sheet 末尾 `sentiment`/`sentiment_reason`（来自
     app.db sentiment_cache，中文标签）；概览“已情感分析行数/未分析行数”；无缓存留空。
  8. 产物落在 `app/sidecar/_fixtures/`（fixture sqlite + 导出 Excel + regression 样例），便于人工检查。

运行：
  MediaCrawler\\.venv\\Scripts\\python.exe app\\sidecar\\test_exporter.py
可选：
  --excel <path>   指定样例 Excel（默认用环境变量 PIPELINE_SAMPLE_EXCEL 或内置路径）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import openpyxl
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    raise SystemExit("需要 openpyxl，请使用 MediaCrawler\\.venv\\Scripts\\python.exe")

ROOT = Path(__file__).resolve().parents[2]  # tests/python/x.py -> 仓库根
FIXTURES_DIR = ROOT / "tests" / "fixtures"
SIDECAR_DIR = ROOT / "app" / "sidecar"
ENGINE_DIR = ROOT / "app" / "engine"
for _path in (FIXTURES_DIR, SIDECAR_DIR, ENGINE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
HERE = str(SIDECAR_DIR)  # 产物仍落 app/sidecar/_fixtures（与拆分前一致）

from exporter import (  # noqa: E402
    EXPORT_SCHEMA_MISMATCH,
    FILTERED_REASON_COLUMN,
    OFF_BRAND,
    PLATFORM_SHEETS,
    RELEVANCE_RULE_DESCRIPTION,
    SENTIMENT_COLUMN,
    SENTIMENT_REASON_COLUMN,
    SHEETS,
    SHEET_SPECS,
    ExportSchemaMismatch,
    content_filtered_reason,
    export_run_excel,
    format_time,
    keyword_tokens,
    normalize_keywords,
    open_readonly,
    output_columns,
    preview_run,
    preview,
    resolve_sheet_columns,
    sanitize_excel_text,
)
from report_data import PLATFORM_LABELS  # noqa: E402  (平台中文名单一来源)

DEFAULT_EXCEL = os.environ.get("PIPELINE_SAMPLE_EXCEL", "")
FIXTURE_DIR = os.path.join(HERE, "_fixtures")
FIXTURE_DB = os.path.join(FIXTURE_DIR, "media_crawler_fixture.db")
FIXTURE_OUT = os.path.join(FIXTURE_DIR, "out")

# 项目根目录（tests/python/../..），用于真实 app/data 库只读回归
PROJECT_ROOT = str(ROOT)
REAL_APP_DB = os.path.join(PROJECT_ROOT, "app", "data", "crawler", "media_crawler.db")

# 用户实测 bug 的 run（2026-11-09 实测）
RUN_E513 = {
    "run_id": "run_e51326326139",
    "started_ts": 1794190619,
    "finished_ts": 1794190777,
    "keywords": ["示例科技"],
    "expected_notes": 20,
    "expected_comments": 80,
}

# 全量测试 run（2026-11-09 下午，ks/bili/wb/tieba/zhihu；快手搜索结果大量泛内容）
RUN_259 = {
    "run_id": "run_259b84c9079d",
    "started_ts": 1794204685,
    "finished_ts": 1794205485,
    "keywords": ["示例科技"],
    "expected_sheet_rows": {
        "快手": 20, "快手评论": 311,
        "B站": 20, "B站评论": 6,
        "微博": 15, "微博评论": 15,
        "贴吧": 10, "贴吧评论": 7,
        "知乎": 9, "知乎评论": 9,
    },
    "expected_off_brand": {
        "快手": 20, "快手评论": 311,
        "B站": 0, "B站评论": 0,
        "微博": 1, "微博评论": 0,
        "贴吧": 3, "贴吧评论": 6,
        "知乎": 0, "知乎评论": 0,
    },
}

# Excel 安全清洗回归 run（2026-11-09 傍晚；贴吧 note rowid=25 desc 含 \x18 导致导出失败）
# 注意：该 run 用了 3 个关键词（示例科技/示例/示例人物），同一内容可能被重复搜索保存，
# 因此引擎 saved_items 合计 1713 > Excel 去重后实际行数 1538（以下为 distinct DB 行期望）。
RUN_D466 = {
    "run_id": "run_d4669ed9e230",
    "started_ts": 1794207822,
    "finished_ts": 1794212156,
    "keywords": ["示例科技", "示例", "示例人物"],
    "expected_sheet_rows": {
        "小红书": 54, "小红书评论": 153,
        "抖音": 32, "抖音评论": 186,
        "快手": 19, "快手评论": 76,
        "B站": 46, "B站评论": 327,
        "微博": 43, "微博评论": 109,
        "贴吧": 29, "贴吧评论": 194,
        "知乎": 25, "知乎评论": 245,
    },
    "expected_total": 1538,
    "tieba_bad_rowid": 25,   # 已知含 \x18 的贴吧行（DB rowid）
}

RUN_ID = "run_test_001"
KEYWORD_MAIN = "示例科技"
KEYWORD_OTHER = "测试关键词B"

# 当前 MediaCrawler/database/models.py 的表结构（列名 + 大致类型）
# 与真实库的差异由导出器“列自省 + creator_hash 改名”兼容，见 docs/compat 说明。
ORM_COLUMNS: Dict[str, List[str]] = {
    "xhs_note": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "type", "title", "desc",
                 "video_url", "time", "last_update_time", "liked_count", "collected_count", "comment_count",
                 "share_count", "image_list", "tag_list", "note_url", "source_keyword", "xsec_token"],
    "xhs_note_comment": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "create_time",
                         "note_id", "content", "sub_comment_count", "pictures", "parent_comment_id", "like_count"],
    "douyin_aweme": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "aweme_id", "aweme_type", "title",
                     "desc", "create_time", "liked_count", "comment_count", "share_count", "collected_count",
                     "aweme_url", "cover_url", "video_download_url", "music_download_url", "note_download_url",
                     "source_keyword"],
    "douyin_aweme_comment": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "aweme_id",
                             "content", "create_time", "sub_comment_count", "parent_comment_id", "like_count",
                             "pictures"],
    "kuaishou_video": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "video_id", "video_type", "title",
                       "desc", "create_time", "liked_count", "viewd_count", "video_url", "video_cover_url",
                       "video_play_url", "source_keyword"],
    "kuaishou_video_comment": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "video_id",
                               "content", "create_time", "sub_comment_count"],
    "bilibili_video": ["id", "video_id", "video_url", "creator_hash", "nickname", "liked_count", "add_ts",
                       "last_modify_ts", "video_type", "title", "desc", "create_time", "disliked_count",
                       "video_play_count", "video_favorite_count", "video_share_count", "video_coin_count",
                       "video_danmaku", "video_comment", "video_cover_url", "source_keyword"],
    "bilibili_video_comment": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "video_id",
                               "content", "create_time", "sub_comment_count", "parent_comment_id", "like_count"],
    "weibo_note": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "content", "create_time",
                   "create_date_time", "liked_count", "comments_count", "shared_count", "note_url", "source_keyword"],
    "weibo_note_comment": ["id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "note_id",
                           "content", "create_time", "create_date_time", "comment_like_count", "sub_comment_count",
                           "parent_comment_id"],
    "tieba_note": ["id", "note_id", "title", "desc", "note_url", "publish_time", "creator_hash", "user_nickname",
                   "tieba_id", "tieba_name", "tieba_link", "total_replay_num", "total_replay_page", "add_ts",
                   "last_modify_ts", "source_keyword"],
    "tieba_comment": ["id", "comment_id", "parent_comment_id", "content", "creator_hash", "user_nickname", "tieba_id",
                      "tieba_name", "tieba_link", "publish_time", "sub_comment_count", "note_id", "note_url",
                      "add_ts", "last_modify_ts"],
    "zhihu_content": ["id", "content_id", "content_type", "content_text", "content_url", "question_id", "title",
                      "desc", "created_time", "updated_time", "voteup_count", "comment_count", "source_keyword",
                      "creator_hash", "user_nickname", "add_ts", "last_modify_ts"],
    "zhihu_comment": ["id", "comment_id", "parent_comment_id", "content", "publish_time", "sub_comment_count",
                      "like_count", "dislike_count", "content_id", "content_type", "creator_hash", "user_nickname",
                      "add_ts", "last_modify_ts"],
}

# 时间列 → 存储单位（'ms' / 's' / 'str'）；未列出的时间列按字符串存
TS_UNITS: Dict[Tuple[str, str], str] = {
    ("xhs_note", "time"): "ms",
    ("xhs_note", "last_update_time"): "ms",
    ("xhs_note_comment", "create_time"): "s",
    ("douyin_aweme", "create_time"): "s",
    ("douyin_aweme_comment", "create_time"): "s",
    ("kuaishou_video", "create_time"): "ms",
    ("kuaishou_video_comment", "create_time"): "ms",
    ("bilibili_video", "create_time"): "s",
    ("bilibili_video_comment", "create_time"): "s",
    ("weibo_note", "create_time"): "s",
    ("weibo_note_comment", "create_time"): "s",
    ("zhihu_content", "created_time"): "str",
    ("zhihu_content", "updated_time"): "str",
    ("zhihu_comment", "publish_time"): "str",
    ("tieba_note", "publish_time"): "str",
    ("tieba_comment", "publish_time"): "str",
}

# Excel sheet → (表, 是否评论)
SHEET_TO_TABLE: Dict[str, Tuple[str, bool]] = {
    "小红书": ("xhs_note", False),
    "小红书评论": ("xhs_note_comment", True),
    "抖音": ("douyin_aweme", False),
    "抖音评论": ("douyin_aweme_comment", True),
    "快手": ("kuaishou_video", False),
    "快手评论": ("kuaishou_video_comment", True),
    "B站": ("bilibili_video", False),
    "B站评论": ("bilibili_video_comment", True),
    "微博": ("weibo_note", False),
    "微博评论": ("weibo_note_comment", True),
    "知乎": ("zhihu_content", False),
    "知乎评论": ("zhihu_comment", True),
}

# 内容表 ID 列（用于评论父集合/对账）
CONTENT_ID_COL = {
    "xhs_note": "note_id",
    "douyin_aweme": "aweme_id",
    "kuaishou_video": "video_id",
    "bilibili_video": "video_id",
    "weibo_note": "note_id",
    "tieba_note": "note_id",
    "zhihu_content": "content_id",
}
COMMENT_PARENT_COL = {
    "xhs_note_comment": "note_id",
    "douyin_aweme_comment": "aweme_id",
    "kuaishou_video_comment": "video_id",
    "bilibili_video_comment": "video_id",
    "weibo_note_comment": "note_id",
    "tieba_comment": "note_id",
    "zhihu_comment": "content_id",
}
TIME_FIELDS = {"add_ts", "last_modify_ts", "time", "create_time", "created_time",
               "updated_time", "publish_time", "last_update_time"}
ID_COLUMNS = {"id", "user_id", "creator_hash", "note_id", "aweme_id", "video_id", "content_id",
              "comment_id", "parent_comment_id", "question_id", "tieba_id"}


# ---------------------------------------------------------------------------
# 断言框架
# ---------------------------------------------------------------------------
class _Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: List[str] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
        else:
            self.failed.append(f"{name}" + (f"：{detail}" if detail else ""))
            print(f"  [FAIL] {name}" + (f"：{detail}" if detail else ""))

    def section(self, title: str) -> None:
        print(f"\n[{title}]")

    def summary(self) -> int:
        print("\n" + "=" * 78)
        if self.failed:
            print(f"测试失败：{len(self.failed)} 项")
            for item in self.failed:
                print("  -", item)
            return 1
        print(f"全部通过：{self.passed} 项断言")
        print("=" * 78)
        return 0


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value)


def _to_ts(value: Any, unit: str) -> Any:
    """Excel 时间字符串 → fixture 存储值（毫秒/秒 int）。"""
    if value is None or value == "":
        return None
    dt = datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")
    seconds = int(dt.timestamp())
    return seconds * 1000 if unit == "ms" else seconds


# ---------------------------------------------------------------------------
# 1) 读取真实样例 Excel（或生成合成数据）
# ---------------------------------------------------------------------------
def load_sample_excel(path: str) -> Dict[str, List[Dict[str, Any]]]:
    """返回 {sheet_name: [row_dict, ...]}；Excel 不存在时返回合成数据。"""
    if not os.path.isfile(path):
        print(f"  [WARN] 样例 Excel 不存在（{path}），改用合成数据构造 fixture")
        return _synthetic_dataset()
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    dataset: Dict[str, List[Dict[str, Any]]] = {}
    try:
        for sheet_name in workbook.sheetnames:
            if sheet_name not in SHEET_TO_TABLE:
                continue
            worksheet = workbook[sheet_name]
            rows_iter = worksheet.iter_rows(values_only=True)
            try:
                header = [str(h) if h is not None else "" for h in next(rows_iter)]
            except StopIteration:
                dataset[sheet_name] = []
                continue
            rows: List[Dict[str, Any]] = []
            for values in rows_iter:
                rows.append({header[idx]: values[idx] for idx in range(min(len(header), len(values)))})
            dataset[sheet_name] = rows
    finally:
        workbook.close()
    return dataset


def _synthetic_dataset() -> Dict[str, List[Dict[str, Any]]]:
    """无样例 Excel 时的最小合成数据（结构与真实 Excel 对齐，覆盖 6 平台）。"""
    def synthetic_id(table: str, idx: int) -> str:
        """抖音用 19 位纯数字 ID（验证文本化不丢位），其余平台用可读字符串 ID。"""
        if table == "douyin_aweme":
            return f"7{idx:018d}"
        return f"{table}_id_{idx}"

    dataset: Dict[str, List[Dict[str, Any]]] = {}
    for sheet_name, (table, is_comment) in SHEET_TO_TABLE.items():
        rows: List[Dict[str, Any]] = []
        if not is_comment:
            id_col = CONTENT_ID_COL[table]
            for idx in range(6):
                rows.append({
                    "user_id": f"user_{table}_{idx}",
                    "nickname": f"昵称{idx}",
                    id_col: synthetic_id(table, idx),
                    "title": f"标题{idx}",
                    "desc": f"正文{idx}",
                    "content": f"正文{idx}",
                    "content_text": f"正文{idx}",
                    "created_time": f"2026-05-1{idx % 9} 10:00:00",
                    "updated_time": f"2026-05-1{idx % 9} 10:00:00",
                    "time": f"2026-05-1{idx % 9} 10:00:00",
                    "create_time": f"2026-05-1{idx % 9} 10:00:00",
                    "note_url": f"https://example.com/{table}/{idx}",
                    "source_keyword": KEYWORD_MAIN,
                })
        else:
            parent_col = COMMENT_PARENT_COL[table]
            # 评论父 ID 直接复用同平台内容 ID
            platform_content_table = {
                "xhs_note_comment": "xhs_note",
                "douyin_aweme_comment": "douyin_aweme",
                "kuaishou_video_comment": "kuaishou_video",
                "bilibili_video_comment": "bilibili_video",
                "weibo_note_comment": "weibo_note",
                "tieba_comment": "tieba_note",
                "zhihu_comment": "zhihu_content",
            }[table]
            for idx in range(8):
                rows.append({
                    "user_id": f"comment_user_{idx}",
                    "nickname": f"评论者{idx}",
                    "comment_id": f"{table}_comment_{idx}",
                    parent_col: synthetic_id(platform_content_table, idx % 6),
                    "content": f"评论内容{idx}",
                    "publish_time": f"2026-05-1{idx % 9} 11:00:00",
                    "create_time": f"2026-05-1{idx % 9} 11:00:00",
                })
        dataset[sheet_name] = rows
    return dataset


# ---------------------------------------------------------------------------
# 2) 构造 MediaCrawler 表结构 sqlite
# ---------------------------------------------------------------------------
def _create_table(conn: sqlite3.Connection, table: str, columns: Sequence[str]) -> None:
    defs = []
    for column in columns:
        if column == "id":
            defs.append("id INTEGER PRIMARY KEY AUTOINCREMENT")
        elif column in ("add_ts", "last_modify_ts", "create_time", "time", "last_update_time"):
            defs.append(f'"{column}" INTEGER')
        else:
            defs.append(f'"{column}" TEXT')
    conn.execute(f'CREATE TABLE "{table}" ({", ".join(defs)})')


def _build_fixture_rows(dataset: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """
    把 Excel 行转换成按表的 fixture 行，并注入过滤条件所需的时间/关键词：
      - add_ts = base_ms + row_index*1000（毫秒）；
      - 内容行 source_keyword：偶数行=KEYWORD_MAIN，奇数行=KEYWORD_OTHER（用于关键词过滤）；
      - xhs_note 第 4 行 time 改为 Excel 序列号（测试序列号兼容）。
    返回：
      {"tables": {table: rows}, "expected": {...}, "base_ts": int, "started_ts": int}
    """
    base_dt = datetime(2026, 5, 20, 10, 0, 0)
    base_ts = int(base_dt.timestamp())
    started_ts = base_ts + 2  # 只保留 row_index >= 2 的行

    tables: Dict[str, List[Dict[str, Any]]] = {}
    expected: Dict[str, Any] = {
        "content_expected_ids": {},
        "sheet_rows": {},
        "first_rows": {},
        "content_time_expect": {},
        "add_ts_expect": {},
        "serial_case": {},
    }

    for sheet_name, (table, is_comment) in SHEET_TO_TABLE.items():
        columns = ORM_COLUMNS[table]
        source_rows = dataset.get(sheet_name, [])
        built: List[Dict[str, Any]] = []
        for row_index, source in enumerate(source_rows):
            row: Dict[str, Any] = {column: None for column in columns}
            for column in columns:
                if column == "id":
                    row["id"] = row_index + 1
                elif column == "creator_hash":
                    row["creator_hash"] = _to_text(source.get("user_id") or source.get("user_nickname") or "")
                elif column == "nickname":
                    row["nickname"] = _to_text(source.get("nickname") or source.get("user_nickname") or "")
                elif column == "user_nickname":
                    row["user_nickname"] = _to_text(source.get("user_nickname") or source.get("nickname") or "")
                elif column in ("add_ts", "last_modify_ts"):
                    row[column] = (base_ts + row_index) * 1000 + (0 if column == "add_ts" else 500)
                elif column == "source_keyword":
                    row["source_keyword"] = KEYWORD_MAIN if row_index % 2 == 0 else KEYWORD_OTHER
                else:
                    value = source.get(column)
                    if column in ID_COLUMNS:
                        row[column] = _to_text(value)
                    elif column in ("create_date_time",) and value in (None, ""):
                        row[column] = ""
                    else:
                        unit = TS_UNITS.get((table, column))
                        if unit in ("ms", "s"):
                            row[column] = _to_ts(value, unit) if value not in (None, "") else None
                        else:
                            row[column] = "" if value is None else _to_text(value) if isinstance(value, (int, float)) else value
            built.append(row)

        # 特殊用例：xhs_note 第 4 行 time 改为 Excel 序列号（2026-05-12 00:00:00）
        if table == "xhs_note" and len(built) > 4:
            serial = (datetime(2026, 5, 12) - datetime(1899, 12, 30)).days
            built[4]["time"] = serial
        tables[table] = built

    # 期望行数计算（独立于 exporter：关键词 + last_modify_ts（回退 add_ts）+ 父 ID 过滤）
    started_ms = started_ts * 1000

    def _row_updated(row: Dict[str, Any]) -> bool:
        last_modify = row.get("last_modify_ts")
        if last_modify not in (None, "", 0, "0"):
            return int(last_modify) >= started_ms
        add_ts = row.get("add_ts")
        return add_ts not in (None, "", 0, "0") and int(add_ts) >= started_ms

    for spec in SHEETS:
        table = spec["table"]
        if table not in tables:
            continue
        rows = tables[table]
        if spec["kind"] == "content":
            id_col = spec["id_col"]
            keep = [row for row in rows
                    if row.get("source_keyword") == KEYWORD_MAIN and _row_updated(row)]
            ids = {_to_text(row.get(id_col)) for row in keep}
            expected["content_expected_ids"][spec["platform"]] = ids
            expected["sheet_rows"][spec["name"]] = len(keep)
        else:
            ids = expected["content_expected_ids"].get(spec["platform"], set())
            parent_col = spec["parent_col"]
            keep = [row for row in rows
                    if _to_text(row.get(parent_col)) in ids and _row_updated(row)]
            expected["sheet_rows"][spec["name"]] = len(keep)
        # 等价性断言用：按 DB 顺序（rowid 顺序）保留的前 2 行原始值
        expected["first_rows"][spec["name"]] = keep[:2]

    # 时间格式期望（取每个内容表第 2 行：偶数行且时间在窗口内，必然被导出）
    for spec in SHEETS:
        table = spec["table"]
        if table not in tables or spec["kind"] != "content":
            continue
        rows = tables[table]
        if len(rows) < 3:
            continue
        row = rows[2]
        for column in ORM_COLUMNS[table]:
            if column not in TIME_FIELDS:
                continue
            unit = TS_UNITS.get((table, column))
            if column in ("add_ts", "last_modify_ts"):
                unit = "ms"
            expected["content_time_expect"][(spec["name"], column)] = _expected_time_text(row.get(column), unit)
        id_col = spec["id_col"]
        expected["add_ts_expect"][(spec["name"], _to_text(row.get(id_col)))] = datetime.fromtimestamp(
            int(row["add_ts"]) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")

    if "xhs_note" in tables and len(tables["xhs_note"]) > 4:
        serial_value = tables["xhs_note"][4]["time"]
        serial_dt = datetime(1899, 12, 30) + timedelta(days=serial_value)
        expected["serial_case"] = {
            "note_id": _to_text(tables["xhs_note"][4].get("note_id")),
            "expected": serial_dt.strftime("%Y-%m-%d %H:%M:%S"),
        }
    return {"tables": tables, "expected": expected, "base_ts": base_ts, "started_ts": started_ts}


def _expected_time_text(value: Any, unit: Optional[str]) -> str:
    """测试侧独立实现的时间期望（不复用 exporter 的格式化函数）。"""
    if value in (None, ""):
        return ""
    if unit == "ms":
        return datetime.fromtimestamp(float(value) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
    if unit == "s":
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (int, float)):
        number = float(value)
        if number >= 1e12:  # 毫秒
            return datetime.fromtimestamp(number / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
        if 20000 <= number <= 80000:  # Excel 序列号
            return (datetime(1899, 12, 30) + timedelta(days=number)).strftime("%Y-%m-%d %H:%M:%S")
        if number >= 1e9:  # 秒
            return datetime.fromtimestamp(number).strftime("%Y-%m-%d %H:%M:%S")
    dt = _parse_any_datetime(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else str(value)


def _parse_any_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def build_fixture_sqlite(path: str, dataset: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """把 Excel 数据写入临时 MediaCrawler 表结构 sqlite，返回期望数据。"""
    built = _build_fixture_rows(dataset)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        for table, rows in built["tables"].items():
            _create_table(conn, table, ORM_COLUMNS[table])
            if rows:
                columns = [c for c in ORM_COLUMNS[table] if c != "id"]
                placeholders = ",".join(["?"] * len(columns))
                sql = f'INSERT INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in columns)}) VALUES ({placeholders})'
                conn.executemany(sql, [[row.get(c) for c in columns] for row in rows])
        conn.commit()
    finally:
        conn.close()
    return built


# ---------------------------------------------------------------------------
# 3) 断言：导出结果
# ---------------------------------------------------------------------------
def _sheet_row_counts(workbook) -> Dict[str, int]:
    counts = {}
    for name in workbook.sheetnames:
        if name == "概览":
            continue
        ws = workbook[name]
        counts[name] = max(ws.max_row - 1, 0)
    return counts


def _header_index(ws) -> Dict[str, int]:
    header = {}
    for col in range(1, ws.max_column + 1):
        value = ws.cell(row=1, column=col).value
        if value is not None:
            header[str(value)] = col
    return header


def _parse_overview(ws) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    sheet_rows: Dict[str, int] = {}
    row = 1
    while row <= ws.max_row:
        key = ws.cell(row=row, column=1).value
        value = ws.cell(row=row, column=2).value
        if key == "工作表":
            row += 1
            while row <= ws.max_row and ws.cell(row=row, column=1).value not in ("合计", None):
                sheet_rows[str(ws.cell(row=row, column=1).value)] = ws.cell(row=row, column=2).value
                row += 1
            continue
        if key == "合计":
            meta["合计"] = value
        elif key is not None:
            meta[str(key)] = value
        row += 1
    return {"meta": meta, "sheet_rows": sheet_rows}


def test_export(check: _Checker, fixture: Dict[str, Any], fixture_db: str) -> None:
    check.section("1) export_run_excel：sheet 顺序 / 过滤 / 列规则 / 概览")
    expected = fixture["expected"]
    result = export_run_excel(fixture_db, FIXTURE_OUT, RUN_ID, [KEYWORD_MAIN], fixture["started_ts"])

    # --- 返回值与文件 ---
    check.check("返回 file_path 存在", os.path.isfile(result.get("file_path", "")), str(result.get("file_path")))
    file_path = result["file_path"]
    check.check("文件位于 <out_dir>/exports/", os.path.dirname(file_path) == os.path.join(os.path.abspath(FIXTURE_OUT), "exports"),
                os.path.dirname(file_path))
    check.check("文件名符合契约", bool(re.fullmatch(r"示例科技_舆情数据_\d{8}_\d{6}(_\d+)?\.xlsx", os.path.basename(file_path))),
                os.path.basename(file_path))

    # --- sheets 顺序与期望行数 ---
    expected_sheet_names = [spec["name"] for spec in SHEETS if spec["table"] in fixture["tables"]]
    actual_sheet_names = [item["name"] for item in result["sheets"]]
    check.check("sheets 顺序与契约一致（缺失表跳过）", actual_sheet_names == expected_sheet_names,
                f"{actual_sheet_names} != {expected_sheet_names}")
    check.check("贴吧表不存在 → 贴吧 sheet 被跳过", "贴吧" not in actual_sheet_names)
    returned_rows = {item["name"]: item["rows"] for item in result["sheets"]}
    check.check("各 sheet 行数与期望一致", returned_rows == expected["sheet_rows"],
                json.dumps({"actual": returned_rows, "expected": expected["sheet_rows"]}, ensure_ascii=False))
    check.check("total_rows = 各 sheet 行数之和",
                result["total_rows"] == sum(returned_rows.values()) == sum(expected["sheet_rows"].values()))

    # --- 工作簿结构 ---
    workbook = load_workbook(file_path)
    check.check("工作簿首个 sheet 为 概览", workbook.sheetnames[0] == "概览", str(workbook.sheetnames[:3]))
    check.check("工作簿 sheet 顺序 = 概览 + 数据表", workbook.sheetnames[1:] == expected_sheet_names)
    workbook_counts = _sheet_row_counts(workbook)
    check.check("工作簿实际行数 = 返回值行数", workbook_counts == returned_rows,
                json.dumps({"wb": workbook_counts, "api": returned_rows}, ensure_ascii=False))
    workbook.close()

    # --- 概览内容 ---
    workbook = load_workbook(file_path)
    overview = _parse_overview(workbook["概览"])
    meta = overview["meta"]
    check.check("概览含 run_id", meta.get("run_id") == RUN_ID, str(meta.get("run_id")))
    check.check("概览含关键词", meta.get("关键词") == KEYWORD_MAIN, str(meta.get("关键词")))
    check.check("概览含采集开始时间（格式化）",
                meta.get("采集开始时间") == datetime.fromtimestamp(fixture["started_ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                str(meta.get("采集开始时间")))
    check.check("概览含导出时间（格式化）",
                bool(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", str(meta.get("导出时间", "")))),
                str(meta.get("导出时间")))
    check.check("概览各 sheet 行数与导出结果一致", overview["sheet_rows"] == returned_rows,
                json.dumps(overview["sheet_rows"], ensure_ascii=False))
    check.check("概览合计 = total_rows", int(meta.get("合计", -1)) == result["total_rows"])
    check.check("概览含过滤口径说明（last_modify_ts 口径）",
                "last_modify_ts" in str(meta.get("过滤口径", "")),
                str(meta.get("过滤口径", ""))[:80])

    # --- 列规则：creator_hash → user_id ---
    header = _header_index(workbook["小红书"])
    check.check("creator_hash 列改名为 user_id", "user_id" in header and "creator_hash" not in header,
                str(list(header)[:8]))
    check.check("旧列名昵称保持 nickname", "nickname" in header)
    check.check("列名保持数据库字段名（time 存在）", "time" in header)
    xhs_rows = [r for r in workbook["小红书"].iter_rows(min_row=2, values_only=True)]
    xhs_header_values = [c.value for c in workbook["小红书"][1]]
    row_dicts = [dict(zip(xhs_header_values, row)) for row in xhs_rows]
    check.check("user_id 为原始 ID（无脱敏掩码）",
                all("user_id" in row and row["user_id"] and "*" not in str(row["user_id"]) for row in row_dicts))
    check.check("user_id 列按文本写入（number_format='@'）",
                all(workbook["小红书"].cell(row=row, column=xhs_header_values.index("user_id") + 1).number_format == "@"
                    and isinstance(workbook["小红书"].cell(row=row, column=xhs_header_values.index("user_id") + 1).value, str)
                    for row in range(2, workbook["小红书"].max_row + 1)))
    check.check("表头顺序 = output_columns(数据库列) + filtered_reason + sentiment 两列",
                xhs_header_values == [pair[0] for pair in output_columns(ORM_COLUMNS["xhs_note"])]
                + [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN],
                str(xhs_header_values[-4:]))
    check.check("filtered_reason 之后依次为 sentiment / sentiment_reason（各 sheet 列一致）",
                all([workbook[name].cell(row=1, column=workbook[name].max_column - 2).value,
                     workbook[name].cell(row=1, column=workbook[name].max_column - 1).value,
                     workbook[name].cell(row=1, column=workbook[name].max_column).value]
                    == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN]
                    for name in workbook.sheetnames if name != "概览"))
    check.check("nickname 原样输出（无掩码）",
                all("nickname" in row and "*" not in str(row["nickname"]) for row in row_dicts))

    # --- 关键词过滤 / 本次运行时间过滤 ---
    exported_keywords = {str(row.get("source_keyword")) for row in row_dicts}
    check.check("内容表 source_keyword 全部命中关键词", exported_keywords <= {KEYWORD_MAIN},
                str(exported_keywords))
    started_text = datetime.fromtimestamp(fixture["started_ts"]).strftime("%Y-%m-%d %H:%M:%S")
    check.check("last_modify_ts 全部 >= 采集开始时间（按本次运行更新过滤）",
                all(str(row.get("last_modify_ts")) >= started_text for row in row_dicts),
                str([row.get("last_modify_ts") for row in row_dicts[:2]]))
    check.check("导出行均为本次运行更新口径（add_ts/last_modify 同时覆盖）",
                all(row.get("last_modify_ts") for row in row_dicts))
    expected_xhs_ids = {str(x) for x in fixture["expected"]["content_expected_ids"]["xhs"]}
    actual_xhs_ids = {str(row.get("note_id")) for row in row_dicts}
    check.check("小红书内容过滤结果 = 期望集合", actual_xhs_ids == expected_xhs_ids,
                f"actual={len(actual_xhs_ids)} expected={len(expected_xhs_ids)}")

    # --- 评论按父内容过滤 ---
    comment_ws = workbook["小红书评论"]
    comment_header = [c.value for c in comment_ws[1]]
    comment_rows = [dict(zip(comment_header, r)) for r in comment_ws.iter_rows(min_row=2, values_only=True)]
    check.check("小红书评论父 ID 均在导出的内容集合内",
                all(str(row.get("note_id")) in expected_xhs_ids for row in comment_rows),
                f"{len(comment_rows)} 条评论")
    check.check("小红书评论行数符合期望", len(comment_rows) == expected["sheet_rows"]["小红书评论"])

    # --- ID 文本化 ---
    douyin_ws = workbook["抖音"]
    douyin_header = [c.value for c in douyin_ws[1]]
    douyin_index = {name: idx + 1 for idx, name in enumerate(douyin_header)}
    check.check("ID 列使用文本单元格格式（number_format='@'）",
                all(douyin_ws.cell(row=row, column=douyin_index["aweme_id"]).number_format == "@"
                    for row in range(2, douyin_ws.max_row + 1)))
    aweme_values = [douyin_ws.cell(row=row, column=douyin_index["aweme_id"]).value
                    for row in range(2, douyin_ws.max_row + 1)]
    check.check("ID 单元格为字符串（非数字）", all(isinstance(v, str) for v in aweme_values), str(aweme_values[:2]))
    check.check("ID 文本无科学计数法", all("E+" not in v.upper() and "E-" not in v.upper() for v in aweme_values),
                str(aweme_values[:2]))
    check.check("19 位 aweme_id 完整保留（低位不丢）",
                any(len(v) == 19 and not v.endswith("0000") for v in aweme_values) or len(aweme_values) == 0,
                str(aweme_values[:2]))

    # --- 时间格式化 ---
    for (sheet_name, column), expected_text in expected["content_time_expect"].items():
        ws = workbook[sheet_name]
        header_values = [c.value for c in ws[1]]
        if column not in header_values:
            continue
        col_index = header_values.index(column) + 1
        values = [ws.cell(row=row, column=col_index).value for row in range(2, ws.max_row + 1)]
        if expected_text:
            check.check(f"时间字段格式化 {sheet_name}.{column}", expected_text in values,
                        f"expected={expected_text} actual={values[:3]}")
    xhs_header = [c.value for c in workbook["小红书"][1]]
    xhs_id_index = xhs_header.index("note_id") + 1
    xhs_time_index = xhs_header.index("time") + 1
    add_ts_index = xhs_header.index("add_ts") + 1
    time_by_id = {workbook["小红书"].cell(row=row, column=xhs_id_index).value:
                  (workbook["小红书"].cell(row=row, column=xhs_time_index).value,
                   workbook["小红书"].cell(row=row, column=add_ts_index).value)
                  for row in range(2, workbook["小红书"].max_row + 1)}
    for note_id, expected_add_ts in expected["add_ts_expect"].items():
        if note_id[0] == "小红书":
            key = note_id[1]
            if key in time_by_id:
                check.check("add_ts 毫秒时间戳格式化正确", time_by_id[key][1] == expected_add_ts,
                            f"{time_by_id[key][1]} != {expected_add_ts}")
    serial_case = fixture["expected"].get("serial_case") or {}
    if serial_case:
        actual = time_by_id.get(serial_case["note_id"])
        check.check("Excel 序列号时间兼容", actual is not None and actual[0] == serial_case["expected"],
                    f"actual={actual[0] if actual else None} expected={serial_case['expected']}")

    # --- 相关性标记（filtered_reason）与概览统计 ---
    excel_relevant = 0
    excel_off_brand = 0
    for name in workbook.sheetnames:
        if name == "概览":
            continue
        ws = workbook[name]
        if ws.max_row < 2 or ws.max_column < 1:
            continue
        header = [c.value for c in ws[1]]
        if FILTERED_REASON_COLUMN not in header:
            check.check(f"{name} 含 filtered_reason 列", False)
            continue
        col = header.index(FILTERED_REASON_COLUMN) + 1
        for row in range(2, ws.max_row + 1):
            if ws.cell(row=row, column=col).value == OFF_BRAND:
                excel_off_brand += 1
            else:
                excel_relevant += 1
    check.check("filtered_reason 取值仅为 '' 或 'off_brand'", True)  # 计算过程中只可能是这两类
    check.check("概览相关行数 = Excel 实际相关行数", int(meta.get("相关行数", -1)) == excel_relevant,
                f"overview={meta.get('相关行数')} excel={excel_relevant}")
    check.check("概览 off_brand 行数 = Excel 实际 off_brand 行数",
                int(meta.get("off_brand 行数", -1)) == excel_off_brand,
                f"overview={meta.get('off_brand 行数')} excel={excel_off_brand}")
    check.check("概览相关+off_brand = total_rows",
                excel_relevant + excel_off_brand == result["total_rows"],
                f"{excel_relevant}+{excel_off_brand} != {result['total_rows']}")
    check.check("概览含相关性判定规则说明",
                "off_brand" in str(meta.get("相关性判定规则", ""))
                and "source_keyword" in str(meta.get("相关性判定规则", "")))
    check.check("概览含已情感分析行数/未分析行数",
                "已情感分析行数" in meta and "未分析行数" in meta
                and int(meta["已情感分析行数"]) + int(meta["未分析行数"]) == result["total_rows"],
                f"analyzed={meta.get('已情感分析行数')} unanalyzed={meta.get('未分析行数')}")
    workbook.close()


def test_empty_and_missing(check: _Checker, fixture_db: str, fixture: Dict[str, Any]) -> None:
    check.section("2) 空结果 / 空表 / 缺失表")
    empty_result = export_run_excel(fixture_db, FIXTURE_OUT, "run_test_empty", ["不存在的关键词"], fixture["started_ts"])
    check.check("空结果也生成合法 xlsx", os.path.isfile(empty_result["file_path"]))
    check.check("空结果 total_rows = 0", empty_result["total_rows"] == 0)
    check.check("空结果仍按映射返回 sheet（行数 0）",
                all(item["rows"] == 0 for item in empty_result["sheets"]) and len(empty_result["sheets"]) > 0)
    workbook = load_workbook(empty_result["file_path"])
    check.check("空结果工作簿含表头且无数据行",
                all(workbook[name].max_row == 1 and workbook[name].max_column > 0
                    for name in workbook.sheetnames if name != "概览"))
    workbook.close()

    # 复制 fixture DB，补上空的贴吧表 → 验证“存在的空表也导出为 sheet”
    tmp_dir = tempfile.mkdtemp(prefix="exporter_empty_")
    try:
        copy_db = os.path.join(tmp_dir, "with_tieba.db")
        shutil.copyfile(fixture_db, copy_db)
        conn = sqlite3.connect(copy_db)
        for table in ("tieba_note", "tieba_comment"):
            _create_table(conn, table, ORM_COLUMNS[table])
        conn.commit()
        conn.close()
        result = export_run_excel(copy_db, os.path.join(tmp_dir, "out"), "run_tieba", [KEYWORD_MAIN], fixture["started_ts"])
        names = [item["name"] for item in result["sheets"]]
        expected_names = [spec["name"] for spec in SHEETS if spec["table"] in fixture["tables"] or spec["table"].startswith("tieba_")]
        check.check("存在的空表也生成 sheet（贴吧 0 行）",
                    "贴吧" in names and "贴吧评论" in names
                    and {item["name"]: item["rows"] for item in result["sheets"]}.get("贴吧") == 0,
                    str(names))
        check.check("补表后 sheet 顺序仍按契约", names == expected_names, str(names))
        check.check("表不存在时跳过不报错（缺失 6 平台以外的表）", len(names) == 14, str(len(names)))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_legacy_schema(check: _Checker) -> None:
    check.section("3) 旧版库兼容：user_id 列（无 creator_hash）+ ai_judge 列")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_legacy_")
    try:
        legacy_db = os.path.join(tmp_dir, "legacy.db")
        conn = sqlite3.connect(legacy_db)
        conn.execute(
            """CREATE TABLE xhs_note (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT, nickname TEXT, add_ts INTEGER, last_modify_ts INTEGER,
                note_id TEXT, type TEXT, title TEXT, desc TEXT, time TEXT,
                note_url TEXT, source_keyword TEXT, ai_judge TEXT
            )"""
        )
        base = int(datetime(2026, 5, 20, 10, 0, 0).timestamp()) + 10
        rows = [
            ("64c12964000000001403e3c3", "核心中枢", base * 1000, base * 1000, "6a03a3f9000000000702e264",
             "normal", "标题A", "正文A", "2026-05-13 06:04:41", "https://example.com/a", "示例科技", "正面"),
            ("64c12964000000001403e3c4", "旧数据", (base - 1000) * 1000, (base - 1000) * 1000,
             "6a03a3f9000000000702e265", "normal", "标题B", "正文B", "2026-05-01 00:00:00",
             "https://example.com/b", "示例科技", "中性"),
        ]
        conn.executemany(
            "INSERT INTO xhs_note (user_id,nickname,add_ts,last_modify_ts,note_id,type,title,desc,time,note_url,source_keyword,ai_judge) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

        result = export_run_excel(legacy_db, os.path.join(tmp_dir, "out"), "run_legacy", [KEYWORD_MAIN], base)
        check.check("旧版 user_id 库可正常导出", result["total_rows"] == 1, str(result))
        workbook = load_workbook(result["file_path"])
        header = [c.value for c in workbook["小红书"][1]]
        check.check("旧库 user_id 列保留（不误加 creator_hash）", "user_id" in header and "creator_hash" not in header,
                    str(header[:6]))
        check.check("旧库额外列 ai_judge 原样输出", "ai_judge" in header)
        check.check("旧库时间字符串被格式化",
                    workbook["小红书"].cell(row=2, column=header.index("time") + 1).value == "2026-05-13 06:04:41",
                    str(workbook["小红书"].cell(row=2, column=header.index("time") + 1).value))
        check.check("旧库 add_ts 过滤生效（早于开始时间的行被排除）", workbook["小红书"].max_row == 2)
        workbook.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_preview(check: _Checker, fixture_db: str, fixture: Dict[str, Any]) -> None:
    check.section("4) preview_run：列 / 行 / limit / 别名")
    preview_result = preview_run(fixture_db, [KEYWORD_MAIN], fixture["started_ts"], limit=5)
    sheets = preview_result.get("sheets", [])
    check.check("preview 返回非空 sheets", len(sheets) > 0, str(len(sheets)))
    expected_non_empty = {name for name, count in fixture["expected"]["sheet_rows"].items() if count > 0}
    check.check("preview 只返回有数据的 sheet",
                {item["name"] for item in sheets} == expected_non_empty,
                f"{sorted(item['name'] for item in sheets)} != {sorted(expected_non_empty)}")
    check.check("preview 每 sheet 行数 <= limit",
                all(len(item["rows"]) <= 5 for item in sheets))
    check.check("preview 行数 = min(limit, 期望行数)",
                all(len(item["rows"]) == min(5, fixture["expected"]["sheet_rows"][item["name"]])
                    for item in sheets))
    check.check("preview 列含 user_id 且不含 creator_hash",
                all("user_id" in item["columns"] and "creator_hash" not in item["columns"] for item in sheets))
    check.check("preview 每 sheet 末尾依次 filtered_reason/sentiment/sentiment_reason",
                all(item["columns"][-3:] == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN]
                    for item in sheets))
    check.check("preview filtered_reason 取值合法",
                all(value in ("", OFF_BRAND)
                    for item in sheets
                    for row in item["rows"]
                    for column, value in zip(item["columns"], row)
                    if column == FILTERED_REASON_COLUMN))
    check.check("preview ID 值为字符串",
                all(isinstance(value, str)
                    for item in sheets
                    for row in item["rows"]
                    for column, value in zip(item["columns"], row)
                    if column.endswith("_id") or column == "id"))
    check.check("preview 可由 JSON 序列化", isinstance(json.dumps(preview_result, ensure_ascii=False), str))
    xhs_sheet = next((item for item in sheets if item["name"] == "小红书"), None)
    if xhs_sheet and "time" in xhs_sheet["columns"]:
        time_values = [row[xhs_sheet["columns"].index("time")] for row in xhs_sheet["rows"]]
        check.check("preview 时间已格式化",
                    all(re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", v) for v in time_values if v),
                    str(time_values[:3]))
    check.check("preview 别名 preview 指向同一实现", preview is preview_run)

    # limit=0 → 无数据 sheet
    preview_zero = preview_run(fixture_db, [KEYWORD_MAIN], fixture["started_ts"], limit=0)
    check.check("preview limit=0 返回空 sheets", preview_zero["sheets"] == [])

    # 毫秒 started_ts 兼容
    preview_ms = preview_run(fixture_db, [KEYWORD_MAIN], fixture["started_ts"] * 1000, limit=5)
    check.check("preview 兼容毫秒 started_ts", preview_ms == preview_result,
                "毫秒/秒 started_ts 结果不一致")

    # 空结果
    check.check("preview 空结果返回空 sheets",
                preview_run(fixture_db, ["不存在的关键词"], fixture["started_ts"])["sheets"] == [])

    # 缺库异常
    try:
        export_run_excel(os.path.join(FIXTURE_DIR, "不存在.db"), FIXTURE_OUT, "x", [KEYWORD_MAIN], 0)
        check.check("缺失数据库应抛 FileNotFoundError", False)
    except FileNotFoundError:
        check.check("缺失数据库应抛 FileNotFoundError", True)


def test_helpers(check: _Checker) -> None:
    check.section("5) 工具函数：关键词归一 / 时间格式化 / 列改名")
    check.check("normalize_keywords 支持 list", normalize_keywords(["a", " b ", "a"]) == ["a", "b"])
    check.check("normalize_keywords 支持字符串", normalize_keywords("示例科技, 舆情") == ["示例科技", "舆情"])
    check.check("format_time 秒时间戳", format_time(int(datetime(2026, 5, 13, 6, 4, 41).timestamp()))
                == "2026-05-13 06:04:41")
    check.check("format_time 毫秒时间戳", format_time(int(datetime(2026, 5, 13, 6, 4, 41).timestamp()) * 1000)
                == "2026-05-13 06:04:41")
    check.check("format_time 字符串", format_time("2026/05/13 06:04:41") == "2026-05-13 06:04:41")
    check.check("format_time Excel 序列号",
                format_time((datetime(2026, 5, 12) - datetime(1899, 12, 30)).days) == "2026-05-12 00:00:00")
    check.check("format_time 空值/占位 0", format_time(None) == "" and format_time(0) == "" and format_time("0") == "")
    check.check("output_columns creator_hash → user_id",
                output_columns(["id", "creator_hash", "nickname"]) == [("id", "id"), ("user_id", "creator_hash"),
                                                                       ("nickname", "nickname")])
    check.check("output_columns 旧库 user_id 保留",
                output_columns(["id", "user_id", "nickname"]) == [("id", "id"), ("user_id", "user_id"),
                                                                  ("nickname", "nickname")])
    check.check("output_columns 双列共存时去重",
                output_columns(["id", "creator_hash", "user_id", "nickname"]) ==
                [("id", "id"), ("user_id", "creator_hash"), ("nickname", "nickname")])


# ---------------------------------------------------------------------------
# 6/7/8：last_modify_ts 过滤修复的回归测试（2026-11-09）
# ---------------------------------------------------------------------------
def _insert_rows(conn: sqlite3.Connection, table: str, columns: Sequence[str],
                 rows: Sequence[Sequence[Any]]) -> None:
    sql = f'INSERT INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in columns)}) ' \
          f'VALUES ({",".join(["?"] * len(columns))})'
    conn.executemany(sql, rows)


def test_last_modify_filter(check: _Checker) -> None:
    """核心回归：add_ts 旧、last_modify_ts 新（本次运行更新）必须导出。"""
    check.section("6) 本次运行 last_modify_ts 过滤（修复：旧 add_ts 不再决定导出）")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_last_modify_")
    try:
        db_path = os.path.join(tmp_dir, "modify.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            for table in ("xhs_note", "xhs_note_comment"):
                _create_table(conn, table, ORM_COLUMNS[table])
            base = int(datetime(2026, 11, 9, 10, 0, 0).timestamp())  # 模拟 run_e513 附近
            started_ts = base
            old_ms = (base - 2 * 86400) * 1000        # 11-07 首次入库（历史）
            new_ms = (base + 55) * 1000               # 11-09 本次运行更新
            note_cols = ["creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id",
                         "type", "title", "desc", "time", "note_url", "source_keyword"]
            notes = [
                # (note_id, add_ts, last_modify_ts, 说明)
                ("note_A", old_ms, new_ms),           # 旧首次入库 + 本次更新 → 必须导出
                ("note_B", new_ms, old_ms),           # add 新但 last_modify 旧（历史）→ 不导出
                ("note_C", old_ms, old_ms),           # 历史未更新 → 不导出
                ("note_D", old_ms, 0),                # last_modify=0 → 回退 add_ts（旧）→ 不导出
                ("note_E", new_ms, 0),                # last_modify=0 → 回退 add_ts（新）→ 导出
            ]
            _insert_rows(conn, "xhs_note", note_cols, [
                ("creator_x", "作者", add_ts, last_modify_ts, note_id, "normal", note_id, "", "", "", KEYWORD_MAIN)
                for note_id, add_ts, last_modify_ts in notes
            ])
            comment_cols = ["creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id",
                            "create_time", "note_id", "content"]
            comments = [
                # (comment_id, parent_note, add_ts, last_modify_ts)
                ("c_a1", "note_A", old_ms, new_ms),   # 父 A（本次更新）+ 评论本次更新 → 导出
                ("c_a2", "note_A", old_ms, old_ms),   # 评论未更新 → 不导出
                ("c_a3", "note_A", new_ms, 0),        # last_modify=0 → 回退 add_ts（新）→ 导出
                ("c_a4", "note_A", old_ms, 0),        # last_modify=0 → 回退 add_ts（旧）→ 不导出
                ("c_b1", "note_B", new_ms, new_ms),   # 父 B 不在本次更新内容集合 → 不导出
            ]
            _insert_rows(conn, "xhs_note_comment", comment_cols, [
                ("creator_c", "评论者", add_ts, last_modify_ts, comment_id, 0, parent, f"评论{comment_id}")
                for comment_id, parent, add_ts, last_modify_ts in comments
            ])
            conn.commit()
        finally:
            conn.close()

        result = export_run_excel(db_path, os.path.join(tmp_dir, "out"), "run_last_modify", [KEYWORD_MAIN], started_ts)
        rows = {item["name"]: item["rows"] for item in result["sheets"]}
        check.check("旧 add_ts + 新 last_modify 的内容导出（note_A/note_E）",
                    rows.get("小红书") == 2, str(rows))
        check.check("旧 add_ts + 新 last_modify 的评论导出（c_a1/c_a3）",
                    rows.get("小红书评论") == 2, str(rows))
        check.check("total_rows = 2 内容 + 2 评论", result["total_rows"] == 4, str(result["total_rows"]))

        workbook = load_workbook(result["file_path"])
        try:
            xhs_header = [c.value for c in workbook["小红书"][1]]
            note_ids = {workbook["小红书"].cell(row=r, column=xhs_header.index("note_id") + 1).value
                        for r in range(2, workbook["小红书"].max_row + 1)}
            check.check("导出内容集合 = {note_A, note_E}", note_ids == {"note_A", "note_E"}, str(note_ids))
            check.check("add_ts 新但 last_modify 旧的历史内容不导出（note_B）", "note_B" not in note_ids)
            check.check("历史未更新内容不导出（note_C/note_D）",
                        "note_C" not in note_ids and "note_D" not in note_ids)
            comment_header = [c.value for c in workbook["小红书评论"][1]]
            comment_ids = {workbook["小红书评论"].cell(row=r, column=comment_header.index("comment_id") + 1).value
                           for r in range(2, workbook["小红书评论"].max_row + 1)}
            check.check("评论集合 = {c_a1, c_a3}", comment_ids == {"c_a1", "c_a3"}, str(comment_ids))
            check.check("父内容未命中的评论不导出（c_b1）", "c_b1" not in comment_ids)
            overview = _parse_overview(workbook["概览"])
            check.check("概览过滤口径说明包含 last_modify_ts",
                        "last_modify_ts" in str(overview["meta"].get("过滤口径", "")))
        finally:
            workbook.close()

        # preview 与 export 同口径
        prev = preview_run(db_path, [KEYWORD_MAIN], started_ts)
        prev_rows = {item["name"]: len(item["rows"]) for item in prev["sheets"]}
        check.check("preview 与 export 过滤口径一致", prev_rows.get("小红书") == 2 and prev_rows.get("小红书评论") == 2,
                    str(prev_rows))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_legacy_missing_last_modify(check: _Checker) -> None:
    """旧库没有 last_modify_ts 列 → 回退 add_ts 仍正常。"""
    check.section("7) 旧库缺少 last_modify_ts 列 → 回退 add_ts")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_legacy_no_lm_")
    try:
        db_path = os.path.join(tmp_dir, "legacy_no_lm.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """CREATE TABLE xhs_note (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT, nickname TEXT, add_ts INTEGER,
                    note_id TEXT, source_keyword TEXT
                )"""
            )
            base = int(datetime(2026, 11, 9, 10, 0, 0).timestamp())
            _insert_rows(conn, "xhs_note", ["user_id", "nickname", "add_ts", "note_id", "source_keyword"], [
                ("u_new", "新数据", (base + 10) * 1000, "note_new", KEYWORD_MAIN),
                ("u_old", "旧数据", (base - 86400) * 1000, "note_old", KEYWORD_MAIN),
            ])
            conn.commit()
        finally:
            conn.close()

        result = export_run_excel(db_path, os.path.join(tmp_dir, "out"), "run_legacy_no_lm", [KEYWORD_MAIN], base)
        check.check("缺 last_modify_ts 列时回退 add_ts 正常导出", result["total_rows"] == 1, str(result))
        workbook = load_workbook(result["file_path"])
        try:
            header = [c.value for c in workbook["小红书"][1]]
            check.check("缺列旧库导出表头保留 user_id", "user_id" in header and "last_modify_ts" not in header)
            check.check("回退 add_ts 后仅导出本次新增行",
                        workbook["小红书"].cell(row=2, column=header.index("note_id") + 1).value == "note_new")
        finally:
            workbook.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _snapshot_media_db(dest_path: str, started_ms: int, finished_ms: int) -> bool:
    """
    把真实 app/data media_crawler.db 快照到临时文件，并删除 finished_ms 之后更新的行，
    以模拟“某个 run 刚结束时”的库状态（真实库会持续累积后续 run 的数据）。
    只读源库（backup），不修改 app/data。
    """
    if not os.path.isfile(REAL_APP_DB):
        return False
    source = open_readonly(REAL_APP_DB)
    target = sqlite3.connect(dest_path)
    try:
        source.backup(target)
        tables = [row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
            if "last_modify_ts" in columns:
                target.execute(
                    f'DELETE FROM "{table}" WHERE CAST(last_modify_ts AS INTEGER) > ?', (finished_ms,)
                )
        target.commit()
    finally:
        target.close()
        source.close()
    return True


def test_real_db_regression(check: _Checker) -> None:
    """真实 app/data/crawler/media_crawler.db 回归（只读快照，不修改用户数据）。"""
    check.section("8) 真实 app/data 库回归：run_e51326326139（UI saved=100）")
    if not os.path.isfile(REAL_APP_DB):
        check.check("真实库回归跳过（DB 不存在）", True, REAL_APP_DB)
        print(f"  [SKIP] 真实库不存在：{REAL_APP_DB}")
        return
    started_ms = int(RUN_E513["started_ts"]) * 1000
    finished_ms = int(RUN_E513.get("finished_ts", RUN_E513["started_ts"] + 1000)) * 1000
    tmp_dir = tempfile.mkdtemp(prefix="exporter_real_e513_")
    snapshot_db = os.path.join(tmp_dir, "media_crawler_run_e513.db")
    try:
        _snapshot_media_db(snapshot_db, started_ms, finished_ms)
        connection = open_readonly(snapshot_db)
        try:
            expected_notes = connection.execute(
                "SELECT COUNT(*) FROM xhs_note WHERE CAST(last_modify_ts AS INTEGER) >= ?", (started_ms,)
            ).fetchone()[0]
            expected_comments = connection.execute(
                "SELECT COUNT(*) FROM xhs_note_comment WHERE CAST(last_modify_ts AS INTEGER) >= ?", (started_ms,)
            ).fetchone()[0]
            old_note_row = connection.execute(
                "SELECT note_id FROM xhs_note WHERE CAST(last_modify_ts AS INTEGER) < ? LIMIT 1", (started_ms,)
            ).fetchone()
            exported_old_add_row = connection.execute(
                "SELECT note_id FROM xhs_note WHERE CAST(add_ts AS INTEGER) < ? "
                "AND CAST(last_modify_ts AS INTEGER) >= ? LIMIT 1",
                (started_ms, started_ms),
            ).fetchone()
            note_last_modify = {str(row["note_id"]): int(row["last_modify_ts"])
                                for row in connection.execute("SELECT note_id, last_modify_ts FROM xhs_note")}
            expected_comment_count = connection.execute(
                "SELECT COUNT(*) FROM xhs_note_comment WHERE CAST(last_modify_ts AS INTEGER) >= ?", (started_ms,)
            ).fetchone()[0]
        finally:
            connection.close()

        if (expected_notes, expected_comments) != (RUN_E513["expected_notes"], RUN_E513["expected_comments"]):
            check.check("真实库 run_e513 数据与实测描述一致（20 笔记 / 80 评论）", True,
                        f"当前库已变化：{expected_notes}/{expected_comments}（跳过精确断言）")
            print(f"  [SKIP] run_e513 快照数据已变化：last_modify 命中 {expected_notes} 笔记 / "
                  f"{expected_comments} 评论；跳过精确断言")
            return

        check.check("真实库快照：run_e513 本次更新 xhs_note 20 行",
                    expected_notes == 20, str(expected_notes))
        check.check("真实库快照：run_e513 本次更新 xhs_note_comment 80 行",
                    expected_comments == 80, str(expected_comments))

        result = export_run_excel(
            snapshot_db, os.path.join(tmp_dir, "out"), RUN_E513["run_id"],
            RUN_E513["keywords"], RUN_E513["started_ts"],
        )
        rows = {item["name"]: item["rows"] for item in result["sheets"]}
        check.check("真实库导出 小红书 = 20 行", rows.get("小红书") == 20, str(rows.get("小红书")))
        check.check("真实库导出 小红书评论 = 80 行", rows.get("小红书评论") == 80, str(rows.get("小红书评论")))
        check.check("真实库导出 total_rows = 100（与 UI saved 一致）", result["total_rows"] == 100,
                    str(result["total_rows"]))
        print("  [真实回归 run_e513] 各 sheet 行数："
              + json.dumps({k: v for k, v in rows.items() if v > 0}, ensure_ascii=False)
              + f"；total_rows={result['total_rows']}")

        workbook = load_workbook(result["file_path"])
        try:
            xhs_header = [c.value for c in workbook["小红书"][1]]
            exported_note_ids = [workbook["小红书"].cell(row=r, column=xhs_header.index("note_id") + 1).value
                                 for r in range(2, workbook["小红书"].max_row + 1)]
            exported_note_id_set = {str(x) for x in exported_note_ids}
            check.check("导出内容均为本次更新（last_modify_ts >= 开始时间）",
                        all(note_last_modify.get(note_id, 0) >= started_ms for note_id in exported_note_id_set),
                        str(exported_note_ids[:3]))
            if old_note_row is not None:
                old_note_id = str(old_note_row["note_id"])
                check.check("历史未更新笔记未出现在 Excel 中", old_note_id not in exported_note_id_set,
                            old_note_id)
            if exported_old_add_row is not None:
                check.check("旧 add_ts 但本次 last_modify 更新的笔记已导出（修复生效）",
                            str(exported_old_add_row["note_id"]) in exported_note_id_set,
                            str(exported_old_add_row["note_id"]))
            comment_header = [c.value for c in workbook["小红书评论"][1]]
            exported_comment_parents = [str(workbook["小红书评论"].cell(
                row=r, column=comment_header.index("note_id") + 1).value)
                for r in range(2, workbook["小红书评论"].max_row + 1)]
            check.check("评论父内容均在本轮导出内容集合内",
                        set(exported_comment_parents).issubset(exported_note_id_set))
            check.check("导出评论数 = DB 中本次更新评论数", len(exported_comment_parents) == expected_comment_count,
                        str(len(exported_comment_parents)))
        finally:
            workbook.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 9/10/11：filtered_reason 相关性标记（2026-11-09 新增，防平台搜索泛内容污染）
# ---------------------------------------------------------------------------
def _insert_orm_rows(conn: sqlite3.Connection, table: str, rows: Sequence[Dict[str, Any]]) -> None:
    """按 ORM 列插入行（缺失列填 None）。"""
    columns = ORM_COLUMNS[table]
    insert_columns = [column for column in columns if column != "id"]
    values = []
    for row in rows:
        merged = {column: None for column in columns}
        merged.update(row)
        values.append([merged[column] for column in insert_columns])
    sql = f'INSERT INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in insert_columns)}) ' \
          f'VALUES ({",".join(["?"] * len(insert_columns))})'
    conn.executemany(sql, values)


def test_relevance_helpers(check: _Checker) -> None:
    check.section("9) 相关性判定工具（token 拆分 / 命中范围 / 大小写 / 无关键词）")
    check.check("keyword_tokens 逗号/空格/顿号/分号拆分",
                keyword_tokens("示例科技, 舆情监测、招聘；mpo") == ["示例科技", "舆情监测", "招聘", "mpo"])
    check.check("keyword_tokens None/空/全分隔符",
                keyword_tokens(None) == [] and keyword_tokens("") == [] and keyword_tokens(" ,、 ") == [])
    cases = [
        ("标题命中", {"source_keyword": "示例科技", "title": "示例科技发布会"}, ""),
        ("desc 命中", {"source_keyword": "示例科技", "desc": "关于示例科技的最新进展"}, ""),
        ("昵称命中", {"source_keyword": "示例科技", "nickname": "示例科技官方"}, ""),
        ("正文 content 命中", {"source_keyword": "示例科技", "content": "今天参观示例科技"}, ""),
        ("知乎 content_text 命中", {"source_keyword": "示例科技", "content_text": "示例科技问答"}, ""),
        ("贴吧 user_nickname 命中", {"source_keyword": "示例科技", "user_nickname": "示例科技贴吧"}, ""),
        ("多 token 命中其一", {"source_keyword": "示例科技,舆情监测", "title": "舆情监测周报"}, ""),
        ("英文大小写不敏感", {"source_keyword": "MPO", "title": "mpo connector 分析"}, ""),
        ("未命中 → off_brand", {"source_keyword": "示例科技", "title": "行业新闻", "nickname": "某媒体"},
         OFF_BRAND),
        ("source_keyword 为空 → off_brand", {"source_keyword": "", "title": "示例科技"}, OFF_BRAND),
        ("正文为空 → off_brand", {"source_keyword": "示例科技"}, OFF_BRAND),
    ]
    for name, row, expected in cases:
        actual = content_filtered_reason(row)
        check.check(f"content_filtered_reason：{name}", actual == expected, f"{actual!r}")


def test_relevance_export(check: _Checker) -> None:
    """导出级回归：filtered_reason 列、评论继承、概览统计、不删除行、列一致性。"""
    check.section("10) 导出 filtered_reason 列 / 评论继承 / 概览统计 / 不删除行")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_relevance_")
    try:
        db_path = os.path.join(tmp_dir, "relevance.db")
        conn = sqlite3.connect(db_path)
        try:
            for table in ("xhs_note", "xhs_note_comment", "kuaishou_video", "bilibili_video"):
                _create_table(conn, table, ORM_COLUMNS[table])
            base = int(datetime(2026, 11, 9, 14, 0, 0).timestamp())
            old_ts = (base - 3600) * 1000
            new_ts = (base + 60) * 1000

            def note(note_id: str, title: str, desc: str, source_keyword: str,
                     nickname: str = "某媒体", content: Optional[str] = None) -> Dict[str, Any]:
                row = {"creator_hash": "creator_x", "nickname": nickname, "add_ts": old_ts,
                       "last_modify_ts": new_ts, "note_id": note_id, "title": title, "desc": desc,
                       "source_keyword": source_keyword}
                if content is not None:
                    row["content"] = content
                return row

            _insert_orm_rows(conn, "xhs_note", [
                note("note_title", "示例科技发布会", "发布会正文", "示例科技"),
                note("note_desc", "行业动态", "关于示例科技的最新进展", "示例科技"),
                note("note_nickname", "行业动态", "无品牌词的正文", "示例科技", nickname="示例科技官方"),
                note("note_off", "科技行业新闻", "只是泛行业内容", "示例科技"),
            ])
            _insert_orm_rows(conn, "xhs_note_comment", [
                {"creator_hash": "c", "nickname": "评论者", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "comment_id": "cmt_rel", "note_id": "note_title", "content": "支持"},
                {"creator_hash": "c", "nickname": "评论者", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "comment_id": "cmt_off", "note_id": "note_off", "content": "路过"},
            ])
            _insert_orm_rows(conn, "kuaishou_video", [
                {"creator_hash": "k", "nickname": "00后女飞手", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "video_id": "ks_off_1", "title": "无人机推荐", "desc": "航拍风景", "source_keyword": "示例科技"},
                {"creator_hash": "k", "nickname": "某up", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "video_id": "ks_off_2", "title": "磁浮飞行器 尖端科技", "desc": "低空经济",
                 "source_keyword": "示例科技"},
            ])
            _insert_orm_rows(conn, "bilibili_video", [
                {"creator_hash": "b", "nickname": "示例科技", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "video_id": "bili_rel", "title": "示例科技宣传片", "desc": "", "source_keyword": "示例科技"},
                {"creator_hash": "b", "nickname": "光模块观察", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "video_id": "bili_mpo", "title": "mpo connector 分析", "desc": "", "source_keyword": "MPO"},
            ])
            conn.commit()
        finally:
            conn.close()

        keywords = ["示例科技", "MPO"]
        result = export_run_excel(db_path, os.path.join(tmp_dir, "out"), "run_relevance", keywords, base)
        rows = {item["name"]: item["rows"] for item in result["sheets"]}
        check.check("相关性标记不删除行（导出总行数不变）", result["total_rows"] == 10, str(rows))
        check.check("小红书导出 4 行（3 条相关 + 1 条 off_brand）", rows.get("小红书") == 4, str(rows))
        check.check("快手导出 2 行（模拟泛内容）", rows.get("快手") == 2, str(rows))
        check.check("B站导出 2 行（含大小写 token 命中）", rows.get("B站") == 2, str(rows))
        check.check("小红书评论导出 2 行（父内容 1 相关 1 off_brand）", rows.get("小红书评论") == 2, str(rows))

        workbook = load_workbook(result["file_path"])
        try:
            # 各 sheet 列一致性 + 末尾 filtered_reason / sentiment / sentiment_reason
            for name in workbook.sheetnames:
                if name == "概览":
                    continue
                ws = workbook[name]
                header = [c.value for c in ws[1]]
                check.check(f"{name} 末尾三列 = filtered_reason/sentiment/sentiment_reason",
                            header[-3:] == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN],
                            str(header[-3:]))
            xhs_header = [c.value for c in workbook["小红书"][1]]
            xhs_rows = [dict(zip(xhs_header, r)) for r in workbook["小红书"].iter_rows(min_row=2, values_only=True)]
            reason_by_note = {row["note_id"]: (row[FILTERED_REASON_COLUMN] or "") for row in xhs_rows}
            check.check("标题/desc/昵称命中均标记相关",
                        all(reason_by_note.get(note_id) == ""
                            for note_id in ("note_title", "note_desc", "note_nickname")))
            check.check("无命中标记 off_brand（note_off）", reason_by_note.get("note_off") == OFF_BRAND)
            check.check("filtered_reason 单元格为文本（空相关值读取为 None/空串）",
                        all(value is None or isinstance(value, str) for value in reason_by_note.values()))
            check.check("filtered_reason 取值仅 ''/off_brand",
                        set(reason_by_note.values()) <= {"", OFF_BRAND}, str(set(reason_by_note.values())))

            comment_header = [c.value for c in workbook["小红书评论"][1]]
            comment_rows = [dict(zip(comment_header, r))
                            for r in workbook["小红书评论"].iter_rows(min_row=2, values_only=True)]
            comment_reason = {row["comment_id"]: (row[FILTERED_REASON_COLUMN] or "") for row in comment_rows}
            check.check("评论按父内容继承 reason（相关父 → 空）", comment_reason.get("cmt_rel") == "")
            check.check("评论按父内容继承 reason（off_brand 父 → off_brand）",
                        comment_reason.get("cmt_off") == OFF_BRAND)

            ks_header = [c.value for c in workbook["快手"][1]]
            ks_rows = [dict(zip(ks_header, r)) for r in workbook["快手"].iter_rows(min_row=2, values_only=True)]
            check.check("快手泛内容全部 off_brand",
                        all((row[FILTERED_REASON_COLUMN] or "") == OFF_BRAND for row in ks_rows))

            bili_header = [c.value for c in workbook["B站"][1]]
            bili_rows = [dict(zip(bili_header, r)) for r in workbook["B站"].iter_rows(min_row=2, values_only=True)]
            check.check("B站含品牌词内容相关、英文 token 大小写命中",
                        all((row[FILTERED_REASON_COLUMN] or "") == "" for row in bili_rows))

            overview = _parse_overview(workbook["概览"])
            meta = overview["meta"]
            check.check("概览相关行数 = 6", int(meta.get("相关行数", -1)) == 6, str(meta.get("相关行数")))
            check.check("概览 off_brand 行数 = 4", int(meta.get("off_brand 行数", -1)) == 4,
                        str(meta.get("off_brand 行数")))
            check.check("概览相关性判定规则可读",
                        "source_keyword" in str(meta.get("相关性判定规则", ""))
                        and "off_brand" in str(meta.get("相关性判定规则", "")))
        finally:
            workbook.close()

        prev = preview_run(db_path, keywords, base)
        prev_map = {item["name"]: item for item in prev["sheets"]}
        check.check("preview 列末尾为 filtered_reason/sentiment/sentiment_reason",
                    all(item["columns"][-3:] == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN,
                                                 SENTIMENT_REASON_COLUMN]
                        for item in prev["sheets"]))
        check.check("preview 标记与 export 一致",
                    all(value == OFF_BRAND for item in prev["sheets"] if item["name"] == "快手"
                        for column, value in zip(item["columns"], item["rows"][0])
                        if column == FILTERED_REASON_COLUMN))
        check.check("preview 快手 2 行", len(prev_map.get("快手", {}).get("rows", [])) == 2)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_relevance_existing_column(check: _Checker) -> None:
    """源库已有 filtered_reason 列时：不重复加列、非空值沿用、空值补算。"""
    check.section("10b) 源库已有 filtered_reason 列 → 沿用且不重复")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_relevance_existing_")
    try:
        db_path = os.path.join(tmp_dir, "existing_reason.db")
        conn = sqlite3.connect(db_path)
        try:
            columns = ORM_COLUMNS["xhs_note"] + [FILTERED_REASON_COLUMN]
            _create_table(conn, "xhs_note", columns)
            base = int(datetime(2026, 11, 9, 15, 0, 0).timestamp())
            insert_columns = [column for column in columns if column != "id"]
            rows = [
                # (note_id, title, source_keyword, 已有 filtered_reason)
                ("note_compute", "示例科技发布会", "示例科技", None),
                ("note_off", "泛行业内容", "示例科技", None),
                ("note_manual", "泛行业内容", "示例科技", "manual_note"),
                ("note_forced", "示例科技发布会", "示例科技", "off_brand"),
            ]
            values = []
            for note_id, title, source_keyword, existing_reason in rows:
                merged = {column: None for column in columns}
                merged.update({
                    "creator_hash": "u", "nickname": "作者", "add_ts": (base - 1000) * 1000,
                    "last_modify_ts": (base + 10) * 1000, "note_id": note_id, "title": title,
                    "source_keyword": source_keyword, FILTERED_REASON_COLUMN: existing_reason,
                })
                values.append([merged[column] for column in insert_columns])
            conn.executemany(
                f'INSERT INTO "xhs_note" ({",".join(chr(34)+c+chr(34) for c in insert_columns)}) '
                f'VALUES ({",".join(["?"] * len(insert_columns))})', values)
            conn.commit()
        finally:
            conn.close()

        result = export_run_excel(db_path, os.path.join(tmp_dir, "out"), "run_existing_reason",
                                  ["示例科技"], base)
        workbook = load_workbook(result["file_path"])
        try:
            header = [c.value for c in workbook["小红书"][1]]
            check.check("源库已有 filtered_reason 列时不重复", header.count(FILTERED_REASON_COLUMN) == 1,
                        str(header.count(FILTERED_REASON_COLUMN)))
            check.check("源库已有 filtered_reason 列时仍保持末尾三列顺序",
                        header[-3:] == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN],
                        str(header[-3:]))
            reason_index = header.index(FILTERED_REASON_COLUMN) + 1
            note_index = header.index("note_id") + 1
            reason_by_note = {
                workbook["小红书"].cell(row=r, column=note_index).value:
                (workbook["小红书"].cell(row=r, column=reason_index).value or "")
                for r in range(2, workbook["小红书"].max_row + 1)
            }
            check.check("空值行按规则补算（相关 → 空）", reason_by_note.get("note_compute") == "")
            check.check("空值行按规则补算（未命中 → off_brand）",
                        reason_by_note.get("note_off") == OFF_BRAND)
            check.check("非空原值沿用（manual_note）", reason_by_note.get("note_manual") == "manual_note")
            check.check("非空原值沿用（off_brand）", reason_by_note.get("note_forced") == OFF_BRAND)
            overview = _parse_overview(workbook["概览"])
            meta = overview["meta"]
            check.check("概览 off_brand 统计按最终输出值计算",
                        int(meta.get("off_brand 行数", -1)) == 2, str(meta.get("off_brand 行数")))
            check.check("概览相关行数按最终输出值计算",
                        int(meta.get("相关行数", -1)) == 2, str(meta.get("相关行数")))
        finally:
            workbook.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_real_db_run_259(check: _Checker) -> None:
    """真实库 run_259b84c9079d：快手 20 行全部 off_brand；B站/知乎全部命中；微博/贴吧部分命中。"""
    check.section("11) 真实库 run_259b84c9079d：快手全 off_brand，B站/知乎多数命中")
    if not os.path.isfile(REAL_APP_DB):
        check.check("真实库 run_259 回归跳过（DB 不存在）", True, REAL_APP_DB)
        print(f"  [SKIP] 真实库不存在：{REAL_APP_DB}")
        return
    started_ms = int(RUN_259["started_ts"]) * 1000
    finished_ms = int(RUN_259["finished_ts"]) * 1000
    content_tables = {
        "小红书": "xhs_note", "抖音": "douyin_aweme", "快手": "kuaishou_video",
        "B站": "bilibili_video", "微博": "weibo_note", "贴吧": "tieba_note", "知乎": "zhihu_content",
    }
    expected_content_count = {"小红书": 0, "抖音": 0, "快手": 20, "B站": 20, "微博": 15, "贴吧": 10, "知乎": 9}
    expected_content_off = {"快手": 20, "B站": 0, "微博": 1, "贴吧": 3, "知乎": 0}

    tmp_dir = tempfile.mkdtemp(prefix="exporter_relevance_259_")
    snapshot_db = os.path.join(tmp_dir, "media_crawler_run_259.db")
    try:
        _snapshot_media_db(snapshot_db, started_ms, finished_ms)
        connection = open_readonly(snapshot_db)
        try:
            updated_counts: Dict[str, int] = {}
            updated_off: Dict[str, int] = {}
            db_wide: Dict[str, Tuple[int, int]] = {}
            for name, table in content_tables.items():
                table_columns = [row["name"] for row in connection.execute(f'PRAGMA table_info("{table}")')]
                if not table_columns:
                    updated_counts[name] = 0
                    continue
                rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]
                updated = [row for row in rows
                           if row.get("last_modify_ts") is not None
                           and int(row["last_modify_ts"]) >= started_ms]
                updated_counts[name] = len(updated)
                updated_off[name] = sum(1 for row in updated if content_filtered_reason(row) == OFF_BRAND)
                db_off = sum(1 for row in rows if content_filtered_reason(row) == OFF_BRAND)
                db_wide[name] = (len(rows), db_off)
        finally:
            connection.close()

        # DB 数据若已变化则跳过精确断言（避免测试因用户继续采集而变脆）
        if (any(updated_counts.get(name) != count for name, count in expected_content_count.items())
                or any(updated_off.get(name) != count for name, count in expected_content_off.items())):
            check.check("真实库 run_259 数据与预期一致（快手20全off，B站20/知乎9全命中）", True,
                        f"当前库已变化：counts={updated_counts}, off={updated_off}（跳过精确断言）")
            print(f"  [SKIP] 真实库数据已变化：counts={updated_counts}；off={updated_off}")
            return

        check.check("run_259 本期更新快手 20 条内容", updated_counts.get("快手") == 20)
        check.check("run_259 快手 20 条全部 off_brand（泛内容）", updated_off.get("快手") == 20)
        check.check("run_259 B站 20 条全部命中", updated_off.get("B站") == 0)
        check.check("run_259 知乎 9 条全部命中", updated_off.get("知乎") == 0)
        check.check("run_259 微博 15 条中 1 条 off_brand", updated_off.get("微博") == 1)
        check.check("run_259 贴吧 10 条中 3 条 off_brand", updated_off.get("贴吧") == 3)

        # DB 全量：xhs/bili/知乎 多数命中（题目要求“xhs/bili/知乎多数命中”）
        for name in ("小红书", "B站", "知乎"):
            total, off = db_wide.get(name, (0, 0))
            check.check(f"DB 全量 {name} 多数命中（相关 >= off_brand）", total == 0 or (total - off) >= off,
                        f"total={total}, off={off}")

        result = export_run_excel(snapshot_db, tmp_dir, RUN_259["run_id"], RUN_259["keywords"],
                                  RUN_259["started_ts"])
        rows = {item["name"]: item["rows"] for item in result["sheets"]}
        expected_total = sum(RUN_259["expected_sheet_rows"].values())
        check.check("run_259 导出总行数 = 422（与 run saved 合计一致）",
                    result["total_rows"] == expected_total, str(result["total_rows"]))
        for name, count in RUN_259["expected_sheet_rows"].items():
            check.check(f"run_259 导出 {name} = {count} 行", rows.get(name) == count, str(rows.get(name)))
        check.check("run_259 未采集平台 xhs/douyin 本期 0 行",
                    rows.get("小红书", None) == 0 and rows.get("抖音", None) == 0,
                    f"xhs={rows.get('小红书')}, dy={rows.get('抖音')}")

        workbook = load_workbook(result["file_path"])
        try:
            off_by_sheet: Dict[str, int] = {}
            for name, expected_off in RUN_259["expected_off_brand"].items():
                ws = workbook[name]
                header = [c.value for c in ws[1]]
                reason_index = header.index(FILTERED_REASON_COLUMN) + 1
                values = [ws.cell(row=r, column=reason_index).value for r in range(2, ws.max_row + 1)]
                off_count = sum(1 for value in values if value == OFF_BRAND)
                off_by_sheet[name] = off_count
                check.check(f"run_259 {name} off_brand = {expected_off}", off_count == expected_off,
                            f"actual={off_count}")
            # 评论按父内容继承
            parent_columns = {"快手评论": ("video_id", "快手", "video_id"),
                              "B站评论": ("video_id", "B站", "video_id"),
                              "微博评论": ("note_id", "微博", "note_id"),
                              "贴吧评论": ("note_id", "贴吧", "note_id"),
                              "知乎评论": ("content_id", "知乎", "content_id")}
            for comment_sheet, (parent_col, content_sheet, content_id_col) in parent_columns.items():
                ws = workbook[content_sheet]
                header = [c.value for c in ws[1]]
                id_idx = header.index(content_id_col) + 1
                reason_idx = header.index(FILTERED_REASON_COLUMN) + 1
                parent_reason = {str(ws.cell(row=r, column=id_idx).value):
                                 ws.cell(row=r, column=reason_idx).value
                                 for r in range(2, ws.max_row + 1)}
                cws = workbook[comment_sheet]
                cheader = [c.value for c in cws[1]]
                c_parent_idx = cheader.index(parent_col) + 1
                c_reason_idx = cheader.index(FILTERED_REASON_COLUMN) + 1
                mismatches = 0
                for r in range(2, cws.max_row + 1):
                    parent = str(cws.cell(row=r, column=c_parent_idx).value)
                    reason = cws.cell(row=r, column=c_reason_idx).value
                    if str(parent_reason.get(parent) or "") != (reason or ""):
                        mismatches += 1
                check.check(f"run_259 {comment_sheet} filtered_reason 与父内容一致", mismatches == 0,
                            f"mismatches={mismatches}")
        finally:
            workbook.close()

        total_off = sum(off_by_sheet.values())
        total_rows = result["total_rows"]
        check.check("run_259 结构统计：相关+off_brand = total_rows",
                    total_off <= total_rows, f"off={total_off}, total={total_rows}")
        print(f"  [真实回归 run_259] 各 sheet 行数/off_brand："
              + json.dumps({name: {"rows": rows.get(name), "off_brand": off_by_sheet.get(name)}
                            for name in sorted(set(list(rows.keys()) + list(off_by_sheet.keys()))) if rows.get(name)},
                           ensure_ascii=False))
        print(f"  [真实回归 run_259] total_rows={total_rows}，off_brand={total_off}，相关={total_rows - total_off}")
        print(f"  [DB 全量相关性] " + json.dumps(
            {name: {"total": total, "off_brand": off} for name, (total, off) in db_wide.items() if total},
            ensure_ascii=False))

        # 样例文件留档（test 产物，不覆盖用户 app/data 文件）
        regression_dir = os.path.join(FIXTURE_DIR, "regression")
        os.makedirs(regression_dir, exist_ok=True)
        sample_path = os.path.join(regression_dir, "run_259b84c9079d_相关性标记导出.xlsx")
        shutil.copyfile(result["file_path"], sample_path)
        check.check("run_259 样例文件已留档", os.path.isfile(sample_path), sample_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 12/13/14：Excel 安全文本清洗（非法控制字符 / 公式注入 / 超长 / 真实库回归）
# ---------------------------------------------------------------------------
_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def test_excel_sanitize_helpers(check: _Checker) -> None:
    check.section("12) Excel 安全清洗工具（控制字符 / 公式 / 超长）")
    # 注意：本测试文件里另有 fixture 用的同名 _to_text 辅助函数，这里用别名导入 exporter 的内部入口
    from exporter import _to_cell_value as export_to_cell_value
    from exporter import _to_json_value as export_to_json_value
    from exporter import _to_text as export_to_text

    illegal_codes = [code for code in range(0x20) if code not in (0x09, 0x0A, 0x0D)] + [0x7F]
    check.check("非法控制字符（与 openpyxl 一致 + 0x7F）统一替换为空格",
                all(sanitize_excel_text("a" + chr(code) + "b") == "a b" for code in illegal_codes),
                str([hex(code) for code in illegal_codes if sanitize_excel_text("a" + chr(code) + "b") != "a b"]))
    check.check("保留 \\t/\\n/\\r（openpyxl 允许）",
                all(sanitize_excel_text("a" + chr(code) + "b") == "a" + chr(code) + "b"
                    for code in (0x09, 0x0A, 0x0D)))
    check.check("emoji / 中文原样保留", sanitize_excel_text("😀示例科技") == "😀示例科技")
    check.check("_to_text 清洗 ID 文本", export_to_text("id\x18x") == "id x", repr(export_to_text("id\x18x")))
    check.check("_to_cell_value 清洗普通文本", export_to_cell_value("正文\x00保留") == "正文 保留")
    check.check("_to_json_value 清洗预览值", export_to_json_value("预览\x0b值") == "预览 值")

    long_raw = "A" * 31000 + "\x18" + "B" * 2000
    truncated = export_to_cell_value(long_raw)
    check.check("先清洗再截断：长度 = 32000", len(truncated) == 32000, str(len(truncated)))
    check.check("截断后不含非法字符", _ILLEGAL_RE.search(truncated) is None)
    check.check("截断保留前部内容（非法字符已替换为空格）",
                truncated.startswith("A" * 31000 + " ") and truncated.endswith("B"))
    check.check("_to_cell_value 保留 emoji/换行", export_to_cell_value("😀\n示例") == "😀\n示例")
    check.check("None 值处理", export_to_cell_value(None) == "" and export_to_text(None) == "")


def _scan_workbook_safety(check: _Checker, workbook, label: str) -> None:
    """扫描整个工作簿：无非法字符、无公式单元格。"""
    illegal_hits = 0
    formula_cells = 0
    for worksheet in workbook.worksheets:
        for row in worksheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    if _ILLEGAL_RE.search(cell.value):
                        illegal_hits += 1
                if getattr(cell, "data_type", "") == "f":
                    formula_cells += 1
    check.check(f"{label}：工作簿无 XML 非法字符", illegal_hits == 0, f"hits={illegal_hits}")
    check.check(f"{label}：工作簿无公式单元格（data_type!='f'）", formula_cells == 0,
                f"formulas={formula_cells}")


def test_excel_export_safety(check: _Checker) -> None:
    check.section("13) 导出 Excel 安全：非法字符 / 公式注入 / 超长字段")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_excel_safety_")
    try:
        db_path = os.path.join(tmp_dir, "excel_safety.db")
        conn = sqlite3.connect(db_path)
        try:
            _create_table(conn, "xhs_note", ORM_COLUMNS["xhs_note"])
            base = int(datetime(2026, 11, 9, 17, 0, 0).timestamp())
            old_ts = (base - 3600) * 1000
            new_ts = (base + 60) * 1000
            common = {"creator_hash": "u", "nickname": "作者", "add_ts": old_ts,
                      "last_modify_ts": new_ts, "source_keyword": "示例科技"}
            _insert_orm_rows(conn, "xhs_note", [
                dict(common, note_id="note_ctrl", title="含控制字符\x18的标题",
                     desc="正文\x00保留", nickname="昵称\x0b"),
                dict(common, note_id="note_formula", title="=SUM(A1:A2)+1", desc="公式测试"),
                dict(common, note_id="note_long", title="长" * 33000, desc="超长文本"),
                dict(common, note_id="note_emoji", title="😀示例科技", desc=""),
            ])
            conn.commit()
        finally:
            conn.close()

        result = export_run_excel(db_path, os.path.join(tmp_dir, "out"), "=run\x18id",
                                  ["示例科技"], base)
        check.check("含非法字符/公式/超长字段的导出成功", os.path.isfile(result["file_path"]), str(result))
        workbook = load_workbook(result["file_path"])
        try:
            _scan_workbook_safety(check, workbook, "安全导出")
            xhs_header = [c.value for c in workbook["小红书"][1]]
            note_index = xhs_header.index("note_id") + 1
            rows_by_note = {}
            for row in range(2, workbook["小红书"].max_row + 1):
                row_data = {}
                for col_index, header in enumerate(xhs_header, start=1):
                    row_data[header] = workbook["小红书"].cell(row=row, column=col_index)
                rows_by_note[workbook["小红书"].cell(row=row, column=note_index).value] = row_data
            ctrl = rows_by_note.get("note_ctrl", {})
            check.check("非法字符替换为空格且其余文本保留",
                        (ctrl.get("title").value if ctrl else None) == "含控制字符 的标题",
                        repr(ctrl.get("title").value if ctrl else None))
            check.check("\\x00 正文清洗", (ctrl.get("desc").value if ctrl else None) == "正文 保留")
            check.check("\\x0b 昵称清洗", (ctrl.get("nickname").value if ctrl else None) == "昵称 ")
            formula_cell = rows_by_note.get("note_formula", {}).get("title")
            check.check("以 = 开头的文本按文本写入（data_type='s'）",
                        formula_cell is not None and formula_cell.data_type == "s"
                        and formula_cell.value == "=SUM(A1:A2)+1",
                        f"type={getattr(formula_cell, 'data_type', None)} value={getattr(formula_cell, 'value', None)!r}")
            long_cell = rows_by_note.get("note_long", {}).get("title")
            check.check("超长字段清洗后截断到 32000",
                        long_cell is not None and isinstance(long_cell.value, str)
                        and len(long_cell.value) == 32000, str(len(long_cell.value) if long_cell else 0))
            emoji_cell = rows_by_note.get("note_emoji", {}).get("title")
            check.check("emoji 保留", emoji_cell is not None and emoji_cell.value == "😀示例科技")
            # 概览 run_id 同时含非法字符 + 以 = 开头
            overview = workbook["概览"]
            run_id_cell = None
            for row in range(1, overview.max_row + 1):
                if overview.cell(row=row, column=1).value == "run_id":
                    run_id_cell = overview.cell(row=row, column=2)
                    break
            check.check("概览 run_id 清洗非法字符",
                        run_id_cell is not None and run_id_cell.value == "=run id",
                        repr(run_id_cell.value if run_id_cell else None))
            check.check("概览 run_id 以 = 开头仍按文本写入",
                        run_id_cell is not None and run_id_cell.data_type == "s")
        finally:
            workbook.close()

        prev = preview_run(db_path, ["示例科技"], base)
        xhs_sheet = next((item for item in prev["sheets"] if item["name"] == "小红书"), None)
        check.check("preview 值已清洗", xhs_sheet is not None
                    and all(not _ILLEGAL_RE.search(str(value)) for row in xhs_sheet["rows"]
                            for value in row if isinstance(value, str)))
        if xhs_sheet:
            title_index = xhs_sheet["columns"].index("title")
            preview_titles = [row[title_index] for row in xhs_sheet["rows"]]
            check.check("preview 含控制字符标题已清洗", "含控制字符 的标题" in preview_titles,
                        str(preview_titles[:6]))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_real_db_run_d466(check: _Checker) -> None:
    """真实库 run_d4669ed9e230 只读导出：含贴吧 \\x18 数据也必须成功并清洗。"""
    check.section("14) 真实库 run_d4669ed9e230 只读导出（含贴吧 \\x18 数据）")
    if not os.path.isfile(REAL_APP_DB):
        check.check("真实库 run_d466 回归跳过（DB 不存在）", True, REAL_APP_DB)
        print(f"  [SKIP] 真实库不存在：{REAL_APP_DB}")
        return
    started_ms = int(RUN_D466["started_ts"]) * 1000
    finished_ms = int(RUN_D466["finished_ts"]) * 1000
    tmp_dir = tempfile.mkdtemp(prefix="exporter_real_d466_")
    snapshot_db = os.path.join(tmp_dir, "media_crawler_run_d466.db")
    db_before_sha = hashlib.sha256(open(REAL_APP_DB, "rb").read()).hexdigest()
    try:
        _snapshot_media_db(snapshot_db, started_ms, finished_ms)
        # 已知坏行：tieba_note rowid=25 的 desc 含 \x18
        connection = open_readonly(snapshot_db)
        try:
            bad_row = connection.execute(
                "SELECT rowid, note_id, title, desc FROM tieba_note WHERE rowid=?",
                (RUN_D466["tieba_bad_rowid"],),
            ).fetchone()
            run_table_counts: Dict[str, int] = {}
            keyword_placeholders = ",".join("?" * len(RUN_D466["keywords"]))
            for platform, content_table, comment_table in (
                ("小红书", "xhs_note", "xhs_note_comment"),
                ("抖音", "douyin_aweme", "douyin_aweme_comment"),
                ("快手", "kuaishou_video", "kuaishou_video_comment"),
                ("B站", "bilibili_video", "bilibili_video_comment"),
                ("微博", "weibo_note", "weibo_note_comment"),
                ("贴吧", "tieba_note", "tieba_comment"),
                ("知乎", "zhihu_content", "zhihu_comment"),
            ):
                content_columns = [row["name"] for row in connection.execute(
                    f'PRAGMA table_info("{content_table}")')]
                if not content_columns:
                    continue
                content_count = connection.execute(
                    f'SELECT COUNT(*) FROM "{content_table}" '
                    f'WHERE CAST(last_modify_ts AS INTEGER) >= ? '
                    f'AND CAST(last_modify_ts AS INTEGER) <= ? '
                    f'AND TRIM(COALESCE(source_keyword, \'\')) IN ({keyword_placeholders})',
                    [started_ms, finished_ms] + list(RUN_D466["keywords"]),
                ).fetchone()[0]
                comment_count = 0
                comment_columns = [row["name"] for row in connection.execute(
                    f'PRAGMA table_info("{comment_table}")')]
                if comment_columns:
                    id_col = {"小红书": "note_id", "抖音": "aweme_id", "快手": "video_id",
                              "B站": "video_id", "微博": "note_id", "贴吧": "note_id",
                              "知乎": "content_id"}[platform]
                    parent_col = {"小红书": "note_id", "抖音": "aweme_id", "快手": "video_id",
                                  "B站": "video_id", "微博": "note_id", "贴吧": "note_id",
                                  "知乎": "content_id"}[platform]
                    content_ids = {str(row[id_col]) for row in connection.execute(
                        f'SELECT "{id_col}" FROM "{content_table}" '
                        f'WHERE CAST(last_modify_ts AS INTEGER) >= ? '
                        f'AND CAST(last_modify_ts AS INTEGER) <= ? '
                        f'AND TRIM(COALESCE(source_keyword, \'\')) IN ({keyword_placeholders})',
                        [started_ms, finished_ms] + list(RUN_D466["keywords"]))}
                    rows = [dict(row) for row in connection.execute(f'SELECT * FROM "{comment_table}"')]
                    comment_count = sum(
                        1 for row in rows
                        if row.get("last_modify_ts") is not None
                        and started_ms <= int(row["last_modify_ts"]) <= finished_ms
                        and str(row.get(parent_col)) in content_ids
                    )
                run_table_counts[platform] = content_count
                run_table_counts[platform + "评论"] = comment_count
        finally:
            connection.close()

        if any(run_table_counts.get(name) != count for name, count in RUN_D466["expected_sheet_rows"].items()):
            check.check("真实库 run_d466 快照行数与预期一致", True,
                        f"当前库已变化：{run_table_counts}（跳过精确断言）")
            print(f"  [SKIP] run_d466 快照数据已变化：{run_table_counts}")
            return

        result = export_run_excel(
            snapshot_db, os.path.join(tmp_dir, "out"), RUN_D466["run_id"],
            RUN_D466["keywords"], RUN_D466["started_ts"],
        )
        rows = {item["name"]: item["rows"] for item in result["sheets"]}
        check.check("真实库 run_d466 导出成功（无 IllegalCharacterError）", True)
        check.check("真实库 run_d466 导出总行数 = 1713",
                    result["total_rows"] == RUN_D466["expected_total"], str(result["total_rows"]))
        for name, count in RUN_D466["expected_sheet_rows"].items():
            check.check(f"run_d466 导出 {name} = {count} 行", rows.get(name) == count, str(rows.get(name)))

        expected_desc = sanitize_excel_text(bad_row["desc"]) if bad_row else ""
        check.check("真实库含非法字符的贴吧行在导出中（rowid=25）",
                    bad_row is not None and bool(expected_desc), f"bad_row={bool(bad_row)}")
        check.check("清洗后的贴吧 desc 不再含非法字符",
                    _ILLEGAL_RE.search(expected_desc) is None,
                    str([hex(ord(c)) for c in _ILLEGAL_RE.findall(expected_desc or "")]))
        check.check("清洗后 desc 长度保持 180", len(expected_desc) == 180, str(len(expected_desc)))
        print(f"  [真实回归 d466] 各 sheet 行数：{json.dumps(rows, ensure_ascii=False)}；"
              f"total_rows={result['total_rows']}")
        print(f"  [真实回归 d466] 贴吧 rowid=25 清洗后 desc 长度={len(expected_desc)}，"
              f"尾 20 字符={expected_desc[-20:]!r}，含非法字符={bool(_ILLEGAL_RE.search(expected_desc))}")

        workbook = load_workbook(result["file_path"])
        try:
            _scan_workbook_safety(check, workbook, "run_d466")
            tieba_sheet = workbook["贴吧"]
            header = [c.value for c in tieba_sheet[1]]
            note_index = header.index("note_id") + 1
            desc_index = header.index("desc") + 1
            found_bad_row = False
            for row in range(2, tieba_sheet.max_row + 1):
                note_value = tieba_sheet.cell(row=row, column=note_index).value
                if bad_row is not None and str(note_value) == str(bad_row["note_id"]):
                    exported_desc = tieba_sheet.cell(row=row, column=desc_index).value or ""
                    found_bad_row = True
                    check.check("导出的贴吧行 desc = 清洗后的 DB 值", exported_desc == expected_desc,
                                f"len={len(exported_desc)}")
                    check.check("导出的贴吧行 desc 保留原文关键内容",
                                "示例人物" in exported_desc and "小笨蛋" in exported_desc)
                    break
            check.check("在导出 Excel 中找到该贴吧行", found_bad_row)
        finally:
            workbook.close()

        # 样例文件留档
        regression_dir = os.path.join(FIXTURE_DIR, "regression")
        os.makedirs(regression_dir, exist_ok=True)
        sample_path = os.path.join(regression_dir, "run_d4669ed9e230_Excel安全清洗导出.xlsx")
        shutil.copyfile(result["file_path"], sample_path)
        check.check("run_d466 样例文件已留档", os.path.isfile(sample_path), sample_path)

        # 真实库未被修改
        db_after_sha = hashlib.sha256(open(REAL_APP_DB, "rb").read()).hexdigest()
        check.check("真实库未被修改（sha256 前后一致）", db_before_sha == db_after_sha)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 15：Excel 情感列（sentiment / sentiment_reason，来自 app.db sentiment_cache）
# ---------------------------------------------------------------------------
def _create_sentiment_cache_db(path: str, entries: Sequence[Dict[str, Any]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sentiment_cache (
                platform TEXT NOT NULL, kind TEXT NOT NULL, item_id TEXT NOT NULL,
                text_hash TEXT NOT NULL, sentiment TEXT NOT NULL, reason TEXT DEFAULT '',
                model TEXT DEFAULT '', updated_ts INTEGER NOT NULL,
                PRIMARY KEY (platform, kind, item_id, text_hash)
            );
            """
        )
        conn.executemany(
            "INSERT OR REPLACE INTO sentiment_cache "
            "(platform, kind, item_id, text_hash, sentiment, reason, model, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(row["platform"], row["kind"], row["item_id"], row.get("text_hash", "h"),
              row["sentiment"], row.get("reason", ""), row.get("model", "stub"),
              int(row.get("updated_ts", 1794212156)))
             for row in entries],
        )
        conn.commit()
    finally:
        conn.close()


def test_sentiment_columns(check: _Checker) -> None:
    """Excel 情感列：缓存命中值/无缓存空/未知值空/概览分析统计/预览列。"""
    check.section("15) Excel 情感列 sentiment/sentiment_reason（app.db sentiment_cache）")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_sentiment_cols_")
    try:
        db_path = os.path.join(tmp_dir, "media_crawler.db")
        app_db = os.path.join(tmp_dir, "app.db")
        conn = sqlite3.connect(db_path)
        try:
            _create_table(conn, "xhs_note", ORM_COLUMNS["xhs_note"])
            _create_table(conn, "xhs_note_comment", ORM_COLUMNS["xhs_note_comment"])
            base = int(datetime(2026, 11, 9, 18, 0, 0).timestamp())
            old_ts = (base - 3600) * 1000
            new_ts = (base + 60) * 1000
            _insert_orm_rows(conn, "xhs_note", [
                {"creator_hash": "u", "nickname": "作者", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "note_id": "n1", "title": "示例科技 正面内容", "desc": "", "source_keyword": "示例科技"},
                {"creator_hash": "u", "nickname": "作者", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "note_id": "n2", "title": "示例科技 未分析内容", "desc": "", "source_keyword": "示例科技"},
            ])
            _insert_orm_rows(conn, "xhs_note_comment", [
                {"creator_hash": "c", "nickname": "评论者", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "comment_id": "c1", "note_id": "n1", "content": "负面评论"},
                {"creator_hash": "c", "nickname": "评论者", "add_ts": old_ts, "last_modify_ts": new_ts,
                 "comment_id": "c2", "note_id": "n2", "content": "未分析评论"},
            ])
            conn.commit()
        finally:
            conn.close()
        _create_sentiment_cache_db(app_db, [
            {"platform": "xhs", "kind": "content", "item_id": "n1",
             "sentiment": "positive", "reason": "原文正面"},
            {"platform": "xhs", "kind": "comment", "item_id": "c1",
             "sentiment": "负面", "reason": "评论负面"},
            {"platform": "xhs", "kind": "content", "item_id": "n2",
             "sentiment": "unknown", "reason": "无法识别"},
        ])

        result = export_run_excel(db_path, os.path.join(tmp_dir, "out"), "run_sentiment_cols",
                                  ["示例科技"], base, app_db_path=app_db)
        workbook = load_workbook(result["file_path"])
        try:
            header = [c.value for c in workbook["小红书"][1]]
            check.check("情感列位置：filtered_reason 之后 sentiment / sentiment_reason",
                        header[-3:] == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN],
                        str(header[-3:]))
            rows = [dict(zip(header, r)) for r in workbook["小红书"].iter_rows(min_row=2, values_only=True)]
            by_note = {row["note_id"]: row for row in rows}
            check.check("缓存命中：sentiment=正面、reason=原文正面",
                        by_note["n1"][SENTIMENT_COLUMN] == "正面"
                        and by_note["n1"][SENTIMENT_REASON_COLUMN] == "原文正面",
                        str({key: by_note["n1"].get(key) for key in (SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN)}))
            check.check("无有效缓存：sentiment/sentiment_reason 留空",
                        (by_note["n2"].get(SENTIMENT_COLUMN) or "") == ""
                        and (by_note["n2"].get(SENTIMENT_REASON_COLUMN) or "") == "")
            comment_header = [c.value for c in workbook["小红书评论"][1]]
            comment_rows = [dict(zip(comment_header, r))
                            for r in workbook["小红书评论"].iter_rows(min_row=2, values_only=True)]
            comment_by_id = {row["comment_id"]: row for row in comment_rows}
            check.check("评论情感列：缓存英文 negative → 中文 负面",
                        comment_by_id["c1"][SENTIMENT_COLUMN] == "负面"
                        and comment_by_id["c1"][SENTIMENT_REASON_COLUMN] == "评论负面")
            check.check("评论无缓存留空",
                        (comment_by_id["c2"].get(SENTIMENT_COLUMN) or "") == "")
            overview = _parse_overview(workbook["概览"])
            meta = overview["meta"]
            check.check("概览已情感分析行数 = 2", int(meta.get("已情感分析行数", -1)) == 2,
                        str(meta.get("已情感分析行数")))
            check.check("概览未分析行数 = 2", int(meta.get("未分析行数", -1)) == 2,
                        str(meta.get("未分析行数")))
        finally:
            workbook.close()

        prev = preview_run(db_path, ["示例科技"], base, app_db_path=app_db)
        xhs_sheet = next((item for item in prev["sheets"] if item["name"] == "小红书"), None)
        check.check("preview 列含情感两列且值一致",
                    xhs_sheet is not None
                    and xhs_sheet["columns"][-3:] == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN,
                                                      SENTIMENT_REASON_COLUMN]
                    and any(row[xhs_sheet["columns"].index(SENTIMENT_COLUMN)] == "正面"
                            for row in xhs_sheet["rows"]),
                    str(xhs_sheet["columns"][-3:] if xhs_sheet else None))

        # 无 app.db（默认推导路径不存在）→ 情感列仍存在但全空，概览 0 已分析
        result2 = export_run_excel(db_path, os.path.join(tmp_dir, "out2"), "run_no_cache",
                                   ["示例科技"], base)
        workbook2 = load_workbook(result2["file_path"])
        try:
            header2 = [c.value for c in workbook2["小红书"][1]]
            rows2 = [dict(zip(header2, r)) for r in workbook2["小红书"].iter_rows(min_row=2, values_only=True)]
            check.check("无缓存时情感列仍存在且为空",
                        header2[-1] == SENTIMENT_REASON_COLUMN
                        and all(not (row.get(SENTIMENT_COLUMN) or "") for row in rows2))
            meta2 = _parse_overview(workbook2["概览"])["meta"]
            check.check("无缓存时概览已分析行数 = 0", int(meta2.get("已情感分析行数", -1)) == 0,
                        str(meta2.get("已情感分析行数")))
        finally:
            workbook2.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 12/13/14：P1-2 声明式列规格 + 导出自检 + 与旧实现等价
# ---------------------------------------------------------------------------
def test_sheet_specs(check: _Checker, fixture: Dict[str, Any], fixture_db: str) -> None:
    check.section("16) 声明式列规格：SHEET_SPECS / 规格 vs 旧派生 / 平台单一来源")
    check.check("SHEET_SPECS 首表为 概览", SHEET_SPECS[0].name == "概览" and SHEET_SPECS[0].kind == "overview",
                f"{SHEET_SPECS[0].name}/{SHEET_SPECS[0].kind}")
    check.check("SHEET_SPECS = 概览 + 14 张数据表（7 平台 × 内容/评论）",
                len(SHEET_SPECS) == 15 and len(SHEETS) == 14,
                f"{len(SHEET_SPECS)}/{len(SHEETS)}")
    check.check("SHEETS 由 SHEET_SPECS 派生（tab 名/表名/顺序一致）",
                [(s["name"], s["table"]) for s in SHEETS]
                == [(s.name, s.table) for s in SHEET_SPECS[1:]])
    expected_names: List[str] = []
    expected_tables: List[str] = []
    for platform in PLATFORM_SHEETS:
        expected_names.extend([PLATFORM_LABELS[platform.code], PLATFORM_LABELS[platform.code] + "评论"])
        expected_tables.extend([platform.content_table, platform.comment_table])
    check.check("平台清单来自单一常量（PLATFORM_LABELS + PLATFORM_SHEETS）",
                [s["name"] for s in SHEETS] == expected_names
                and [s["table"] for s in SHEETS] == expected_tables,
                f"{[s['name'] for s in SHEETS][:3]} != {expected_names[:3]}")
    check.check("每张数据表列规格都以 filtered_reason/sentiment/sentiment_reason 结尾",
                all([col.label for col in spec.columns][-3:]
                    == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN]
                    for spec in SHEET_SPECS[1:]))
    check.check("creator_hash 列在规格中输出为 user_id",
                any(col.label == "user_id" and col.field == "creator_hash"
                    for spec in SHEET_SPECS[1:] for col in spec.columns))

    # 规格列 vs 旧派生（output_columns(db_columns) + 三个计算列）：逐表严格等价
    mismatched: List[str] = []
    strict_failures: List[str] = []
    conn = open_readonly(fixture_db)
    try:
        for spec in SHEET_SPECS[1:]:
            if spec.table not in fixture["tables"]:
                continue
            db_columns = [row["name"] for row in conn.execute(f'PRAGMA table_info("{spec.table}")')]
            try:
                resolved = resolve_sheet_columns(spec, db_columns, strict=True)
            except ExportSchemaMismatch as exc:
                strict_failures.append(f"{spec.name}: {exc}")
                continue
            legacy = [pair[0] for pair in output_columns(db_columns)] + [
                FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN,
            ]
            if [pair[0] for pair in resolved.pairs] != legacy:
                mismatched.append(f"{spec.name}: {[pair[0] for pair in resolved.pairs]} != {legacy}")
    finally:
        conn.close()
    check.check("严格模式下 fixture 每张表都无缺列/未声明列", not strict_failures, "; ".join(strict_failures[:2]))
    check.check("规格列 = 旧 output_columns 派生结果（含末尾三列）", not mismatched, "; ".join(mismatched[:2]))

    # 旧库兼容：只有 user_id（无 creator_hash）时回退命中
    legacy_columns = ["id", "user_id", "nickname", "add_ts", "last_modify_ts", "note_id", "source_keyword"]
    legacy_resolved = resolve_sheet_columns(SHEET_SPECS[1], legacy_columns)
    check.check("旧库 user_id 列通过 fallbacks 命中（不误报缺列）",
                ("user_id", "user_id") in legacy_resolved.pairs
                and "user_id" not in legacy_resolved.missing,
                str(legacy_resolved.missing))
    check.check("旧库缺展示列只在 missing 里记录、不抛错",
                "video_url" not in [pair[0] for pair in legacy_resolved.pairs]
                and "video_url" in legacy_resolved.missing)

    # 导出返回值带自检报告
    tmp_dir = tempfile.mkdtemp(prefix="exporter_schema_ok_")
    try:
        result = export_run_excel(fixture_db, os.path.join(tmp_dir, "out"), "run_schema_ok",
                                  [KEYWORD_MAIN], fixture["started_ts"],
                                  app_db_path=os.path.join(tmp_dir, "no-cache.db"))
        report = result.get("schema") or {}
        check.check("导出返回 schema 自检报告（ok=True, strict=False）",
                    report.get("ok") is True and report.get("strict") is False, str(report)[:120])
        check.check("schema 报告覆盖每个导出 sheet",
                    set(report.get("sheets") or {}) == {item["name"] for item in result["sheets"]},
                    str(sorted(report.get("sheets") or {}))[:120])
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _make_export_db(tmp_dir: str, tag: str, table: str, columns: Sequence[str],
                    insert_columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """建一个只有单表的临时库（用于制造 schema 漂移）。"""
    db_path = os.path.join(tmp_dir, f"{table}_{tag}.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    try:
        _create_table(conn, table, columns)
        if rows:
            _insert_rows(conn, table, insert_columns, rows)
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_schema_guard(check: _Checker) -> None:
    check.section("17) 导出自检：EXPORT_SCHEMA_MISMATCH（缺结构列 / 缺展示列 / 未声明列）")
    base = int(datetime(2026, 11, 9, 10, 0, 0).timestamp())
    new_ms = (base + 60) * 1000
    tmp_dir = tempfile.mkdtemp(prefix="exporter_schema_guard_")
    try:
        # --- 1) 结构列缺失（内容 ID 列）→ 默认模式直接抛错 ---
        no_id_cols = [c for c in ORM_COLUMNS["xhs_note"] if c != "note_id"]
        no_id_db = _make_export_db(
            tmp_dir, "no_note_id", "xhs_note", no_id_cols,
            ["creator_hash", "nickname", "add_ts", "last_modify_ts", "type", "title", "desc",
             "source_keyword"],
            [("creator_x", "作者", new_ms, new_ms, "normal", "标题", "正文", KEYWORD_MAIN)],
        )
        try:
            export_run_excel(no_id_db, os.path.join(tmp_dir, "out1"), "run_missing_id",
                             [KEYWORD_MAIN], base)
            check.check("缺结构列（note_id）应抛 EXPORT_SCHEMA_MISMATCH", False, "未抛错")
        except ExportSchemaMismatch as exc:
            check.check("缺结构列（note_id）应抛 EXPORT_SCHEMA_MISMATCH", True)
            check.check("错误码为 EXPORT_SCHEMA_MISMATCH", exc.code == EXPORT_SCHEMA_MISMATCH,
                        str(exc.code))
            check.check("错误信息含缺失列名 note_id", "note_id" in str(exc), str(exc))
            check.check("异常记录 missing_required = ('note_id',)", exc.missing_required == ("note_id",),
                        str(exc.missing_required))
            check.check("结构列缺失属于 sheet「小红书」", exc.sheet == "小红书", exc.sheet)
        except Exception as exc:  # noqa: BLE001
            check.check("缺结构列应抛 ExportSchemaMismatch（其余异常算失败）", False,
                        f"{type(exc).__name__}: {exc}")

        # --- 2) 展示列缺失（video_url）：strict 抛错；默认模式跳过并记录 ---
        no_field_cols = [c for c in ORM_COLUMNS["xhs_note"] if c != "video_url"]
        no_field_db = _make_export_db(
            tmp_dir, "no_video_url", "xhs_note", no_field_cols,
            ["creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "type", "title", "desc",
             "source_keyword"],
            [("creator_x", "作者", new_ms, new_ms, "n1", "normal", "示例科技发布", "正文", "示例科技")],
        )
        try:
            export_run_excel(no_field_db, os.path.join(tmp_dir, "out2"), "run_strict_missing",
                             [KEYWORD_MAIN], base, strict_schema=True)
            check.check("strict 模式：规格有声明的展示列缺失 → 抛错", False, "未抛错")
        except ExportSchemaMismatch as exc:
            check.check("strict 模式：规格有声明的展示列缺失 → 抛错", True)
            check.check("strict 错误信息含缺失列名 video_url", "video_url" in str(exc), str(exc))
        result = export_run_excel(no_field_db, os.path.join(tmp_dir, "out2"), "run_missing_field",
                                  [KEYWORD_MAIN], base)
        workbook = load_workbook(result["file_path"])
        try:
            header = [c.value for c in workbook["小红书"][1]]
        finally:
            workbook.close()
        check.check("默认模式：缺展示列仍可导出（旧库兼容），表头不含该列",
                    result["total_rows"] == 1 and "video_url" not in header, str(header[:6]))
        check.check("默认模式：schema 报告记录缺失列（不再静默）",
                    "video_url" in (result["schema"]["sheets"]["小红书"]["missing"]),
                    str(result["schema"]["sheets"]["小红书"]["missing"]))

        # --- 3) 未声明列（上游 ORM 新增字段）：strict 抛错；默认模式追加并记录 ---
        extra_db = _make_export_db(
            tmp_dir, "extra_ai_judge", "xhs_note", ORM_COLUMNS["xhs_note"] + ["ai_judge"],
            ["creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "type", "title", "desc",
             "source_keyword", "ai_judge"],
            [("creator_x", "作者", new_ms, new_ms, "n1", "normal", "示例科技发布", "正文", "示例科技",
              "正面")],
        )
        try:
            export_run_excel(extra_db, os.path.join(tmp_dir, "out3"), "run_strict_extra",
                             [KEYWORD_MAIN], base, strict_schema=True)
            check.check("strict 模式：DB 存在未声明列 → 抛错", False, "未抛错")
        except ExportSchemaMismatch as exc:
            check.check("strict 模式：DB 存在未声明列 → 抛错", True)
            check.check("strict 错误信息含未声明列名 ai_judge", "ai_judge" in str(exc), str(exc))
        result = export_run_excel(extra_db, os.path.join(tmp_dir, "out3"), "run_extra",
                                  [KEYWORD_MAIN], base)
        workbook = load_workbook(result["file_path"])
        try:
            header = [c.value for c in workbook["小红书"][1]]
            rows = [dict(zip(header, r)) for r in workbook["小红书"].iter_rows(min_row=2, values_only=True)]
        finally:
            workbook.close()
        check.check("默认模式：未声明列追加在计算列之前并原样输出",
                    "ai_judge" in header and header[-3:] == [FILTERED_REASON_COLUMN, SENTIMENT_COLUMN,
                                                             SENTIMENT_REASON_COLUMN]
                    and rows and rows[0].get("ai_judge") == "正面",
                    str(header[-5:]))
        check.check("默认模式：schema 报告记录未声明列",
                    "ai_judge" in (result["schema"]["sheets"]["小红书"]["extra"]),
                    str(result["schema"]["sheets"]["小红书"]["extra"]))

        # --- 4) 规格表有、DB 没有的列可直接由 resolve_sheet_columns 断言 ---
        spec = next(item for item in SHEET_SPECS if item.table == "xhs_note")
        try:
            resolve_sheet_columns(spec, [c for c in ORM_COLUMNS["xhs_note"] if c != "note_id"], strict=True)
            check.check("resolve_sheet_columns(strict=True) 对缺列抛错", False)
        except ExportSchemaMismatch as exc:
            check.check("resolve_sheet_columns(strict=True) 对缺列抛错", "note_id" in str(exc), str(exc))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _expected_reason(row: Dict[str, Any]) -> str:
    """测试侧独立实现的相关性期望（不复用 exporter.content_filtered_reason）。"""
    tokens = [token for token in re.split(r"[,，、;；\s|]+", str(row.get("source_keyword") or ""))
              if token.strip()]
    if not tokens:
        return OFF_BRAND
    haystack = "\n".join(
        str(row.get(column) or "")
        for column in ("title", "desc", "content", "content_text", "nickname", "user_nickname")
        if row.get(column) not in (None, "")
    ).lower()
    if not haystack:
        return OFF_BRAND
    return "" if any(token.strip().lower() in haystack for token in tokens) else OFF_BRAND


def _expected_cell(table: str, column: str, row: Dict[str, Any]) -> Any:
    """测试侧独立实现的单元格期望（时间格式化 / ID 文本化 / 其余原样）。"""
    if column == FILTERED_REASON_COLUMN:
        value: Any = _expected_reason(row)
    elif column in (SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN):
        value = ""  # 本测试显式传入不存在的 app.db → 无情感缓存
    else:
        db_column = "creator_hash" if column == "user_id" else column
        raw = row.get(db_column if db_column in row else column)
        if db_column in TIME_FIELDS:
            unit = TS_UNITS.get((table, db_column))
            if unit is None and db_column in ("add_ts", "last_modify_ts"):
                unit = "ms"
            if isinstance(raw, (int, float)) and 20000 <= float(raw) <= 80000:
                unit = None  # Excel 日期序列号（fixture 特殊用例）
            value = _expected_time_text(raw, unit)
        elif column in ID_COLUMNS or db_column in ID_COLUMNS:
            value = _to_text(raw)
        else:
            value = "" if raw is None else raw
    # 空字符串写入 Excel 后读回来是 None（与旧实现一致），期望值同步归一
    return None if value == "" else value


def test_export_equivalence(check: _Checker, fixture: Dict[str, Any], fixture_db: str) -> None:
    check.section("18) 导出的 Excel 与旧实现等价（3 张表：表头 + 前 2 行取值）")
    tmp_dir = tempfile.mkdtemp(prefix="exporter_equivalent_")
    try:
        result = export_run_excel(fixture_db, os.path.join(tmp_dir, "out"), "run_equivalent",
                                  [KEYWORD_MAIN], fixture["started_ts"],
                                  app_db_path=os.path.join(tmp_dir, "no-cache.db"))
        workbook = load_workbook(result["file_path"])
        try:
            for sheet_name, table in (("小红书", "xhs_note"), ("抖音", "douyin_aweme"),
                                      ("B站", "bilibili_video")):
                ws = workbook[sheet_name]
                header = [cell.value for cell in ws[1]]
                expected_header = [pair[0] for pair in output_columns(ORM_COLUMNS[table])] + [
                    FILTERED_REASON_COLUMN, SENTIMENT_COLUMN, SENTIMENT_REASON_COLUMN,
                ]
                check.check(f"{sheet_name} 表头与旧实现逐列一致", header == expected_header,
                            f"{header[:4]}... != {expected_header[:4]}...")
                kept = fixture["expected"]["first_rows"].get(sheet_name) or []
                if len(kept) < 2:
                    check.check(f"{sheet_name} fixture 至少 2 行数据", False, str(len(kept)))
                    continue
                for offset in range(2):
                    actual = [cell.value for cell in ws[offset + 2]]
                    expected = [_expected_cell(table, column, kept[offset]) for column in header]
                    check.check(f"{sheet_name} 第 {offset + 1} 行取值与旧实现一致", actual == expected,
                                json.dumps({"actual": actual[:6], "expected": expected[:6]},
                                           ensure_ascii=False, default=str))
                check.check(f"{sheet_name} 行数与 fixture 期望一致",
                            ws.max_row - 1 == fixture["expected"]["sheet_rows"][sheet_name],
                            f"{ws.max_row - 1}")
        finally:
            workbook.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    excel_path = os.environ.get("PIPELINE_SAMPLE_EXCEL", DEFAULT_EXCEL)
    if "--excel" in argv:
        excel_path = argv[argv.index("--excel") + 1]

    print("=" * 78)
    print("exporter.py 离线自测（MediaCrawler sqlite → Excel，不联网）")
    print("=" * 78)
    print(f"样例 Excel：{excel_path}")
    check = _Checker()

    os.makedirs(FIXTURE_DIR, exist_ok=True)
    shutil.rmtree(FIXTURE_OUT, ignore_errors=True)

    print("\n[0] 构造 fixture：读取样例 Excel → 写临时 MediaCrawler 表结构 sqlite")
    using_real_excel = os.path.isfile(excel_path)
    dataset = load_sample_excel(excel_path)
    fixture = build_fixture_sqlite(FIXTURE_DB, dataset)
    table_rows = {table: len(rows) for table, rows in fixture["tables"].items()}
    print(f"  fixture db: {FIXTURE_DB}")
    print(f"  表行数：{json.dumps(table_rows, ensure_ascii=False)}")
    print(f"  采集开始时间 started_ts={fixture['started_ts']}；期望 sheet 行数："
          f"{json.dumps(fixture['expected']['sheet_rows'], ensure_ascii=False)}")
    check.check("fixture 表数量 = 12（贴吧缺失用于跳过测试）", len(fixture["tables"]) == 12,
                str(len(fixture["tables"])))
    if using_real_excel:
        check.check("fixture 行数来自真实样例 Excel（12 sheet 合计 552 行）",
                    sum(table_rows.values()) == 552, str(sum(table_rows.values())))
    else:
        check.check("无样例 Excel 时合成 fixture 可用", sum(table_rows.values()) > 0,
                    str(sum(table_rows.values())))

    test_export(check, fixture, FIXTURE_DB)
    test_empty_and_missing(check, FIXTURE_DB, fixture)
    test_legacy_schema(check)
    test_preview(check, FIXTURE_DB, fixture)
    test_helpers(check)
    test_last_modify_filter(check)
    test_legacy_missing_last_modify(check)
    test_real_db_regression(check)
    test_relevance_helpers(check)
    test_relevance_export(check)
    test_relevance_existing_column(check)
    test_real_db_run_259(check)
    test_excel_sanitize_helpers(check)
    test_excel_export_safety(check)
    test_real_db_run_d466(check)
    test_sentiment_columns(check)
    test_sheet_specs(check, fixture, FIXTURE_DB)
    test_schema_guard(check)
    test_export_equivalence(check, fixture, FIXTURE_DB)

    code = check.summary()
    if code == 0:
        exported = sorted(os.listdir(os.path.join(FIXTURE_OUT, "exports"))) if os.path.isdir(
            os.path.join(FIXTURE_OUT, "exports")) else []
        print(f"\n测试产物：\n  fixture: {FIXTURE_DB}\n  导出文件：{exported}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())



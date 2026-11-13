# -*- coding: utf-8 -*-
"""
exporter.py — MediaCrawler SQLite → 多 sheet Excel 导出器（M1 采集闭环，不脱敏）

契约见 `app/CONTRACT.md` §3；本模块是**纯函数模块**，不启动任何服务，
供 sidecar 直接 `from exporter import export_run_excel, preview_run` 使用：

    export_run_excel(db_path, out_dir, run_id, keywords, started_ts) -> dict
    preview_run(db_path, keywords, started_ts, limit=20) -> dict

行为约定（与契约逐条对应）：
  1. 表 → sheet 顺序固定（见 SHEETS），表不存在时跳过该 sheet，不报错；
  2. 过滤（2026-11-09 修复：UI saved=100 而 Excel 只有 1 条）：
       - 内容表：`source_keyword ∈ keywords` 且 **`last_modify_ts >= started_ts`**；
       - 评论表：父内容 ID 集合（同关键词 + **同一 last_modify 口径**）内，
         且评论自身 **`last_modify_ts >= started_ts`**；
       - `last_modify_ts` 缺失 / 为 0 时回退 `add_ts`（兼容旧库/旧表结构）；
       - 原因：`add_ts` 是**首次入库时间**，重复采集不会变化；`last_modify_ts` 是
         **本次运行最后写入时间**，才能反映“本次新增+更新”的数据量（与 UI saved 对齐）。
  3. 列规则：
       - `creator_hash` 列改名为 `user_id`（raw ID，不脱敏；兼容旧库只有 `user_id` 的情况）；
       - 时间字段统一格式化为 `YYYY-MM-DD HH:MM:SS`（兼容秒/毫秒时间戳、字符串、Excel 序列号）；
       - 所有 ID 列按文本写入（number_format='@'，避免 Excel 科学计数法丢位）；
       - 其余列原样输出（列名保持数据库字段名）；
  4. 首表 `概览`：run_id / 关键词 / 采集开始时间 / 导出时间 / 过滤口径 /
     相关行数 / off_brand 行数 / 相关性判定规则 / 各 sheet 行数 / 合计；
  5. 文件名：`<首关键词>_舆情数据_<YYYYMMDD_HHMMSS>.xlsx`，落在 `<out_dir>/exports/`；
  6. 空结果也生成合法 xlsx（仅表头）。
  7. 相关性标记（2026-11-09 新增）：每个数据 sheet 末尾追加 `filtered_reason` 列；
     `source_keyword`（逗号/空格拆分）在 title/desc/正文/nickname 中命中任一 token → `""`，
     否则 → `"off_brand"`（**只标记不删除行**，报告阶段再排除）；评论按父内容继承。
  8. 情感列（2026-11-09 新增）：`filtered_reason` 之后追加 `sentiment`（中文 正面/中性/负面）与
     `sentiment_reason` 两列，值来自 `<data-dir>/app.db` 的 `sentiment_cache`（按 platform+kind+item_id
     匹配，无缓存留空）；概览增加“已情感分析行数/未分析行数”。
  9. Excel 安全（2026-11-09 新增）：所有写入 Excel 的字符串统一清洗 XML 非法控制字符
     （0x00-0x08/0x0B/0x0C/0x0E-0x1F，额外 0x7F，替换为空格；保留 \\t/\\n/\\r 与 emoji）；
     以 `=`/`+`/`-`/`@` 开头的文本强制按文本写入（`data_type='s'`，防公式注入）；清洗后再做 32000 截断。

时间兼容说明（真实 MediaCrawler sqlite 现状）：
  - `add_ts` / `last_modify_ts` 在真实库中为**毫秒**整数（BigInteger，如 1794190674936）；
    旧版库可能是毫秒/秒的字符串；
  - sidecar 传入的 `started_ts` 是**秒**（如 1794190619），本模块统一归一为毫秒后比较；
  - `create_time`/`time` 在不同平台分别可能是秒（抖音/B站）或毫秒（小红书/快手）；
  - 知乎 `created_time`/`updated_time`、贴吧 `publish_time` 为日期时间字符串；
  - 本模块按数值大小（>=1e12 视为毫秒）与字符串位数自动识别，统一输出秒级本地时间格式。
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "exporter 依赖 openpyxl；请使用 MediaCrawler\\.venv\\Scripts\\python.exe"
    ) from exc


# ---------------------------------------------------------------------------
# 固定映射（顺序即 Excel sheet 顺序，契约 §3）
# ---------------------------------------------------------------------------
# kind=content：内容表，按 source_keyword + last_modify_ts 过滤，id_col 为内容 ID 列；
# kind=comment：评论表，按父内容 ID 集合 + last_modify_ts 过滤，parent_col 为父内容 ID 列。
SHEETS: List[Dict[str, str]] = [
    {"name": "小红书", "table": "xhs_note", "kind": "content", "id_col": "note_id", "platform": "xhs"},
    {"name": "小红书评论", "table": "xhs_note_comment", "kind": "comment", "parent_col": "note_id", "platform": "xhs"},
    {"name": "抖音", "table": "douyin_aweme", "kind": "content", "id_col": "aweme_id", "platform": "dy"},
    {"name": "抖音评论", "table": "douyin_aweme_comment", "kind": "comment", "parent_col": "aweme_id", "platform": "dy"},
    {"name": "快手", "table": "kuaishou_video", "kind": "content", "id_col": "video_id", "platform": "ks"},
    {"name": "快手评论", "table": "kuaishou_video_comment", "kind": "comment", "parent_col": "video_id", "platform": "ks"},
    {"name": "B站", "table": "bilibili_video", "kind": "content", "id_col": "video_id", "platform": "bili"},
    {"name": "B站评论", "table": "bilibili_video_comment", "kind": "comment", "parent_col": "video_id", "platform": "bili"},
    {"name": "微博", "table": "weibo_note", "kind": "content", "id_col": "note_id", "platform": "wb"},
    {"name": "微博评论", "table": "weibo_note_comment", "kind": "comment", "parent_col": "note_id", "platform": "wb"},
    {"name": "贴吧", "table": "tieba_note", "kind": "content", "id_col": "note_id", "platform": "tieba"},
    {"name": "贴吧评论", "table": "tieba_comment", "kind": "comment", "parent_col": "note_id", "platform": "tieba"},
    {"name": "知乎", "table": "zhihu_content", "kind": "content", "id_col": "content_id", "platform": "zhihu"},
    {"name": "知乎评论", "table": "zhihu_comment", "kind": "comment", "parent_col": "content_id", "platform": "zhihu"},
]

# 需要统一格式化的时间字段（契约 §3）
TIME_FIELDS = {
    "add_ts",
    "last_modify_ts",
    "time",
    "create_time",
    "created_time",
    "updated_time",
    "publish_time",
    "last_update_time",
}

_OVERVIEW_SHEET = "概览"
_EXCEL_SERIAL_MIN = 20000   # 1954 年之后
_EXCEL_SERIAL_MAX = 80000   # 2119 年之前
_MAX_CELL_TEXT = 32000      # Excel 单元格上限 32767，留余量
# Excel/XML 非法控制字符（与 openpyxl ILLEGAL_CHARACTERS_RE 一致；额外把 0x7F 也替换为空格）
_ILLEGAL_EXCEL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 需要强制按“文本”写入、避免被 Excel/openpyxl 当公式的起始字符
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def sanitize_excel_text(text: str) -> str:
    """
    清洗 Excel/XML 非法控制字符（替换为空格），保留 \\t/\\n/\\r 与 emoji。

    非法字符集合与 openpyxl 的 `ILLEGAL_CHARACTERS_RE` 一致：
    0x00-0x08、0x0B、0x0C、0x0E-0x1F（本实现额外处理 0x7F）。
    2026-11-09 修复：贴吧原文含 \\x18 导致 openpyxl 抛 IllegalCharacterError、导出失败。
    """
    if not isinstance(text, str):
        return text
    return _ILLEGAL_EXCEL_CHARS_RE.sub(" ", text)


def _write_cell_safe(ws, row_index: int, column_index: int, value: Any):
    """安全写单元格：清洗非法字符；以 =/+/ -/@ 开头的字符串强制按文本写入（防公式注入）。"""
    cell = ws.cell(row=row_index, column=column_index)
    if isinstance(value, str):
        safe = sanitize_excel_text(value)
        cell.value = safe
        if safe.startswith(_FORMULA_PREFIXES):
            cell.data_type = "s"
            cell.number_format = "@"
    else:
        cell.value = value
    return cell
_FILENAME_MAX_KEYWORD = 48


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def normalize_keywords(keywords: Any) -> List[str]:
    """把 sidecar 传入的关键词统一为去空白的字符串列表（容忍 str / 逗号分隔）。"""
    if keywords is None:
        return []
    if isinstance(keywords, str):
        parts = re.split(r"[,，、;；\n]+", keywords)
    else:
        parts = []
        for item in keywords:
            if isinstance(item, str):
                parts.extend(re.split(r"[,，、;；\n]+", item))
            elif item is not None:
                parts.append(str(item))
    result: List[str] = []
    for part in parts:
        text = str(part).strip()
        if text and text not in result:
            result.append(text)
    return result


def _to_datetime(value: Any) -> Optional[datetime]:
    """把各种时间表示统一为 datetime（无法识别返回 None）。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        return _numeric_to_datetime(float(value))

    text = str(value).strip()
    if not text or text.lower() in ("none", "null", "nan"):
        return None

    # 纯数字字符串：可能是时间戳、Excel 序列号，也可能是 YYYYMMDD / YYYYMMDDHHMMSS
    if re.fullmatch(r"\d+", text):
        if len(text) == 8:
            try:
                return datetime.strptime(text, "%Y%m%d")
            except ValueError:
                pass
        if len(text) == 14:
            try:
                return datetime.strptime(text, "%Y%m%d%H%M%S")
            except ValueError:
                pass
        return _numeric_to_datetime(float(text))
    if re.fullmatch(r"\d+\.\d+", text):
        return _numeric_to_datetime(float(text))

    iso_text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y.%m.%d %H:%M:%S",
        "%Y.%m.%d",
        "%Y%m%d",
        "%Y%m%d%H%M%S",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _numeric_to_datetime(num: float) -> Optional[datetime]:
    if num != num or num <= 0:  # NaN / 非正数
        return None
    # Excel 日期序列号
    if _EXCEL_SERIAL_MIN <= num <= _EXCEL_SERIAL_MAX:
        return datetime(1899, 12, 30) + timedelta(days=num)
    # 微秒 / 毫秒 / 秒 时间戳
    if num >= 1e15:
        num /= 1_000_000.0
    elif num >= 1e12:
        num /= 1000.0
    if num < 1e9:  # 不是合理时间戳
        return None
    try:
        return datetime.fromtimestamp(num)
    except (OverflowError, OSError, ValueError):
        return None


def _numeric_to_epoch_ms(num: float) -> Optional[int]:
    """数值 → 毫秒时间戳（自动识别微秒/毫秒/秒/Excel 序列号）。"""
    if num != num or num <= 0:  # NaN / 非正数
        return None
    if _EXCEL_SERIAL_MIN <= num <= _EXCEL_SERIAL_MAX:
        serial_dt = datetime(1899, 12, 30) + timedelta(days=num)
        return int(round(serial_dt.timestamp() * 1000))
    if num >= 1e15:          # 微秒级时间戳 → 毫秒
        return int(round(num / 1000.0))
    if num >= 1e12:          # 已是毫秒
        return int(round(num))
    if num >= 1e9:           # 秒级
        return int(round(num * 1000.0))
    return None


def _to_epoch_ms(value: Any) -> Optional[int]:
    """
    任意时间表示 → 毫秒时间戳；无法识别 / 空 / 0 / 负数返回 None。

    这个函数是“本次运行过滤”的单位归一核心：真实 MediaCrawler 的
    `last_modify_ts`/`add_ts` 是毫秒（如 1794190674936），而 sidecar 的
    `started_ts` 是秒（如 1794190619）；旧库/部分平台可能是秒或字符串。
    """
    if value is None or value == "" or value is False:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return int(round(value.timestamp() * 1000))
    if isinstance(value, date):
        return int(round(datetime(value.year, value.month, value.day).timestamp() * 1000))
    if isinstance(value, (int, float)):
        return _numeric_to_epoch_ms(float(value))

    text = str(value).strip()
    if not text or text.lower() in ("none", "null", "nan", "0", "0.0", "-1"):
        return None
    if re.fullmatch(r"\d+", text):
        if len(text) == 8:
            try:
                return int(round(datetime.strptime(text, "%Y%m%d").timestamp() * 1000))
            except ValueError:
                pass
        if len(text) == 14:
            try:
                return int(round(datetime.strptime(text, "%Y%m%d%H%M%S").timestamp() * 1000))
            except ValueError:
                pass
        return _numeric_to_epoch_ms(float(text))
    if re.fullmatch(r"\d+\.\d+", text):
        return _numeric_to_epoch_ms(float(text))
    parsed = _to_datetime(text)
    return int(round(parsed.timestamp() * 1000)) if parsed is not None else None


def format_time(value: Any) -> str:
    """时间字段 → `YYYY-MM-DD HH:MM:SS`；无法识别时原样返回字符串（简单容错）。"""
    if value is None or value == "":
        return ""
    parsed = _to_datetime(value)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    text = str(value).strip()
    if text in ("0", "0.0", "-1"):
        return ""  # 缺失时间的常见占位值
    return sanitize_excel_text(text)


def format_datetime(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _is_id_column(column: str) -> bool:
    """ID 列判定：`id` 本身或 `*_id`（note_id / aweme_id / video_id / content_id / comment_id / user_id ...）。"""
    return column == "id" or column.endswith("_id")


def _to_text(value: Any) -> str:
    """ID 值 → 文本，避免 19 位数字变成科学计数法；统一清洗 Excel 非法字符。"""
    if value is None:
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, bytes):
        return sanitize_excel_text(value.decode("utf-8", errors="replace"))
    return sanitize_excel_text(str(value))


def _to_cell_value(value: Any) -> Any:
    """普通值 → Excel 单元格值（None 空串，bytes 解码，先清洗非法字符再超长截断）。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        value = sanitize_excel_text(value)
        if len(value) > _MAX_CELL_TEXT:
            return value[:_MAX_CELL_TEXT]
        return value
    return value


def _to_json_value(value: Any) -> Any:
    """预览用：保证可 JSON 序列化，None → 空串；统一清洗 Excel 非法字符。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return sanitize_excel_text(value.decode("utf-8", errors="replace"))
    if isinstance(value, (datetime, date)):
        return format_datetime(value) if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, str):
        return sanitize_excel_text(value)
    if isinstance(value, (int, float, bool)):
        return value
    return sanitize_excel_text(str(value))


# ---------------------------------------------------------------------------
# SQLite 读取
# ---------------------------------------------------------------------------
def _db_uri(db_path: str) -> str:
    absolute = Path(db_path).resolve()
    quoted = quote(absolute.as_posix(), safe="/:")
    return f"file:{quoted}?mode=ro"


def open_readonly(db_path: str) -> sqlite3.Connection:
    """只读打开 MediaCrawler sqlite；文件不存在时抛 FileNotFoundError。"""
    if not db_path:
        raise ValueError("db_path 不能为空")
    if not os.path.isfile(db_path):
        raise FileNotFoundError(f"MediaCrawler 数据库不存在：{db_path}")
    conn = sqlite3.connect(_db_uri(db_path), uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row["name"] for row in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _read_rows(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    cursor = conn.execute(f'SELECT * FROM "{table}"')
    return [dict(row) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# 过滤（按“本次运行更新时间”而不是“首次入库时间”）
# ---------------------------------------------------------------------------
# 口径说明（2026-11-09 修复：UI saved 100 但 Excel 只有 1 条）：
#   - MediaCrawler 的 `add_ts` 是**首次入库时间**：同一篇笔记被重复采集时不会变化；
#   - `last_modify_ts` 是**最后一次写入/更新时间**：本次运行重新采集/更新的行会变成运行时间；
#   - 因此“本次运行新增+更新的数据”应使用 `last_modify_ts >= 运行开始时间` 过滤，
#     否则历史已采内容会被 add_ts 过滤掉（旧 bug 的根因）。
#   - `last_modify_ts` 缺失/为 0 时回退 `add_ts`（兼容旧库）。
FILTER_RULE_DESCRIPTION = "last_modify_ts >= 采集开始时间（毫秒归一；缺失/0 回退 add_ts）；评论另需父内容命中本次更新"

# ---------------------------------------------------------------------------
# 相关性标记（2026-11-09 新增：防止平台搜索返回泛内容污染数据/报告）
# ---------------------------------------------------------------------------
# 口径：按 `source_keyword`（多个关键词用逗号/空格/顿号拆分）判断内容文本
#       （title/desc/content/content_text/nickname/user_nickname）是否命中任一 token：
#         - 命中 → filtered_reason = ""（相关）
#         - 未命中 → filtered_reason = "off_brand"（疑似泛内容）
#       **只标记、不删除行**，报告/分析阶段再按 off_brand 排除。
#       评论表的 filtered_reason 按父内容继承（父内容 off_brand → 评论也 off_brand）。
FILTERED_REASON_COLUMN = "filtered_reason"
OFF_BRAND = "off_brand"
_COMPUTED_REASON_KEY = "__computed_filtered_reason__"
# 情感判定列（2026-11-09 新增；值来自 app.db sentiment_cache，供报告/Excel 共用）
SENTIMENT_COLUMN = "sentiment"
SENTIMENT_REASON_COLUMN = "sentiment_reason"
_SENTIMENT_CN_KEY = "__sentiment_cn__"
_SENTIMENT_REASON_KEY = "__sentiment_reason__"
_SENTIMENT_CN = {"positive": "正面", "neutral": "中性", "negative": "负面"}
_SENTIMENT_ALIASES = {
    "positive": "positive", "pos": "positive", "正面": "positive", "正向": "positive", "积极": "positive",
    "negative": "negative", "neg": "negative", "负面": "negative", "负向": "negative", "消极": "negative",
    "neutral": "neutral", "neu": "neutral", "中性": "neutral", "中立": "neutral",
}
RELEVANCE_RULE_DESCRIPTION = (
    "按 source_keyword（逗号/空格/顿号拆分）在 title/desc/正文/nickname 中命中任一关键词 token → 相关（空）；"
    "未命中 → off_brand；评论按父内容继承；M1 只标记不删除行，报告阶段再排除"
)
RELEVANCE_TEXT_FIELDS = ("title", "desc", "content", "content_text", "nickname", "user_nickname")
_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；\s|]+")


def keyword_tokens(source_keyword: Any) -> List[str]:
    """把 source_keyword 拆成关键词 token（逗号/空格/顿号/分号/竖线分隔）。"""
    if source_keyword is None:
        return []
    return [token for token in _KEYWORD_SPLIT_RE.split(str(source_keyword)) if token.strip()]


def content_filtered_reason(row: Dict[str, Any]) -> str:
    """
    判定内容行与品牌关键词的相关性：
      - 命中 source_keyword 任一 token → ""；
      - 未命中（含 source_keyword 为空/缺失）→ "off_brand"。
    匹配范围：title/desc/正文/nickname（大小写不敏感，兼容英文关键词）。
    """
    tokens = keyword_tokens(row.get("source_keyword"))
    if not tokens:
        return OFF_BRAND
    haystack = "\n".join(
        str(row.get(field) or "")
        for field in RELEVANCE_TEXT_FIELDS
        if field in row and row.get(field) not in (None, "")
    ).lower()
    if not haystack:
        return OFF_BRAND
    for token in tokens:
        if token.strip().lower() in haystack:
            return ""
    return OFF_BRAND


def _final_reason(row: Dict[str, Any], has_db_column: bool) -> str:
    """取行最终输出的 filtered_reason（兼容源库已有该列的情况）。"""
    if has_db_column:
        return str(row.get(FILTERED_REASON_COLUMN) or "")
    return str(row.get(_COMPUTED_REASON_KEY) or "")


def _annotate_reason(row: Dict[str, Any], reason: str, has_db_column: bool) -> str:
    """把相关性标记写入行：源库已有 filtered_reason 列时沿用非空值，否则写计算值。"""
    if has_db_column:
        existing = str(row.get(FILTERED_REASON_COLUMN) or "").strip()
        final = existing or reason
        row[FILTERED_REASON_COLUMN] = final
        return final
    row[_COMPUTED_REASON_KEY] = reason
    return reason


def _pairs_with_reason(db_columns: Sequence[str]) -> Tuple[List[Tuple[str, str]], bool]:
    """
    输出列 = 数据库列（creator_hash→user_id）+ filtered_reason + sentiment + sentiment_reason。

    `filtered_reason` 若源库已有则沿用，避免重复；`sentiment/sentiment_reason` 两列固定追加在最后
    （2026-11-09 新增，值来自 app.db 的 sentiment_cache）。
    """
    pairs = output_columns(db_columns)
    has_db_column = FILTERED_REASON_COLUMN in [pair[0] for pair in pairs]
    if not has_db_column:
        pairs = pairs + [(FILTERED_REASON_COLUMN, _COMPUTED_REASON_KEY)]
    pairs = pairs + [
        (SENTIMENT_COLUMN, _SENTIMENT_CN_KEY),
        (SENTIMENT_REASON_COLUMN, _SENTIMENT_REASON_KEY),
    ]
    return pairs, has_db_column


def _sentiment_cn(value: Any) -> str:
    """缓存中的情感值（英文/中文）→ Excel 中文标签；未知/空返回空串。"""
    if value is None:
        return ""
    text = re.sub(r"[\[\]【】()（）\s]", "", str(value)).strip().lower()
    canonical = _SENTIMENT_ALIASES.get(text)
    return _SENTIMENT_CN.get(canonical, "") if canonical else ""


def _resolve_report_app_db(db_path: str, app_db_path: Optional[str]) -> str:
    """定位 app.db（情感缓存）：显式参数优先，否则 <db_path> 的上两级/app.db。"""
    if app_db_path:
        return os.path.abspath(app_db_path)
    try:
        return str(Path(db_path).resolve().parent.parent / "app.db")
    except OSError:
        return ""


def _load_sentiment_cache(app_db_path: str) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """读取情感缓存（复用 report_data.load_sentiment_cache；失败/未就绪返回空字典）。"""
    if not app_db_path:
        return {}
    try:
        from report_data import load_sentiment_cache  # 同目录模块，延迟导入避免耦合/循环
        return load_sentiment_cache(app_db_path)
    except Exception:  # noqa: BLE001 - 没有缓存不应影响 Excel 导出
        return {}


def _annotate_sentiment(row: Dict[str, Any], *, platform_code: str, kind: str, item_id: Any,
                        cache: Dict[Tuple[str, str, str], Dict[str, Any]]) -> None:
    """把 sentiment_cache 中的情感/原因写入行（无缓存则留空）。"""
    item_key = _to_text(item_id)
    cached = cache.get((platform_code, kind, item_key)) or {}
    sentiment_cn = _sentiment_cn(cached.get("sentiment"))
    row[_SENTIMENT_CN_KEY] = sentiment_cn
    row[_SENTIMENT_REASON_KEY] = str(cached.get("reason") or "") if sentiment_cn else ""


def _sentiment_counts(collected: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """统计本次导出数据中“已情感分析/未分析”行数。"""
    analyzed = 0
    unanalyzed = 0
    for item in collected:
        for row in item["rows"]:
            if str(row.get(_SENTIMENT_CN_KEY) or ""):
                analyzed += 1
            else:
                unanalyzed += 1
    return {"analyzed": analyzed, "unanalyzed": unanalyzed}


def _relevance_totals(collected: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """统计导出数据的相关性：相关行数 / off_brand 行数。"""
    relevant = 0
    off_brand = 0
    for item in collected:
        has_db_column = bool(item.get("has_reason_db_column"))
        for row in item["rows"]:
            if _final_reason(row, has_db_column) == OFF_BRAND:
                off_brand += 1
            else:
                relevant += 1
    return {"relevant": relevant, "off_brand": off_brand}


def _row_updated_in_run(row: Dict[str, Any], started_ms: Optional[int]) -> bool:
    """
    判断行是否属于“本次运行新增或更新”。

    优先 `last_modify_ts`；该列缺失 / 值为 0 / 无法解析时回退 `add_ts`。
    两列都缺失时不过滤（保持旧行为，避免把整表误剔除）。
    """
    if started_ms is None:
        return True
    if "last_modify_ts" in row:
        modified_ms = _to_epoch_ms(row.get("last_modify_ts"))
        if modified_ms is not None:  # 存在且非 0
            return modified_ms >= started_ms
    if "add_ts" in row:
        added_ms = _to_epoch_ms(row.get("add_ts"))
        if added_ms is None:
            return False
        return added_ms >= started_ms
    return True


def _filter_content_rows(rows: List[Dict[str, Any]], keywords: Sequence[str],
                         started_ms: Optional[int]) -> List[Dict[str, Any]]:
    keyword_set = set(keywords)
    result = []
    for row in rows:
        if keyword_set:
            if "source_keyword" in row:
                value = row.get("source_keyword")
                if value is None or str(value).strip() not in keyword_set:
                    continue
        if not _row_updated_in_run(row, started_ms):
            continue
        result.append(row)
    return result


def _filter_comment_rows(rows: List[Dict[str, Any]], parent_col: str,
                         parent_ids: set, started_ms: Optional[int]) -> List[Dict[str, Any]]:
    result = []
    for row in rows:
        if not _row_updated_in_run(row, started_ms):
            continue
        if parent_col not in row:
            continue
        parent = row.get(parent_col)
        if parent is None:
            continue
        if _to_text(parent) in parent_ids:
            result.append(row)
    return result


# ---------------------------------------------------------------------------
# 列处理
# ---------------------------------------------------------------------------
def output_columns(db_columns: Sequence[str]) -> List[Tuple[str, str]]:
    """
    数据库列 → (输出列名, 数据库列名)。

    - `creator_hash` → 输出为 `user_id`（M1 不脱敏，raw ID）；
    - 若同时存在 `creator_hash` 与 `user_id`（迁移期库），优先 creator_hash，避免重复列。
    """
    pairs: List[Tuple[str, str]] = []
    has_creator_hash = "creator_hash" in db_columns
    for column in db_columns:
        if column == "creator_hash":
            pairs.append(("user_id", column))
        elif column == "user_id" and has_creator_hash:
            continue
        else:
            pairs.append((column, column))
    return pairs


def _convert_row(row: Dict[str, Any], pairs: Sequence[Tuple[str, str]]) -> List[Any]:
    values: List[Any] = []
    for out_name, db_column in pairs:
        value = row.get(db_column)
        if db_column in TIME_FIELDS:
            values.append(format_time(value))
        elif _is_id_column(out_name) or _is_id_column(db_column):
            values.append(_to_text(value))
        else:
            values.append(_to_cell_value(value))
    return values


def _convert_preview_row(row: Dict[str, Any], pairs: Sequence[Tuple[str, str]]) -> List[Any]:
    values: List[Any] = []
    for out_name, db_column in pairs:
        value = row.get(db_column)
        if db_column in TIME_FIELDS:
            values.append(format_time(value))
        elif _is_id_column(out_name) or _is_id_column(db_column):
            values.append(_to_text(value))
        else:
            values.append(_to_json_value(value))
    return values


# ---------------------------------------------------------------------------
# 数据收集（导出与预览共用）
# ---------------------------------------------------------------------------
def _collect(db_path: str, keywords: Sequence[str], started_ts: Any,
             app_db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    按契约顺序读取 14 张表并过滤，返回：
      [{"sheet": 名称, "table": 表名, "kind": ..., "pairs": [(输出列, db列)],
        "rows": [db row...]}]
    表不存在 → 从结果中跳过。

    过滤口径：`last_modify_ts >= started_ts`（毫秒归一；缺失/0 回退 `add_ts`），
    内容表另加 `source_keyword ∈ keywords`；评论表的父内容集合用同一口径得到。
    每个数据行会附加 filtered_reason 与 sentiment/sentiment_reason 注解（来自 app.db 情感缓存）。
    """
    keywords = normalize_keywords(keywords)
    started_ms = _to_epoch_ms(started_ts)
    cache = _load_sentiment_cache(app_db_path or "")
    collected: List[Dict[str, Any]] = []
    content_ids: Dict[str, set] = {}
    parent_reasons: Dict[str, Dict[str, str]] = {}

    conn = open_readonly(db_path)
    try:
        for spec in SHEETS:
            table = spec["table"]
            if not _table_exists(conn, table):
                continue
            db_columns = _table_columns(conn, table)
            rows = _read_rows(conn, table)
            pairs, has_reason_db_column = _pairs_with_reason(db_columns)
            if spec["kind"] == "content":
                filtered = _filter_content_rows(rows, keywords, started_ms)
                id_col = spec["id_col"]
                id_to_reason: Dict[str, str] = {}
                parent_set = set()
                for row in filtered:
                    reason = _annotate_reason(row, content_filtered_reason(row), has_reason_db_column)
                    row_id = _to_text(row.get(id_col))
                    if row.get(id_col) is not None:
                        parent_set.add(row_id)
                    id_to_reason[row_id] = reason
                    _annotate_sentiment(row, platform_code=spec["platform"], kind="content",
                                        item_id=row.get(id_col), cache=cache)
                content_ids[spec["platform"]] = parent_set
                parent_reasons[spec["platform"]] = id_to_reason
            else:
                parent_set = content_ids.get(spec["platform"], set())
                filtered = _filter_comment_rows(rows, spec["parent_col"], parent_set, started_ms)
                reasons = parent_reasons.get(spec["platform"], {})
                for row in filtered:
                    parent_id = _to_text(row.get(spec["parent_col"]))
                    _annotate_reason(row, reasons.get(parent_id, ""), has_reason_db_column)
                    _annotate_sentiment(row, platform_code=spec["platform"], kind="comment",
                                        item_id=row.get("comment_id"), cache=cache)
            collected.append(
                {
                    "sheet": spec["name"],
                    "table": table,
                    "kind": spec["kind"],
                    "pairs": pairs,
                    "rows": filtered,
                    "has_reason_db_column": has_reason_db_column,
                }
            )
    finally:
        conn.close()
    return collected


def _total_rows(collected: Sequence[Dict[str, Any]]) -> int:
    return sum(len(item["rows"]) for item in collected)


# ---------------------------------------------------------------------------
# 文件名
# ---------------------------------------------------------------------------
_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_keyword(keyword: str) -> str:
    text = sanitize_excel_text(str(keyword or ""))
    text = _ILLEGAL_FILENAME_CHARS.sub("_", text).strip().strip(".")
    text = text[:_FILENAME_MAX_KEYWORD].strip()
    return text or "舆情"


def _build_output_path(out_dir: str, keywords: Sequence[str], now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    exports_dir = os.path.join(os.path.abspath(out_dir), "exports")
    os.makedirs(exports_dir, exist_ok=True)
    keyword = _safe_keyword(keywords[0] if keywords else "舆情")
    base = f"{keyword}_舆情数据_{now.strftime('%Y%m%d_%H%M%S')}"
    candidate = os.path.join(exports_dir, base + ".xlsx")
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(exports_dir, f"{base}_{index}.xlsx")
        index += 1
    return candidate


# ---------------------------------------------------------------------------
# 概览 sheet
# ---------------------------------------------------------------------------
_HEADER_FILL = PatternFill("solid", fgColor="1A5FB4")
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_META_FONT = Font(bold=True, color="124A8F")
_TITLE_FONT = Font(bold=True, size=13, color="124A8F")
_THIN_ALIGN = Alignment(vertical="top", wrap_text=False)


def _style_header(ws, row_index: int, columns: int) -> None:
    for col_index in range(1, columns + 1):
        cell = ws.cell(row=row_index, column=col_index)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _write_overview(ws, *, run_id: str, keywords: Sequence[str], started_dt: Optional[datetime],
                    exported_at: datetime, sheet_rows: Sequence[Tuple[str, int]],
                    total_rows: int, relevance: Optional[Dict[str, int]] = None,
                    sentiment_counts: Optional[Dict[str, int]] = None) -> None:
    _write_cell_safe(ws, 1, 1, "舆见舆情数据导出概览").font = _TITLE_FONT
    ws.merge_cells("A1:B1")

    relevance = relevance or {"relevant": 0, "off_brand": 0}
    sentiment_counts = sentiment_counts or {"analyzed": 0, "unanalyzed": 0}
    meta: List[Tuple[str, Any]] = [
        ("run_id", str(run_id)),
        ("关键词", "、".join(keywords)),
        ("采集开始时间", format_datetime(started_dt) if started_dt else ""),
        ("导出时间", format_datetime(exported_at)),
        ("过滤口径", FILTER_RULE_DESCRIPTION),
        ("相关行数", int(relevance.get("relevant", 0))),
        ("off_brand 行数", int(relevance.get("off_brand", 0))),
        ("相关性判定规则", RELEVANCE_RULE_DESCRIPTION),
        ("已情感分析行数", int(sentiment_counts.get("analyzed", 0))),
        ("未分析行数", int(sentiment_counts.get("unanalyzed", 0))),
    ]
    row_index = 3
    for label, value in meta:
        _write_cell_safe(ws, row_index, 1, label).font = _META_FONT
        _write_cell_safe(ws, row_index, 2, value)
        row_index += 1

    row_index += 1
    _write_cell_safe(ws, row_index, 1, "工作表")
    _write_cell_safe(ws, row_index, 2, "行数")
    _style_header(ws, row_index, 2)
    row_index += 1
    for name, count in sheet_rows:
        _write_cell_safe(ws, row_index, 1, name)
        _write_cell_safe(ws, row_index, 2, int(count))
        row_index += 1
    _write_cell_safe(ws, row_index, 1, "合计").font = _META_FONT
    _write_cell_safe(ws, row_index, 2, int(total_rows)).font = _META_FONT

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 60
    ws.freeze_panes = "A3"


# ---------------------------------------------------------------------------
# 写 Excel
# ---------------------------------------------------------------------------
def _write_data_sheet(wb: Workbook, name: str, pairs: Sequence[Tuple[str, str]],
                      rows: Sequence[Dict[str, Any]]) -> None:
    ws = wb.create_sheet(title=name)
    headers = [pair[0] for pair in pairs]
    ws.append(headers)
    # 表头也过安全清洗（列名来自数据库，正常无控制字符；统一入口防意外）
    for col_index, header in enumerate(headers, start=1):
        _write_cell_safe(ws, 1, col_index, sanitize_excel_text(str(header)))
    # ID 列按“输出列名”判定（creator_hash 已改名为 user_id，也要按文本写入）
    id_column_indexes = [idx for idx, pair in enumerate(pairs, start=1) if _is_id_column(pair[0])]

    if rows:
        for row in rows:
            ws.append(_convert_row(row, pairs))
        # ID 列强制文本格式，避免科学计数法/自动转数字
        for row_index in range(2, len(rows) + 2):
            for col_index in id_column_indexes:
                cell = ws.cell(row=row_index, column=col_index)
                cell.number_format = "@"
                cell.alignment = _THIN_ALIGN
        # 防公式注入：以 =/+/-/@ 开头的文本强制 data_type='s'（Excel 显示原文，不执行公式）
        for row_index in range(2, len(rows) + 2):
            for cell in ws[row_index]:
                if isinstance(cell.value, str) and cell.value.startswith(_FORMULA_PREFIXES):
                    cell.data_type = "s"
                    cell.number_format = "@"

    _style_header(ws, 1, max(len(headers), 1))
    ws.freeze_panes = "A2"
    if headers:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"

    # 列宽：按表头与前 60 行估算，最长 50
    for col_index, header in enumerate(headers, start=1):
        max_len = len(str(header))
        for row in rows[:60]:
            value = row.get(pairs[col_index - 1][1])
            text = "" if value is None else str(value)
            if len(text) > max_len:
                max_len = len(text)
        ws.column_dimensions[get_column_letter(col_index)].width = min(max(10, max_len + 2), 50)


def export_run_excel(db_path: str, out_dir: str, run_id: str, keywords: Any,
                     started_ts: Any, app_db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    导出一次采集结果为多 sheet Excel。

    参数：
      db_path   MediaCrawler sqlite 路径（只读打开）；
      out_dir   输出根目录，Excel 落在 `<out_dir>/exports/`；
      run_id    采集任务 ID（写入概览）；
      keywords  关键词列表（内容表 source_keyword 过滤，评论表通过父内容过滤）；
      started_ts 采集开始时间（秒级时间戳；兼容毫秒/字符串），
                 用于 `last_modify_ts >= started_ts`（缺失/0 回退 add_ts）过滤
                 “本次运行新增+更新”的数据；
      app_db_path 可选：情感缓存 app.db 路径（默认由 db_path 推导 `<data-dir>/app.db`）。

    返回：
      {"file_path": str, "sheets": [{"name": str, "rows": int}], "total_rows": int}

    说明：每个数据 sheet 末尾会追加 `filtered_reason` 相关性标记列，以及
    `sentiment`（正面/中性/负面）/`sentiment_reason` 情感列（来自 app.db sentiment_cache，无缓存留空）；
    概览包含相关行数 / off_brand 行数 / 已情感分析行数 / 未分析行数 / 判定规则。
    """
    keyword_list = normalize_keywords(keywords)
    started_dt = _to_datetime(started_ts) if started_ts not in (None, "", 0) else None
    resolved_app_db = _resolve_report_app_db(db_path, app_db_path)
    collected = _collect(db_path, keyword_list, started_ts, app_db_path=resolved_app_db)
    total_rows = _total_rows(collected)
    relevance = _relevance_totals(collected)
    sentiment_counts = _sentiment_counts(collected)

    wb = Workbook()
    overview = wb.active
    overview.title = _OVERVIEW_SHEET
    _write_overview(
        overview,
        run_id=str(run_id),
        keywords=keyword_list,
        started_dt=started_dt,
        exported_at=datetime.now(),
        sheet_rows=[(item["sheet"], len(item["rows"])) for item in collected],
        total_rows=total_rows,
        relevance=relevance,
        sentiment_counts=sentiment_counts,
    )

    sheets_result: List[Dict[str, Any]] = []
    for item in collected:
        _write_data_sheet(wb, item["sheet"], item["pairs"], item["rows"])
        sheets_result.append({"name": item["sheet"], "rows": len(item["rows"])})

    file_path = _build_output_path(out_dir, keyword_list)
    wb.save(file_path)
    return {"file_path": file_path, "sheets": sheets_result, "total_rows": total_rows}


def preview_run(db_path: str, keywords: Any, started_ts: Any, limit: int = 20,
                app_db_path: Optional[str] = None) -> Dict[str, Any]:
    """
    预览一次采集结果（供 sidecar `GET /api/runs/{run_id}/data` 使用）。

    过滤口径与 `export_run_excel` 完全一致：
      `last_modify_ts >= started_ts`（缺失/0 回退 add_ts），评论与父内容集合同口径。

    返回：
      {"sheets": [{"name": str, "columns": [str], "rows": [[...]]}]}
    - 只返回过滤后**有数据**的 sheet（无数据的平台不展示）；
    - 每 sheet 最多 `limit` 行；ID 为文本、时间已格式化、值可 JSON 序列化；
    - 列末尾依次包含 `filtered_reason` 与 `sentiment`/`sentiment_reason`（与 Excel 导出口径一致）。
    """
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = 20
    limit_int = max(0, min(limit_int, 5000))

    resolved_app_db = _resolve_report_app_db(db_path, app_db_path)
    collected = _collect(db_path, normalize_keywords(keywords), started_ts, app_db_path=resolved_app_db)
    sheets_result: List[Dict[str, Any]] = []
    for item in collected:
        rows = item["rows"][:limit_int]
        if not rows:
            continue
        sheets_result.append(
            {
                "name": item["sheet"],
                "columns": [pair[0] for pair in item["pairs"]],
                "rows": [_convert_preview_row(row, item["pairs"]) for row in rows],
            }
        )
    return {"sheets": sheets_result}


# 兼容契约 §2.2 中的 `exporter.preview` 与旧命名
preview = preview_run
export_run = export_run_excel


__all__ = [
    "SHEETS",
    "TIME_FIELDS",
    "FILTERED_REASON_COLUMN",
    "SENTIMENT_COLUMN",
    "SENTIMENT_REASON_COLUMN",
    "OFF_BRAND",
    "RELEVANCE_RULE_DESCRIPTION",
    "export_run_excel",
    "preview_run",
    "preview",
    "export_run",
    "normalize_keywords",
    "format_time",
    "sanitize_excel_text",
    "output_columns",
    "keyword_tokens",
    "content_filtered_reason",
    "open_readonly",
]

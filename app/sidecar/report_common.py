# -*- coding: utf-8 -*-
"""
report_common.py — 报告数据层公共底座（平台注册表 / 时间文本工具 / 只读 DB / 流水线定位）

`report_data.py` 拆分后（P1-1）三个子模块共用这里的东西，避免复制粘贴：

  - `report_dataset.py`  SQLite → NormalizedDataset（窗口过滤、去重、相关性）
  - `report_package.py`  `public-sentiment-data@v1` 组装 + schema 校验
  - `report_sentiment.py` 情感批处理 / 缓存 / neutral 兜底

本模块**不做任何 IO 副作用**（不连库、不建表、不读环境变量以外的状态），可在测试里安全导入。

平台注册表（唯一来源）：
  `DEFAULT_PLATFORMS` / `PLATFORM_LABELS` / `LABEL_TO_CODE` —— 前端、导出器
  （`export_specs`）与报告层都从这里取；新增平台先改 `CONTENT_SPECS` / `COMMENT_SPECS`，
  再改 `PLATFORM_LABELS`。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "public-sentiment-data@v1"
DB_SCHEMA = "media_crawler@2026-11"
TIMEZONE = "Asia/Shanghai"
TZ = timezone(timedelta(hours=8))

DEFAULT_PLATFORMS: List[str] = ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]
PLATFORM_LABELS: Dict[str, str] = {
    "xhs": "小红书",
    "dy": "抖音",
    "ks": "快手",
    "bili": "B站",
    "wb": "微博",
    "tieba": "贴吧",
    "zhihu": "知乎",
}
LABEL_TO_CODE: Dict[str, str] = {label: code for code, label in PLATFORM_LABELS.items()}

SENTIMENT_CN = {"positive": "正面", "neutral": "中性", "negative": "负面"}
_SENTIMENT_ALIASES = {
    "positive": "positive", "pos": "positive", "正面": "positive", "正向": "positive", "积极": "positive",
    "negative": "negative", "neg": "negative", "负面": "negative", "负向": "negative", "消极": "negative",
    "neutral": "neutral", "neu": "neutral", "中性": "neutral", "中立": "neutral",
}

# 各平台内容表/评论表字段约定（来自 MediaCrawler/database/models.py 当前 ORM）
CONTENT_SPECS: Dict[str, Dict[str, Any]] = {
    "xhs": {
        "table": "xhs_note", "id_col": "note_id",
        "time_cols": ["time", "last_update_time"],
        "title_cols": ["title"], "text_cols": ["title", "desc"],
        "author_cols": ["nickname"], "url_col": "note_url",
        "interactions": {"likes": "liked_count", "comments": "comment_count",
                         "collects": "collected_count", "shares": "share_count"},
    },
    "dy": {
        "table": "douyin_aweme", "id_col": "aweme_id",
        "time_cols": ["create_time"],
        "title_cols": ["title"], "text_cols": ["title", "desc"],
        "author_cols": ["nickname"], "url_col": "aweme_url",
        "interactions": {"likes": "liked_count", "comments": "comment_count",
                         "shares": "share_count", "collects": "collected_count"},
    },
    "ks": {
        "table": "kuaishou_video", "id_col": "video_id",
        "time_cols": ["create_time"],
        "title_cols": ["title"], "text_cols": ["title", "desc"],
        "author_cols": ["nickname"], "url_col": "video_url",
        "interactions": {"likes": "liked_count", "plays": "viewd_count"},
    },
    "bili": {
        "table": "bilibili_video", "id_col": "video_id",
        "time_cols": ["create_time"],
        "title_cols": ["title"], "text_cols": ["title", "desc"],
        "author_cols": ["nickname"], "url_col": "video_url",
        "interactions": {"likes": "liked_count", "comments": "video_comment",
                         "shares": "video_share_count", "collects": "video_favorite_count",
                         "coins": "video_coin_count", "plays": "video_play_count"},
    },
    "wb": {
        "table": "weibo_note", "id_col": "note_id",
        "time_cols": ["create_time", "create_date_time"],
        "title_cols": [], "text_cols": ["content"],
        "author_cols": ["nickname"], "url_col": "note_url",
        "interactions": {"likes": "liked_count", "comments": "comments_count",
                         "shares": "shared_count"},
    },
    "tieba": {
        "table": "tieba_note", "id_col": "note_id",
        "time_cols": ["publish_time", "add_ts"],
        "title_cols": ["title"], "text_cols": ["title", "desc"],
        "author_cols": ["user_nickname", "nickname"], "url_col": "note_url",
        "interactions": {"comments": "total_replay_num"},
    },
    "zhihu": {
        "table": "zhihu_content", "id_col": "content_id",
        "time_cols": ["created_time"],
        "title_cols": ["title"], "text_cols": ["title", "desc", "content_text"],
        "author_cols": ["user_nickname", "nickname"], "url_col": "content_url",
        "interactions": {"likes": "voteup_count", "comments": "comment_count"},
    },
}

COMMENT_SPECS: Dict[str, Dict[str, Any]] = {
    "xhs": {"table": "xhs_note_comment", "id_col": "comment_id", "parent_col": "note_id",
            "time_cols": ["create_time"], "text_col": "content", "like_col": "like_count"},
    "dy": {"table": "douyin_aweme_comment", "id_col": "comment_id", "parent_col": "aweme_id",
           "time_cols": ["create_time"], "text_col": "content", "like_col": "like_count"},
    "ks": {"table": "kuaishou_video_comment", "id_col": "comment_id", "parent_col": "video_id",
           "time_cols": ["create_time"], "text_col": "content", "like_col": None},
    "bili": {"table": "bilibili_video_comment", "id_col": "comment_id", "parent_col": "video_id",
             "time_cols": ["create_time"], "text_col": "content", "like_col": "like_count"},
    "wb": {"table": "weibo_note_comment", "id_col": "comment_id", "parent_col": "note_id",
           "time_cols": ["create_time", "create_date_time"], "text_col": "content",
           "like_col": "comment_like_count"},
    "tieba": {"table": "tieba_comment", "id_col": "comment_id", "parent_col": "note_id",
              "time_cols": ["publish_time", "add_ts"], "text_col": "content", "like_col": None},
    "zhihu": {"table": "zhihu_comment", "id_col": "comment_id", "parent_col": "content_id",
              "time_cols": ["publish_time"], "text_col": "content", "like_col": "like_count"},
}

_KEYWORD_SPLIT_RE = re.compile(r"[,，、;；\s|]+")
_ILLEGAL_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 各平台“发布时间字段”说明（写入数据包 meta.filtering.window_field）
WINDOW_FIELD_DESCRIPTION = "publish_time(mode=window)"

_POC_MODULES: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# 报告流水线导入（不复制代码，按 env/相对路径注入 sys.path）
# 运行时代码位于 app/report_pipeline/（旧版为 poc/report_pipeline，保留兼容）
# ---------------------------------------------------------------------------
def poc_pipeline_dir() -> str:
    """定位报告流水线：优先 `FS_REPORT_PIPELINE_DIR`，否则项目内相对路径。"""
    env_dir = os.environ.get("FS_REPORT_PIPELINE_DIR", "").strip()
    if env_dir:
        return os.path.abspath(env_dir)
    here = Path(__file__).resolve()
    candidates = [
        here.parents[1] / "report_pipeline",           # app/sidecar -> app/report_pipeline
        here.parent / "report_pipeline",               # app/sidecar/report_pipeline
        here.parents[2] / "app" / "report_pipeline",   # project/app/report_pipeline
        here.parents[2] / "poc" / "report_pipeline",   # 兼容旧目录
        Path.cwd() / "app" / "report_pipeline",
        Path.cwd() / "poc" / "report_pipeline",
    ]
    for candidate in candidates:
        if (candidate / "excel_loader.py").is_file():
            return str(candidate)
    return str(candidates[0])


def load_poc_modules() -> Dict[str, Any]:
    """导入报告流水线模块（只读复用；失败抛 RuntimeError 并给出路径提示）。"""
    global _POC_MODULES
    if _POC_MODULES is not None:
        return _POC_MODULES
    poc_dir = poc_pipeline_dir()
    if not os.path.isfile(os.path.join(poc_dir, "excel_loader.py")):
        raise RuntimeError(
            f"未找到报告流水线：{poc_dir}；请设置环境变量 FS_REPORT_PIPELINE_DIR 指向 app/report_pipeline"
        )
    if poc_dir not in sys.path:
        sys.path.insert(0, poc_dir)
    import excel_loader  # noqa: PLC0415
    import compute_stats  # noqa: PLC0415

    # POC 原始平台列表只有 6 个（无贴吧）；在“报告数据层”把贴吧补进其模块级常量，
    # 使 totals/platform_ranking/trend/sentiment.by_platform 覆盖 7 平台。
    order = list(compute_stats.PLATFORM_ORDER)
    if "贴吧" not in order:
        insert_at = order.index("知乎") if "知乎" in order else len(order)
        order.insert(insert_at, "贴吧")
        compute_stats.PLATFORM_ORDER = order

    _POC_MODULES = {
        "poc_dir": poc_dir,
        "NormalizedDataset": excel_loader.NormalizedDataset,
        "dataset_fingerprint": excel_loader.dataset_fingerprint,
        "compute_stats": compute_stats.compute_stats,
        "stats_fingerprint": compute_stats.stats_fingerprint,
        "compute_stats_module": compute_stats,
    }
    return _POC_MODULES


# ---------------------------------------------------------------------------
# 基础工具：时间 / 文本 / 关键词
# ---------------------------------------------------------------------------
def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and value != value:  # NaN
        return True
    if isinstance(value, str) and value.strip().lower() in ("", "none", "null", "nan"):
        return True
    return False


def _text(value: Any) -> str:
    if _is_blank(value):
        return ""
    return _ILLEGAL_TEXT_RE.sub(" ", str(value)).strip()


def _to_int(value: Any, default: int = 0) -> int:
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default
    if number != number or number < 0:
        return default
    return int(round(number))


def _to_epoch_ms(value: Any) -> Optional[int]:
    """任意时间表示 → 毫秒时间戳（用于选择最新行）；无法识别返回 None。"""
    parsed = parse_publish_time(value)
    return int(round(parsed.timestamp() * 1000)) if parsed else None


def _numeric_to_datetime(number: float) -> Optional[datetime]:
    if number != number or number <= 0:
        return None
    if 20000 <= number <= 80000:  # Excel 日期序列号
        return datetime(1899, 12, 30) + timedelta(days=number)
    if number >= 1e15:            # 微秒
        number /= 1_000_000.0
    elif number >= 1e12:          # 毫秒
        number /= 1000.0
    if number < 1e9:              # 非合理时间戳
        return None
    try:
        return datetime.fromtimestamp(number, tz=TZ).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def parse_publish_time(value: Any) -> Optional[datetime]:
    """
    统一解析发布时间（秒/毫秒/微秒时间戳、Excel 序列号、常见日期时间字符串、ISO8601）。

    返回 naive datetime（按 Asia/Shanghai 本地时间理解）；无法解析返回 None。
    """
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(TZ).replace(tzinfo=None)
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _numeric_to_datetime(float(value))

    text = str(value).strip()
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

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(TZ).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def keyword_tokens(value: Any) -> List[str]:
    if _is_blank(value):
        return []
    return [token for token in _KEYWORD_SPLIT_RE.split(str(value)) if token.strip()]


def _unique(items: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _truncate(text: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", _text(text))
    if len(value) <= limit:
        return value
    return value[: max(limit - 1, 0)].rstrip() + "…"


def parse_window(window_start: str, window_end: str) -> Tuple[datetime, datetime]:
    """窗口日期 → (start 00:00:00, end 23:59:59)，校验起止顺序。"""
    start_dt = parse_publish_time(window_start)
    end_dt = parse_publish_time(window_end)
    if start_dt is None or end_dt is None:
        raise ValueError("window_start/window_end 必须为 YYYY-MM-DD（或可解析的日期字符串）")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(window_start).strip()):
        start_dt = datetime(start_dt.year, start_dt.month, start_dt.day, 0, 0, 0)
    else:
        start_dt = start_dt.replace(microsecond=0)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(window_end).strip()):
        end_dt = datetime(end_dt.year, end_dt.month, end_dt.day, 23, 59, 59)
    else:
        end_dt = end_dt.replace(microsecond=0)
    if start_dt > end_dt:
        raise ValueError(f"window_start({window_start}) 晚于 window_end({window_end})")
    return start_dt, end_dt


def _fmt_dt(value: Optional[datetime]) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else ""


def _normalize_platform(value: Any) -> str:
    text = str(value or "").strip()
    if text in PLATFORM_LABELS:
        return text
    if text in LABEL_TO_CODE:
        return LABEL_TO_CODE[text]
    return text.lower() if text else ""


# ---------------------------------------------------------------------------
# 只读打开 MediaCrawler DB
# ---------------------------------------------------------------------------
def _db_uri(db_path: str) -> str:
    from urllib.parse import quote
    absolute = Path(db_path).resolve().as_posix()
    return f"file:{quote(absolute, safe='/:')}?mode=ro"


def _file_sha256(path: str, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 sha256（用于 stats.source.excel_sha256，8000 只校验 64 位 hex 格式）。"""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return hashlib.sha256(b"").hexdigest()
    return digest.hexdigest()


def open_media_db(db_path: str) -> sqlite3.Connection:
    if not db_path or not os.path.isfile(db_path):
        raise FileNotFoundError(f"MediaCrawler 数据库不存在：{db_path}")
    conn = sqlite3.connect(_db_uri(db_path), uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _read_rows(conn: sqlite3.Connection, table: str) -> List[Dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]
    except sqlite3.Error:
        return []


__all__ = [
    "COMMENT_SPECS",
    "CONTENT_SPECS",
    "DB_SCHEMA",
    "DEFAULT_PLATFORMS",
    "LABEL_TO_CODE",
    "PLATFORM_LABELS",
    "SCHEMA_VERSION",
    "SENTIMENT_CN",
    "TIMEZONE",
    "TZ",
    "WINDOW_FIELD_DESCRIPTION",
    "_SENTIMENT_ALIASES",
    "_WEEKDAY_CN",
    "_db_uri",
    "_file_sha256",
    "_fmt_dt",
    "_is_blank",
    "_normalize_platform",
    "_read_rows",
    "_table_exists",
    "_text",
    "_to_epoch_ms",
    "_to_int",
    "_truncate",
    "_unique",
    "keyword_tokens",
    "load_poc_modules",
    "open_media_db",
    "parse_publish_time",
    "parse_window",
    "poc_pipeline_dir",
]

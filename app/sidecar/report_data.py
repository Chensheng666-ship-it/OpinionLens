# -*- coding: utf-8 -*-
"""
report_data.py — exe「生成周报」数据层（DB → POC 统计 → 公共舆情数据包 v1）

职责（统计/模板管线见 `app/report_pipeline/README.md`）：
  1. 从 MediaCrawler SQLite 读取 7 平台内容/评论，按**发布时间窗口** + 关键词相关度过滤/去重；
  2. 构造 `poc/report_pipeline` 的 `NormalizedDataset`（复用其字段约定，不复制 POC 代码），
     调用 `compute_stats.compute_stats()` 生成确定性 stats；
  3. 批量情感分析（调用方传入 `sentiment_analyzer`，失败/未配置按 neutral 兜底），结果缓存到
     `<data-dir>/app.db` 的 `sentiment_cache` 表，命中缓存不重复请求；
  4. 组装 `public-sentiment-data@v1` 数据包（counts/stats/samples/contents/meta + stats fingerprint）。

对外 API：
  - `build_report_package(db_path, window_start, window_end, keywords=(), platforms=..., sentiment_analyzer=None,
                          *, report_date="", data_dir="", app_db_path="", batch_size=50, ...) -> dict`
  - `load_report_dataset(db_path, *, window_start, window_end, keywords, platforms)
        -> (NormalizedDataset, data_quality)`（调试/测试用）
  - `load_sentiment_cache(app_db_path, *, platforms=None, kinds=None) -> dict`
  - `upsert_sentiment(app_db_path, ...)` / `upsert_sentiments(app_db_path, entries)`
  - `normalize_sentiment_label(value)`（中文/英文 → positive|neutral|negative）
  - `DEFAULT_PLATFORMS` / `PLATFORM_LABELS` / `SENTIMENT_CN`

情感回调契约（与 app-sidecar-dev 对齐）：
  analyzer(texts: list[{"id": str, "text": str}]) -> list[{"id": str,
                                                          "sentiment": "positive|neutral|negative|正面|中性|负面",
                                                          "confidence": number, "reason": str}]
  - 调用方内部可自行分批/重试；本模块也已按 `batch_size`（默认 50）分批、文本去重后调用；
  - 返回缺失 / 异常 / 未配置 → 对应项 neutral 兜底并记入 stats.warnings / data_quality.sentiment；
    fallback 结果**不写缓存**（下次可重试真实分析）。

窗口口径：
  - `window_start` / `window_end` 为 ISO 日期字符串（含当天）：start 00:00:00 ~ end 23:59:59，
    timezone Asia/Shanghai（与数据包方案一致）；
  - 内容按**发布时间**进窗口；评论只取“窗口内内容的评论”（评论时间不单独设窗口，父内容在窗口内即可）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# 版本号唯一来源（同目录 ``version.py``）：主进程通过 ``FS_APP_VERSION`` 注入，
# 缺失时回退 ``FALLBACK_VERSION``；不要在此硬编码版本字符串。
_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))
from version import APP_VERSION  # noqa: E402

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

SENTIMENT_CACHE_TABLE = "sentiment_cache"
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
    import excel_loader  # type: ignore  # noqa: PLC0415
    import compute_stats  # type: ignore  # noqa: PLC0415

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


def normalize_sentiment_label(value: Any) -> Optional[str]:
    """中文/英文情感标签 → positive|neutral|negative；未知返回 None。"""
    if _is_blank(value):
        return None
    text = str(value).strip().lower()
    text = re.sub(r"[\[\]【】()（）\s]", "", text)
    return _SENTIMENT_ALIASES.get(text)


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


# ---------------------------------------------------------------------------
# 窗口数据读取 → POC NormalizedDataset
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
    analyzer 为 None / 抛异常 / 返回缺失 → 对应文本 neutral 兜底（不写缓存，下次可重试）。
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
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        item["_sentiment_text_hash"] = text_hash
        platform_code = item["_platform_code"]
        cached = cache.get((platform_code, item["kind"], item_id))
        if cached and cached.get("text_hash") == text_hash:
            sentiment = normalize_sentiment_label(cached.get("sentiment")) or "neutral"
            item["sentiment"] = sentiment
            item["sentiment_raw"] = cached.get("sentiment") or ""
            item["_sentiment_reason"] = cached.get("reason") or ""
            info["cache_hits"] += 1
            info["analyzed_items"] += 1
            continue
        pending_texts[text_hash] = text
        item_entries.append((item, text_hash, ""))

    # 2) 无 analyzer / 无待分析：全部 neutral 兜底（无缓存时）
    if not item_entries:
        return info
    if not callable(sentiment_analyzer):
        for item, text_hash, _ in item_entries:
            item["sentiment"] = "neutral"
            item["sentiment_raw"] = ""
            item["_sentiment_reason"] = ""
        info["fallback_neutral_items"] = len(item_entries)
        info["fallback_neutral_texts"] = len(pending_texts)
        warnings.append("sentiment_analyzer 未配置：情感按 neutral 兜底（未写缓存）")
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
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or "")
            if entry_id:
                results[entry_id] = entry

    # 4) 写回 dataset + 写缓存
    cache_entries: List[Dict[str, Any]] = []
    for item, text_hash, _ in item_entries:
        entry = results.get(text_hash)
        sentiment = normalize_sentiment_label(entry.get("sentiment")) if entry else None
        if sentiment is None:
            item["sentiment"] = "neutral"
            item["sentiment_raw"] = ""
            item["_sentiment_reason"] = ""
            info["fallback_neutral_items"] += 1
            continue
        reason = _truncate(entry.get("reason") or "", 500)
        item["sentiment"] = sentiment
        item["sentiment_raw"] = _text(entry.get("sentiment"))
        item["_sentiment_reason"] = reason
        info["analyzed_items"] += 1
        item_id = str(item.get("content_id") if item["kind"] == "content" else item.get("comment_id"))
        cache_entries.append({
            "platform": item["_platform_code"],
            "kind": item["kind"],
            "item_id": item_id,
            "text_hash": text_hash,
            "sentiment": sentiment,
            "reason": reason,
        })
    fallback_texts = {text_hash for text_hash, _ in texts} - set(results.keys())
    info["fallback_neutral_texts"] = len(fallback_texts)
    if fallback_texts:
        warnings.append(f"情感分析有 {len(fallback_texts)} 条文本未返回结果，已按 neutral 兜底")
    if info["errors"]:
        warnings.append(f"情感分析失败 {len(info['errors'])} 个批次，失败批次内文本按 neutral 兜底")

    if cache_entries:
        try:
            info["cache_written"] = upsert_sentiments(app_db_path, cache_entries, model=model)
        except Exception as exc:  # noqa: BLE001 - 缓存写失败不影响报告
            info["cache_error"] = f"{type(exc).__name__}: {exc}"
            warnings.append(f"情感缓存写入失败（报告继续）：{info['cache_error']}")
    return info


# ---------------------------------------------------------------------------
# 空数据 stats（窗口内无数据时保证包结构完整）
# ---------------------------------------------------------------------------
def _empty_stats(*, report_date: str, brand: str, title: str,
                 window_start: str, window_end: str, warnings: List[str]) -> Dict[str, Any]:
    """窗口内无数据时的 stats：结构完整、可过 `stats.schema.json` 校验（8000 会用同一 schema）。"""
    start_dt = parse_publish_time(window_start) or datetime.now()
    end_dt = parse_publish_time(window_end) or start_dt
    days: List[Dict[str, Any]] = []
    cursor = datetime(start_dt.year, start_dt.month, start_dt.day)
    window_days = max(1, min((end_dt.date() - start_dt.date()).days + 1, 90))
    for _ in range(window_days):
        day_key = cursor.strftime("%Y-%m-%d")
        days.append({
            "date": day_key,
            "label": f"{cursor.month:02d}-{cursor.day:02d} {_WEEKDAY_CN[cursor.weekday()]}",
            "weekday": _WEEKDAY_CN[cursor.weekday()],
            "total": 0,
            "content": 0,
            "comments": 0,
            "by_platform": {},
        })
        cursor += timedelta(days=1)
    empty_sentiment = {
        "positive": {"count": 0, "ratio": 0.0},
        "neutral": {"count": 0, "ratio": 0.0},
        "negative": {"count": 0, "ratio": 0.0},
        "total": 0,
    }
    return {
        "schema_version": "1.0",
        "generator": {"name": "app/sidecar/report_data.py", "version": APP_VERSION,
                      "generated_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")},
        "source": {"excel_path": "", "excel_sha256": "", "sheets": [], "read_at": ""},
        "report": {
            "title": title, "brand": brand, "report_date": report_date, "version": 1,
            "scope_mode": "period",
            "period": {"start": window_start, "end": window_end,
                       "label": f"{window_start} 至 {window_end}"},
            "collected_at": f"{window_end} 23:59:59",
            "data_span": {"start": f"{window_start} 00:00:00", "end": f"{window_end} 23:59:59"},
            "trend_window": {"start": window_start, "end": window_end, "days": window_days},
            "data_source": "MediaCrawler SQLite + exe 代码统计",
            "template_version": "public-sentiment-weekly@v1",
        },
        "totals": {"items": 0, "content": 0, "comments": 0, "platforms": 0,
                   "by_platform": [],
                   "content_interactions": {"likes": 0, "comments": 0, "shares": 0},
                   "content_engagement": 0},
        "sentiment": {"overall": empty_sentiment, "content": dict(empty_sentiment),
                      "comments": dict(empty_sentiment), "by_platform": []},
        "risk": {"level": "低", "level_code": "low", "negative_ratio": 0.0,
                 "content_negative_ratio": 0.0, "negative_events": 0,
                 "thresholds": {"low": 0.08, "medium": 0.15},
                 "basis": "窗口内无数据"},
        "platform_ranking": [],
        "trend": {"granularity": "day", "window": {"start": window_start, "end": window_end,
                                                   "days": window_days},
                  "platforms": [], "days": days,
                  "window_totals": {"items": 0, "content": 0, "comments": 0,
                                    "by_platform": {}, "daily_average": 0},
                  "peak": None, "note": "窗口内无数据"},
        "negative_events": [], "positive_events": [],
        "keywords": {"method": "", "documents": 0, "brand_terms": [], "top": [], "related": []},
        "comments": {"total": 0, "by_platform": {}, "sentiment": dict(empty_sentiment),
                     "avg_likes": 0, "max_likes": 0, "comment_to_content_ratio": 0, "top_liked": []},
        "data_quality": {"sheet_row_counts": {}, "content_items": 0, "comment_items": 0,
                         "time_parse_failures": 0, "content_id_precision_fixed": 0,
                         "relevance": {"brand_matched_content": 0, "unmatched_content": 0,
                                       "brand_match_rate": 0.0},
                         "historical_share": {"items_in_trend_window": 0, "items_historical": 0,
                                              "historical_ratio": 0.0}},
        "samples": {"negative": [], "positive": [], "top_comments": [], "hot_keywords": []},
        "warnings": list(warnings),
    }


# ---------------------------------------------------------------------------
# 数据包组装
# ---------------------------------------------------------------------------
def _normalize_platform(value: Any) -> str:
    text = str(value or "").strip()
    if text in PLATFORM_LABELS:
        return text
    if text in LABEL_TO_CODE:
        return LABEL_TO_CODE[text]
    return text.lower() if text else ""


def _platform_code(item: Dict[str, Any]) -> str:
    return item.get("_platform_code") or LABEL_TO_CODE.get(item.get("platform", ""), "")


def _event_metrics(item: Dict[str, Any]) -> Dict[str, int]:
    interactions = item.get("interactions") or {}
    return {key: int(interactions.get(key) or 0)
            for key in ("likes", "comments", "shares", "collects", "coins", "plays")}


def _sample_from_content(item: Dict[str, Any], *, limit: int = 120) -> Dict[str, Any]:
    return {
        "platform": _platform_code(item),
        "content_id": item.get("content_id", ""),
        "title": _truncate(item.get("title") or item.get("summary") or "", limit),
        "summary": _truncate(item.get("summary") or item.get("text") or "", limit),
        "url": item.get("url", ""),
        "published_at": item.get("published_at", ""),
        "metrics": _event_metrics(item),
        "sentiment": SENTIMENT_CN.get(item.get("sentiment", "neutral"), "中性"),
        "heat": round(float(item.get("heat_score") or 0.0), 1),
    }


def _build_samples(dataset: Any, stats: Dict[str, Any], *, max_samples: int = 20) -> Dict[str, Any]:
    contents = list(dataset.contents)
    comments = list(dataset.comments)
    negative = sorted([item for item in contents if item.get("sentiment") == "negative"],
                      key=lambda item: (-float(item.get("heat_score") or 0.0), item.get("content_id", "")))
    positive = sorted([item for item in contents if item.get("sentiment") == "positive"],
                      key=lambda item: (-float(item.get("heat_score") or 0.0), item.get("content_id", "")))
    top_comments = sorted(comments, key=lambda item: (-int(item.get("likes") or 0), item.get("comment_id", "")))
    # 负面评论样例：第四章「重点负面舆情事件」只列负面内容帖，无负面内容帖但评论区负面占比高时，
    # AI 需要这些样例才能正确说明「风险来自评论区」，避免写出「未识别到负面内容、舆情面平稳」。
    negative_comments = sorted(
        [item for item in comments if item.get("sentiment") == "negative"],
        key=lambda item: (-int(item.get("likes") or 0), item.get("comment_id", "")),
    )
    keywords = stats.get("keywords") or {}
    hot = [item["keyword"] for item in (keywords.get("related") or [])[:10]] or \
          [item["keyword"] for item in (keywords.get("top") or [])[:10]]
    return {
        "negative": [_sample_from_content(item) for item in negative[:max_samples]],
        "negative_comments": [
            {
                "platform": _platform_code(item),
                "content": _truncate(item.get("text") or item.get("summary") or "", 160),
                "likes": int(item.get("likes") or 0),
                "published_at": item.get("published_at", ""),
            }
            for item in negative_comments[:10]
        ],
        "positive": [_sample_from_content(item) for item in positive[:max_samples]],
        "top_comments": [
            {
                "platform": _platform_code(item),
                "content_id": item.get("content_id", ""),
                "content": _truncate(item.get("text") or item.get("summary") or "", 120),
                "likes": int(item.get("likes") or 0),
                "sentiment": SENTIMENT_CN.get(item.get("sentiment", "neutral"), "中性"),
            }
            for item in top_comments[:max_samples]
        ],
        "hot_keywords": hot,
    }


def _build_contents(dataset: Any, *, max_contents: int = 500) -> List[Dict[str, Any]]:
    ordered = sorted(dataset.contents, key=lambda item: (-float(item.get("heat_score") or 0.0),
                                                         item.get("published_at", "")))
    result = []
    for item in ordered[:max_contents]:
        result.append({
            "platform": _platform_code(item),
            "content_id": item.get("content_id", ""),
            "title": _truncate(item.get("title") or item.get("summary") or "", 80),
            "published_at": item.get("published_at", ""),
            "sentiment": SENTIMENT_CN.get(item.get("sentiment", "neutral"), "中性"),
            "heat": round(float(item.get("heat_score") or 0.0), 1),
            "url": item.get("url", ""),
        })
    return result


def _build_counts(dataset: Any, platforms: Sequence[str]) -> Dict[str, Any]:
    by_platform = []
    for code in platforms:
        label = PLATFORM_LABELS.get(code, code)
        content_count = sum(1 for item in dataset.contents if item.get("platform") == label)
        comment_count = sum(1 for item in dataset.comments if item.get("platform") == label)
        by_platform.append({"platform": code, "content": content_count, "comments": comment_count})
    return {
        "content": len(dataset.contents),
        "comments": len(dataset.comments),
        "total": len(dataset.contents) + len(dataset.comments),
        "by_platform": by_platform,
    }


def _window_label(window_start: str, window_end: str) -> str:
    try:
        start_dt = datetime.strptime(window_start, "%Y-%m-%d")
        end_dt = datetime.strptime(window_end, "%Y-%m-%d")
        iso_week = end_dt.isocalendar()
        if (end_dt - start_dt).days == 6 and start_dt.isoweekday() == 1:
            return f"{iso_week[0]}年第{iso_week[1]}周（{window_start[5:]} ~ {window_end[5:]}）"
        return f"{window_start} ~ {window_end}"
    except ValueError:
        return f"{window_start} ~ {window_end}"


def build_report_package(
    db_path: str,
    window_start: str,
    window_end: str,
    keywords: Sequence[str] = (),
    platforms: Sequence[str] = DEFAULT_PLATFORMS,
    sentiment_analyzer: Optional[Callable[[List[Dict[str, Any]]], Any]] = None,
    *,
    report_date: str = "",
    data_dir: str = "",
    app_db_path: str = "",
    batch_size: int = 50,
    brand: str = "示例科技",
    title: str = "示例科技舆情监测周报",
    sentiment_model: str = "",
    max_contents: int = 500,
    max_samples: int = 20,
    include_contents: bool = True,
    instruction: str = "",
) -> Dict[str, Any]:
    """
    构建「公共舆情数据包 v1」（`public-sentiment-data@v1`）。

    参数：
      db_path            MediaCrawler sqlite（只读）；
      window_start/end   发布时间窗口 ISO 日期（含当天，Asia/Shanghai）；
      keywords           关键词列表（内容 source_keyword 召回 + 标题/正文/nickname 相关度过滤）；
      platforms          平台 code 子集，默认全部 7 个；
      sentiment_analyzer 批量情感回调（见模块 docstring），None/异常按 neutral 兜底；
      report_date        报告日期，默认 = window_end；
      data_dir           exe 数据目录（用于定位 app.db；默认由 db_path 推导 <data-dir>）；
      app_db_path        显式 app.db 路径（优先级最高，测试可用临时库）；
      batch_size         情感分析每批文本数（≤200）；
      include_contents   是否在包中附带 contents 摘要（≤max_contents）。
    """
    window_start_dt, window_end_dt = parse_window(window_start, window_end)
    platform_codes = _unique(
        code for code in (_normalize_platform(platform) for platform in (platforms or DEFAULT_PLATFORMS))
        if code
    )
    if not platform_codes:
        platform_codes = list(DEFAULT_PLATFORMS)
    unknown = [code for code in platform_codes if code not in CONTENT_SPECS]
    if unknown:
        raise ValueError(f"不支持的平台：{unknown}（支持 {DEFAULT_PLATFORMS} 或中文名）")
    keyword_list = _unique(str(keyword).strip() for keyword in (keywords or []) if str(keyword).strip())
    report_date = (report_date or window_end).strip() or window_end

    resolved_app_db = _resolve_app_db_path(db_path, data_dir=data_dir, app_db_path=app_db_path)

    dataset, _quality = load_report_dataset(
        db_path,
        window_start=window_start,
        window_end=window_end,
        keywords=keyword_list,
        platforms=platform_codes,
    )

    warnings: List[str] = list(dataset.warnings)
    sentiment_info = analyze_dataset_sentiment(
        dataset,
        sentiment_analyzer,
        app_db_path=resolved_app_db,
        batch_size=batch_size,
        model=sentiment_model,
        warnings=warnings,
    )
    dataset.warnings = warnings
    dataset.data_quality["sentiment"] = sentiment_info
    dataset.data_quality["window_filter"]["report_date"] = report_date

    poc = load_poc_modules()
    compute_stats = poc["compute_stats"]
    window_days = max(1, min((window_end_dt.date() - window_start_dt.date()).days + 1, 90))
    try:
        # 注意：dataset 已经按“发布时间窗 + 关键词相关”过滤好，这里用 snapshot 口径统计，
        # 避免 compute_stats 的 period 二次过滤（评论按父内容纳入，不用评论时间再卡窗口），
        # 保证 package.counts 与 stats.totals 严格一致；随后把 report.period 覆写为报告窗口。
        stats = compute_stats(
            dataset,
            report_date=report_date,
            brand=brand,
            title=title,
            mode="snapshot",
            trend_end=window_end_dt.strftime("%Y-%m-%d"),
            trend_days=window_days,
            version=1,
            extra_meta={
                "package_schema": SCHEMA_VERSION,
                "window": {"start": window_start, "end": window_end, "timezone": TIMEZONE},
                "db_path": os.path.abspath(db_path),
            },
        )
        report_meta = stats.get("report")
        if isinstance(report_meta, dict):
            report_meta["scope_mode"] = "period"
            report_meta["period"] = {
                "start": window_start_dt.strftime("%Y-%m-%d"),
                "end": window_end_dt.strftime("%Y-%m-%d"),
                "label": _window_label(window_start, window_end),
            }
            report_meta["trend_window"] = {
                "start": window_start_dt.strftime("%Y-%m-%d"),
                "end": window_end_dt.strftime("%Y-%m-%d"),
                "days": window_days,
            }
    except ValueError:
        stats = _empty_stats(report_date=report_date, brand=brand, title=title,
                             window_start=window_start, window_end=window_end, warnings=warnings)
    # stats 里再补情感分析信息与 warnings（compute_stats 会复制 dataset.data_quality/warnings）
    stats.setdefault("data_quality", {})["sentiment"] = sentiment_info
    stats["warnings"] = _unique(stats.get("warnings", []) + warnings)

    # ---- 8000 数据包校验要求（2026-11-09 真实联调修复）----
    # 1) stats.source.excel_sha256 必须是 64 位 hex（8000 stats.schema.json 有 pattern 校验）；
    #    用 MediaCrawler DB 文件内容 sha256 兜底/填充（8000 只校验格式，不会再比对文件）。
    if not isinstance(stats.get("source"), dict):
        stats["source"] = {}
    source = stats["source"]
    if not re.fullmatch(r"[a-f0-9]{64}", str(source.get("excel_sha256") or "")):
        source["excel_sha256"] = _file_sha256(db_path)
    source.setdefault("excel_path", os.path.abspath(db_path))
    try:
        source["excel_size_bytes"] = int(
            dataset.source.get("excel_size_bytes") or os.path.getsize(db_path)
        )
    except OSError:
        source["excel_size_bytes"] = 0
    if not source.get("sheets"):
        source["sheets"] = list(dataset.source.get("sheets") or [os.path.basename(db_path)])
    if not source.get("read_at"):
        source["read_at"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")

    # 2) 8000 校验 `_canonical_stats_digest(stats) == stats.stats_fingerprint`，
    #    期望**纯 hex**（与 POC compute_stats.stats_fingerprint() 返回值一致），不能带 "sha256:" 前缀。
    #    包 meta.stats_fingerprint 仍保留 "sha256:" 前缀便于展示/审计。
    fingerprint_hex = poc["stats_fingerprint"](stats)
    stats["stats_fingerprint"] = fingerprint_hex
    fingerprint = "sha256:" + fingerprint_hex

    counts = _build_counts(dataset, platform_codes)
    samples = _build_samples(dataset, stats, max_samples=max_samples)
    contents = _build_contents(dataset, max_contents=max_contents) if include_contents else []
    long_text_truncated = 0
    for item in list(dataset.contents) + list(dataset.comments):
        if len(item.get("text") or "") > 1200:
            long_text_truncated += 1

    package_id = f"pkg_{window_end.replace('-', '')}_{fingerprint.split(':')[-1][:8]}"
    generated_at = datetime.now(TZ).isoformat(timespec="seconds")
    data_freshness = ""
    all_times = [item.get("published_at") for item in dataset.contents] or []
    if all_times:
        data_freshness = f"窗口数据最新发布时间 {max(all_times)}（Asia/Shanghai）"
    package: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "package_id": package_id,
        "generated_at": generated_at,
        "source": {
            "app": "opinionlens-public-opinion",
            "app_version": APP_VERSION,
            "db_schema": DB_SCHEMA,
        },
        "report": {
            "brand": brand,
            "title": title,
            "window": {
                "mode": "publish_time",
                "start": window_start,
                "end": window_end,
                "timezone": TIMEZONE,
                "label": _window_label(window_start, window_end),
            },
            "keywords": keyword_list,
            "platforms": platform_codes,
        },
        "counts": counts,
        "stats": stats,
        "samples": samples,
        "contents": contents,
        "meta": {
            "stats_fingerprint": fingerprint,
            "filtering": {
                "window_field": WINDOW_FIELD_DESCRIPTION,
                "dedup": "platform+content_id",
                "exclude": ["off_brand"],
                "truncated": {
                    "contents": max(0, len(dataset.contents) - len(contents)),
                    "samples": 0,
                    "long_text": long_text_truncated,
                },
            },
            "data_freshness": data_freshness,
            "sentiment": sentiment_info,
        },
    }
    if instruction:
        package["instruction"] = instruction
    return package


def _resolve_app_db_path(db_path: str, *, data_dir: str = "", app_db_path: str = "") -> str:
    """确定 app.db 路径：显式参数 > data_dir/app.db > <db_path 的上两级>/app.db。"""
    if app_db_path:
        return os.path.abspath(app_db_path)
    if data_dir:
        return os.path.abspath(os.path.join(data_dir, "app.db"))
    db_parent = Path(os.path.abspath(db_path)).parent  # <data-dir>/crawler
    return str(db_parent.parent / "app.db")


# ---------------------------------------------------------------------------
# CLI（调试/联调用；sidecar 走函数调用）
# ---------------------------------------------------------------------------
def _stub_analyzer(texts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """离线演示用启发式 analyzer（真实环境由 sidecar 传模型 API 分析器）。"""
    negative_words = ("拖欠", "仲裁", "避雷", "投诉", "维权", "裁员", "违法", "骗", "差", "坑")
    positive_words = ("获奖", "表彰", "荣誉", "优秀", "点赞", "赞", "成功", "合作", "恭喜", "周年")
    results = []
    for entry in texts:
        text = str(entry.get("text") or "")
        if any(word in text for word in negative_words):
            sentiment, reason = "negative", "命中负面词"
        elif any(word in text for word in positive_words):
            sentiment, reason = "positive", "命中正面词"
        else:
            sentiment, reason = "neutral", "无明显情感词"
        results.append({"id": entry.get("id"), "sentiment": sentiment,
                        "confidence": 0.6, "reason": reason})
    return results


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="构建舆情报告数据包（public-sentiment-data@v1）")
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument("--keywords", default="示例科技", help="逗号分隔")
    parser.add_argument("--platforms", default=",".join(DEFAULT_PLATFORMS))
    parser.add_argument("--app-db-path", default="", help="情感缓存 app.db（默认由 db_path 推导）")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--report-date", default="")
    parser.add_argument("--brand", default="示例科技")
    parser.add_argument("--title", default="示例科技舆情监测周报")
    parser.add_argument("--sentiment-model", default="stub")
    parser.add_argument("--stub-analyzer", action="store_true", help="使用离线启发式 analyzer（演示）")
    parser.add_argument("--no-analyzer", action="store_true", help="不传 analyzer，情感按 neutral 兜底")
    parser.add_argument("--out", default="", help="数据包 JSON 输出路径（默认打印摘要）")
    args = parser.parse_args(argv)

    keywords = [part.strip() for part in re.split(r"[,，、]", args.keywords) if part.strip()]
    platforms = [part.strip() for part in args.platforms.split(",") if part.strip()]
    analyzer = None if args.no_analyzer else (_stub_analyzer if args.stub_analyzer else None)
    package = build_report_package(
        args.db_path, args.window_start, args.window_end, keywords, platforms, analyzer,
        report_date=args.report_date, data_dir=args.data_dir, app_db_path=args.app_db_path,
        sentiment_model=args.sentiment_model, brand=args.brand, title=args.title,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(package, fh, ensure_ascii=False, indent=2)
        print(f"数据包已写出：{args.out}（{os.path.getsize(args.out)} bytes）")
    print(json.dumps({
        "schema_version": package["schema_version"],
        "package_id": package["package_id"],
        "window": package["report"]["window"],
        "counts": {"content": package["counts"]["content"], "comments": package["counts"]["comments"],
                   "total": package["counts"]["total"]},
        "by_platform": package["counts"]["by_platform"],
        "stats_totals": package["stats"]["totals"]["items"],
        "sentiment": package["stats"]["sentiment"]["overall"],
        "risk": package["stats"]["risk"]["level"],
        "stats_fingerprint": package["meta"]["stats_fingerprint"],
        "sentiment_analysis": package["meta"]["sentiment"],
        "warnings": package["stats"]["warnings"][:5],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

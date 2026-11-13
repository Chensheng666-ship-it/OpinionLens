# -*- coding: utf-8 -*-
"""
excel_loader.py — MediaCrawler 舆情 Excel 读取与规范化

职责（只做“读 + 清洗 + 归一化”，不做任何统计聚合，不做任何 HTML）：
  1. 读取 6 个平台的内容表 + 评论表（共 12 个 sheet，允许缺表）；
  2. 时间字段归一化：支持 datetime / Excel 序列号 / 10 位秒时间戳 / 13 位毫秒时间戳 /
     "YYYY-MM-DD HH:MM:SS" / "YYYY/MM/DD" 等字符串；
  3. 情感标签归一化：`【正面】`、`正面 `、`Positive` 等统一为 positive / neutral / negative；
  4. 内容 ID 规范化与精度修复：抖音 aweme_id、微博 note_id 在 Excel 中曾被双精度浮点存储，
     低位数字被舍入（科学计数法精度丢失），本模块优先从 *_url 中取回精确 ID，
     并用“Excel 原始 ID”建立评论 → 内容的关联映射；
  5. 去重：内容按 (platform, content_id)，评论按 (platform, comment_id)；
  6. 输出 NormalizedDataset（纯 Python dict/list），并附带 data_quality 说明。

设计原则：统计逻辑全部放在 compute_stats.py；本模块输出可直接被审计的规范化明细，
便于 docs 中逐条抽查（Excel 行数 <-> 规范化条数 <-> stats JSON <-> HTML 数字 四层对账）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    from openpyxl import load_workbook
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少 openpyxl，请使用 MediaCrawler 自带解释器：\n"
        "  MediaCrawler\\.venv\\Scripts\\python.exe"
    ) from exc


SCHEMA_VERSION = "1.0"
PIPELINE_VERSION = "1.0.0"

# 平台展示顺序（与模板/报告表格顺序保持一致；真正的排名由数据决定）
PLATFORM_ORDER: List[str] = ["小红书", "抖音", "快手", "B站", "微博", "知乎"]

# ---------------------------------------------------------------------------
# 平台字段映射
# ---------------------------------------------------------------------------
# content spec 字段说明：
#   sheet        内容表名
#   time_col     发布时间列
#   id_col       内容 ID 列（Excel 中可能被存成数字）
#   url_col      原文链接列（用于取回精确 ID + 供报告溯源）
#   url_id_pattern 从链接中提取内容 ID 的正则
#   text_cols    正文候选列（按顺序拼接）
#   title_col    标题列（可为空）
#   author_col   作者昵称列
#   interactions 互动字段 -> 统一键：likes / comments / shares / collects / plays
CONTENT_SPECS: Dict[str, Dict[str, Any]] = {
    "小红书": {
        "sheet": "小红书",
        "time_col": "time",
        "id_col": "note_id",
        "url_col": "note_url",
        "url_id_pattern": r"/explore/([0-9a-zA-Z]+)",
        "title_col": "title",
        "text_cols": ["title", "desc"],
        "author_col": "nickname",
        "interactions": {
            "likes": "liked_count",
            "comments": "comment_count",
            "collects": "collected_count",
            "shares": "share_count",
        },
    },
    "抖音": {
        "sheet": "抖音",
        "time_col": "create_time",
        "id_col": "aweme_id",
        "url_col": "aweme_url",
        "url_id_pattern": r"/video/(\d+)",
        "title_col": "title",
        "text_cols": ["title", "desc"],
        "author_col": "nickname",
        "interactions": {
            "likes": "liked_count",
            "comments": "comment_count",
            "shares": "share_count",
            "collects": "collected_count",
        },
    },
    "快手": {
        "sheet": "快手",
        "time_col": "create_time",
        "id_col": "video_id",
        "url_col": "video_url",
        "url_id_pattern": r"/short-video/([0-9a-zA-Z]+)",
        "title_col": "title",
        "text_cols": ["title", "desc"],
        "author_col": "nickname",
        "interactions": {"likes": "liked_count", "plays": "viewd_count"},
    },
    "B站": {
        "sheet": "B站",
        "time_col": "create_time",
        "id_col": "video_id",
        "url_col": "video_url",
        "url_id_pattern": r"/video/(?:av)?(\d+)",
        "title_col": "title",
        "text_cols": ["title", "desc"],
        "author_col": "nickname",
        "interactions": {
            "likes": "liked_count",
            "comments": "video_comment",
            "shares": "video_share_count",
            "collects": "video_favorite_count",
            "coins": "video_coin_count",
            "plays": "video_play_count",
        },
    },
    "微博": {
        "sheet": "微博",
        "time_col": "create_time",
        "id_col": "note_id",
        "url_col": "note_url",
        "url_id_pattern": r"/detail/(\d+)",
        "title_col": None,
        "text_cols": ["content"],
        "author_col": "nickname",
        "interactions": {
            "likes": "liked_count",
            "comments": "comments_count",
            "shares": "shared_count",
        },
    },
    "知乎": {
        "sheet": "知乎",
        "time_col": "created_time",
        "id_col": "content_id",
        "url_col": "content_url",
        "url_id_pattern": r"/(?:p|answer)/(\d+)",
        "title_col": "title",
        "text_cols": ["title", "desc", "content_text"],
        "author_col": "user_nickname",
        "interactions": {"likes": "voteup_count", "comments": "comment_count"},
    },
}

# comment spec 字段说明：
#   sheet      评论表名
#   time_col   评论发布时间列
#   id_col     评论 ID 列
#   text_col   评论正文列
#   parent_col 关联内容 ID 列（取 Excel 原始值，与内容 raw_id 对齐）
#   like_col   点赞列（用于评论互动量）
COMMENT_SPECS: Dict[str, Dict[str, Any]] = {
    "小红书": {
        "sheet": "小红书评论",
        "time_col": "create_time",
        "id_col": "comment_id",
        "text_col": "content",
        "parent_col": "note_id",
        "like_col": "like_count",
    },
    "抖音": {
        "sheet": "抖音评论",
        "time_col": "create_time",
        "id_col": "comment_id",
        "text_col": "content",
        "parent_col": "aweme_id",
        "like_col": "like_count",
    },
    "快手": {
        "sheet": "快手评论",
        "time_col": "create_time",
        "id_col": "comment_id",
        "text_col": "content",
        "parent_col": "video_id",
        "like_col": None,  # 快手评论表无点赞列
    },
    "B站": {
        "sheet": "B站评论",
        "time_col": "create_time",
        "id_col": "comment_id",
        "text_col": "content",
        "parent_col": "video_id",
        "like_col": "like_count",
    },
    "微博": {
        "sheet": "微博评论",
        "time_col": "create_time",
        "id_col": "comment_id",
        "text_col": "content",
        "parent_col": "note_id",
        "like_col": "comment_like_count",
    },
    "知乎": {
        "sheet": "知乎评论",
        "time_col": "publish_time",
        "id_col": "comment_id",
        "text_col": "content",
        "parent_col": "content_id",
        "like_col": "like_count",
    },
}

# 已知“数字 ID 被浮点存储”的平台（用于 data_quality 提示：评论 ID 无法从链接修复精度）
NUMERIC_ID_RISK_PLATFORMS = {"抖音", "微博"}

# ---------------------------------------------------------------------------
# 基础解析工具
# ---------------------------------------------------------------------------
_EPOCH_MIN_S = 10 ** 9        # 2001-09-09 之前的“秒级时间戳”不合理
_EPOCH_MIN_MS = 10 ** 12      # 毫秒级时间戳下限
_EXCEL_SERIAL_MIN = 20000     # Excel 日期序列号合理区间（1954 年之后）
_EXCEL_SERIAL_MAX = 80000     # 至 2119 年

_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%Y%m%d%H%M%S",
    "%Y%m%d",
)


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip().lower() in ("", "nan", "none", "null", "nat"):
        return True
    return False


def cell_str(value: Any) -> str:
    """单元格 -> 干净字符串。整数型浮点（7.63e18）转为不带科学计数法的整数字符串。"""
    if _is_blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def cell_int(value: Any) -> int:
    """单元格 -> 非负整数（互动量缺失按 0 处理）。"""
    if _is_blank(value):
        return 0
    try:
        f = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0
    if math.isnan(f) or f < 0:
        return 0
    return int(round(f))


def _clean_text(text: str) -> str:
    """正文清洗：压缩空白、去掉零宽字符。保留中文标点与 emoji 之外的可打印字符。"""
    if not text:
        return ""
    text = str(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def parse_time(value: Any) -> Optional[datetime]:
    """
    统一时间解析。

    支持：
      - datetime / date
      - Excel 日期序列号（如 46163.5）
      - 10 位秒时间戳、13 位毫秒时间戳（int / float / 纯数字字符串）
      - "2026-05-13 06:04:41" / "2026/05/13" / "20260513" / ISO8601
    解析失败返回 None（由调用方计入 data_quality）。
    """
    if _is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    # 数字：时间戳或 Excel 序列号
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        return _from_numeric_time(num)

    text = str(value).strip()
    if not text:
        return None
    # 纯数字字符串
    if re.fullmatch(r"\d+(\.\d+)?", text):
        num = float(text)
        parsed = _from_numeric_time(num)
        if parsed is not None:
            return parsed
        # 形如 20260513 的日期数字
        if re.fullmatch(r"\d{8}", text):
            try:
                return datetime.strptime(text, "%Y%m%d")
            except ValueError:
                return None
        return None

    # ISO8601（含 T、时区）
    iso = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass

    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # 最后兜底：取前 19 位尝试
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19] if fmt.endswith("%S") else text[:10], fmt)
        except ValueError:
            continue
    return None


def _from_numeric_time(num: float) -> Optional[datetime]:
    if math.isnan(num) or num <= 0:
        return None
    if _EXCEL_SERIAL_MIN <= num <= _EXCEL_SERIAL_MAX:
        # Excel 1900 日期系统（含 1900-02-29 的历史 bug，对 2000 年后无影响）
        base = datetime(1899, 12, 30)
        return base + timedelta(days=num)
    if num >= _EPOCH_MIN_MS:
        try:
            return datetime.fromtimestamp(num / 1000.0)
        except (OverflowError, OSError, ValueError):
            return None
    if num >= _EPOCH_MIN_S:
        try:
            return datetime.fromtimestamp(num)
        except (OverflowError, OSError, ValueError):
            return None
    return None


_SENTIMENT_POSITIVE = {"正面", "正向", "积极", "positive", "pos", "好评", "正向情感"}
_SENTIMENT_NEGATIVE = {"负面", "负向", "消极", "negative", "neg", "差评", "负向情感"}
_SENTIMENT_NEUTRAL = {"中性", "中立", "neutral", "neu", "无明显情感", "无关"}


def normalize_sentiment(raw: Any) -> Tuple[str, str]:
    """
    情感标签归一化。

    返回 (sentiment, cleaned_raw)：
      sentiment ∈ {"positive", "neutral", "negative"}
    ai_judge 原始值可能带括号（【正面】）、空格、甚至一整句话，
    这里先精确匹配，再做包含匹配，最后回退 neutral。
    """
    text = cell_str(raw)
    cleaned = re.sub(r"[\[\]【】()（）\s]", "", text)
    low = cleaned.lower()
    if not cleaned:
        return "neutral", ""
    if low in _SENTIMENT_POSITIVE or any(k in cleaned for k in ("正面", "正向", "积极")):
        return "positive", text
    if low in _SENTIMENT_NEGATIVE or any(k in cleaned for k in ("负面", "负向", "消极")):
        return "negative", text
    if low in _SENTIMENT_NEUTRAL or any(k in cleaned for k in ("中性", "中立", "无关")):
        return "neutral", text
    if "positive" in low:
        return "positive", text
    if "negative" in low:
        return "negative", text
    return "neutral", text


# ---------------------------------------------------------------------------
# 规范化模型
# ---------------------------------------------------------------------------
@dataclass
class NormalizedDataset:
    contents: List[Dict[str, Any]] = field(default_factory=list)
    comments: List[Dict[str, Any]] = field(default_factory=list)
    source: Dict[str, Any] = field(default_factory=dict)
    data_quality: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "data_quality": self.data_quality,
            "warnings": self.warnings,
            "contents": self.contents,
            "comments": self.comments,
        }


def _sheet_rows(workbook, sheet_name: str) -> Tuple[List[str], List[Tuple[int, List[Any]]]]:
    """读取一个 sheet，返回 (表头列表, [(Excel 行号, 值列表)])。"""
    if sheet_name not in workbook.sheetnames:
        return [], []
    ws = workbook[sheet_name]
    rows = ws.iter_rows(values_only=True)
    try:
        header = next(rows)
    except StopIteration:
        return [], []
    header = [cell_str(h) for h in header]
    body: List[Tuple[int, List[Any]]] = []
    for excel_row_no, row in enumerate(rows, start=2):
        body.append((excel_row_no, list(row)))
    return header, body


def _row_dict(header: List[str], values: List[Any]) -> Dict[str, Any]:
    record: Dict[str, Any] = {}
    for idx, name in enumerate(header):
        if not name:
            continue
        record[name] = values[idx] if idx < len(values) else None
    return record


def _extract_url_id(url: Any, pattern: str) -> Optional[str]:
    url_text = cell_str(url)
    if not url_text:
        return None
    match = re.search(pattern, url_text)
    return match.group(1) if match else None


def _build_text(record: Dict[str, Any], spec: Dict[str, Any]) -> Tuple[str, str]:
    """拼正文，返回 (title, text)。"""
    title = _clean_text(cell_str(record.get(spec.get("title_col") or "")))
    parts: List[str] = []
    for col in spec.get("text_cols", []):
        val = _clean_text(cell_str(record.get(col)))
        if val and val not in parts:
            parts.append(val)
    text = "\n".join(parts).strip()
    if not title and text:
        title = text.split("\n", 1)[0][:40]
    return title, text


def _make_summary(text: str, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def load_excel(path: str) -> NormalizedDataset:
    """
    读取并规范化 Excel。

    输出 contents / comments 两条明细流 + data_quality / warnings；
    该函数不做去重之外的数据裁剪，便于对账。
    """
    abs_path = os.path.abspath(path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Excel 文件不存在：{abs_path}")

    with open(abs_path, "rb") as fh:
        sha256 = hashlib.sha256(fh.read()).hexdigest()

    workbook = load_workbook(abs_path, read_only=True, data_only=True)
    ds = NormalizedDataset()
    ds.source = {
        "excel_path": abs_path,
        "excel_sha256": sha256,
        "excel_size_bytes": os.path.getsize(abs_path),
        "sheets": list(workbook.sheetnames),
        "read_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    sheet_stats: Dict[str, int] = {}
    time_failures: List[Dict[str, Any]] = []
    sentiment_fixups: Dict[str, int] = {}
    numeric_id_risk_comments = 0
    contents_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    raw_to_canonical: Dict[Tuple[str, str], str] = {}
    duplicate_contents = 0
    content_precision_fixed = 0
    precision_examples: List[Dict[str, str]] = []
    missing_url = 0

    # ---------------- 1) 内容表 ----------------
    for platform in PLATFORM_ORDER:
        spec = CONTENT_SPECS[platform]
        header, body = _sheet_rows(workbook, spec["sheet"])
        if not header:
            ds.warnings.append(f"内容表缺失：{spec['sheet']}")
            continue
        sheet_stats[spec["sheet"]] = len(body)
        for excel_row_no, values in body:
            record = _row_dict(header, values)
            published_raw = record.get(spec["time_col"])
            published_at = parse_time(published_raw)
            if published_at is None:
                time_failures.append(
                    {"sheet": spec["sheet"], "row": excel_row_no, "column": spec["time_col"],
                     "value": cell_str(published_raw)[:40]}
                )
                continue

            raw_id = cell_str(record.get(spec["id_col"]))
            url = cell_str(record.get(spec["url_col"]))
            url_id = _extract_url_id(url, spec["url_id_pattern"])
            content_id = url_id or raw_id
            precision_fixed = bool(url_id and raw_id and url_id != raw_id)
            if precision_fixed:
                content_precision_fixed += 1
                if len(precision_examples) < 5:
                    precision_examples.append(
                        {"sheet": spec["sheet"], "row": excel_row_no,
                         "raw_id": raw_id, "url_id": url_id}
                    )
            if not url:
                missing_url += 1
            if not content_id:
                ds.warnings.append(f"{spec['sheet']} 第 {excel_row_no} 行缺少内容 ID，已跳过")
                continue

            title, text = _build_text(record, spec)
            sentiment, sentiment_raw = normalize_sentiment(record.get("ai_judge"))
            sentiment_key = sentiment_raw
            if sentiment_key and sentiment_key.strip() != {
                "positive": "正面", "negative": "负面", "neutral": "中性"
            }[sentiment]:
                sentiment_fixups[sentiment_key] = sentiment_fixups.get(sentiment_key, 0) + 1

            interactions: Dict[str, int] = {}
            for unified_key, col in spec["interactions"].items():
                interactions[unified_key] = cell_int(record.get(col))

            key = (platform, content_id)
            item = {
                "kind": "content",
                "platform": platform,
                "content_id": content_id,
                "raw_id": raw_id,
                "url": url,
                "published_at": published_at.strftime("%Y-%m-%d %H:%M:%S"),
                "published_date": published_at.strftime("%Y-%m-%d"),
                "title": title,
                "text": text,
                "summary": _make_summary(text or title),
                "author": cell_str(record.get(spec["author_col"])),
                "sentiment": sentiment,
                "sentiment_raw": sentiment_raw,
                "interactions": interactions,
                "id_precision_fixed": precision_fixed,
                "sheet": spec["sheet"],
                "row_index": excel_row_no,
            }
            if key in contents_by_key:
                duplicate_contents += 1
                continue
            contents_by_key[key] = item
            raw_to_canonical[(platform, raw_id)] = content_id

    # ---------------- 2) 评论表 ----------------
    comment_seen: set = set()
    duplicate_comments = 0
    comments_unlinked = 0
    for platform in PLATFORM_ORDER:
        spec = COMMENT_SPECS[platform]
        header, body = _sheet_rows(workbook, spec["sheet"])
        if not header:
            ds.warnings.append(f"评论表缺失：{spec['sheet']}")
            continue
        sheet_stats[spec["sheet"]] = len(body)
        for excel_row_no, values in body:
            record = _row_dict(header, values)
            published_raw = record.get(spec["time_col"])
            published_at = parse_time(published_raw)
            if published_at is None:
                time_failures.append(
                    {"sheet": spec["sheet"], "row": excel_row_no, "column": spec["time_col"],
                     "value": cell_str(published_raw)[:40]}
                )
                continue
            comment_id = cell_str(record.get(spec["id_col"]))
            if not comment_id:
                ds.warnings.append(f"{spec['sheet']} 第 {excel_row_no} 行缺少评论 ID，已跳过")
                continue
            parent_raw = cell_str(record.get(spec["parent_col"]))
            content_id = raw_to_canonical.get((platform, parent_raw))
            if not content_id:
                comments_unlinked += 1
            if platform in NUMERIC_ID_RISK_PLATFORMS:
                numeric_id_risk_comments += 1

            text = _clean_text(cell_str(record.get(spec["text_col"])))
            sentiment, sentiment_raw = normalize_sentiment(record.get("ai_judge"))
            comment_id_risk = platform in NUMERIC_ID_RISK_PLATFORMS
            key = (platform, comment_id)
            if key in comment_seen:
                duplicate_comments += 1
                continue
            comment_seen.add(key)
            ds.comments.append(
                {
                    "kind": "comment",
                    "platform": platform,
                    "comment_id": comment_id,
                    "parent_raw_id": parent_raw,
                    "content_id": content_id or "",
                    "published_at": published_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "published_date": published_at.strftime("%Y-%m-%d"),
                    "text": text,
                    "summary": _make_summary(text, 80),
                    "likes": cell_int(record.get(spec["like_col"])) if spec.get("like_col") else 0,
                    "sentiment": sentiment,
                    "sentiment_raw": sentiment_raw,
                    "id_precision_risk": comment_id_risk,
                    "sheet": spec["sheet"],
                    "row_index": excel_row_no,
                }
            )

    ds.contents = list(contents_by_key.values())
    # 按发布时间倒序，方便抽查与 TopN 稳定排序
    ds.contents.sort(key=lambda x: (x["published_at"], x["platform"]), reverse=True)
    ds.comments.sort(key=lambda x: (x["published_at"], x["platform"]), reverse=True)

    content_count = len(ds.contents)
    comment_count = len(ds.comments)
    ds.data_quality = {
        "sheet_row_counts": sheet_stats,
        "content_rows": sum(v for k, v in sheet_stats.items() if k in
                            [CONTENT_SPECS[p]["sheet"] for p in PLATFORM_ORDER]),
        "comment_rows": sum(v for k, v in sheet_stats.items() if k in
                            [COMMENT_SPECS[p]["sheet"] for p in PLATFORM_ORDER]),
        "content_items": content_count,
        "comment_items": comment_count,
        "duplicate_contents_removed": duplicate_contents,
        "duplicate_comments_removed": duplicate_comments,
        "content_id_precision_fixed": content_precision_fixed,
        "content_id_precision_examples": precision_examples,
        "comment_ids_numeric_risk": numeric_id_risk_comments,
        "numeric_id_risk_platforms": sorted(NUMERIC_ID_RISK_PLATFORMS),
        "time_parse_failures": len(time_failures),
        "time_parse_failure_examples": time_failures[:5],
        "sentiment_raw_variants": sentiment_fixups,
        "comments_unlinked": comments_unlinked,
        "missing_url": missing_url,
        "time_sources_supported": [
            "datetime/date", "Excel 序列号", "10 位秒时间戳", "13 位毫秒时间戳",
            "YYYY-MM-DD HH:MM:SS", "YYYY/MM/DD", "YYYYMMDD", "ISO8601",
        ],
    }
    if time_failures:
        ds.warnings.append(f"{len(time_failures)} 行时间字段无法解析，已跳过")
    if comments_unlinked:
        ds.warnings.append(f"{comments_unlinked} 条评论未能关联到对应内容（父 ID 不在内容表中）")
    if duplicate_comments:
        ds.warnings.append(f"评论去重 {duplicate_comments} 条")
    if duplicate_contents:
        ds.warnings.append(f"内容去重 {duplicate_contents} 条")

    workbook.close()
    return ds


def dataset_fingerprint(ds: NormalizedDataset) -> str:
    """对规范化明细做指纹，便于在 stats / HTML 中标注数据版本。"""
    h = hashlib.sha256()
    for item in sorted(ds.contents, key=lambda x: (x["platform"], x["content_id"])):
        h.update(f"C|{item['platform']}|{item['content_id']}|{item['published_at']}|{item['sentiment']}|"
                 f"{json.dumps(item['interactions'], sort_keys=True, ensure_ascii=False)}".encode("utf-8"))
    for item in sorted(ds.comments, key=lambda x: (x["platform"], x["comment_id"])):
        h.update(f"M|{item['platform']}|{item['comment_id']}|{item['published_at']}|{item['sentiment']}".encode("utf-8"))
    return h.hexdigest()


if __name__ == "__main__":  # 手工调试入口
    import argparse

    parser = argparse.ArgumentParser(description="MediaCrawler 舆情 Excel 读取/规范化调试")
    parser.add_argument("--excel", required=True, help="Excel 路径")
    parser.add_argument("--out", default="", help="可选：规范化明细 JSON 输出路径")
    args = parser.parse_args()

    data = load_excel(args.excel)
    payload = data.to_dict()
    payload["content_fingerprint"] = dataset_fingerprint(data)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已写出：{args.out}")
    else:
        print(text[:4000])
    print("\n[SUMMARY]",
          json.dumps({
              "contents": len(data.contents),
              "comments": len(data.comments),
              "quality": {k: data.data_quality[k] for k in (
                  "content_id_precision_fixed", "time_parse_failures", "comments_unlinked",
                  "duplicate_contents_removed", "duplicate_comments_removed")},
          }, ensure_ascii=False))

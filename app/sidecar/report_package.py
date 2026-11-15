# -*- coding: utf-8 -*-
"""
report_package.py — `public-sentiment-data@v1` 数据包组装 + schema 校验（P1-1 拆分自 report_data.py）

职责：
  1. 调用 `report_dataset.load_report_dataset()` 取窗口数据，`report_sentiment.analyze_dataset_sentiment()`
     打情感，再走 `app/report_pipeline/compute_stats` 生成确定性 stats；
  2. 组装数据包（`schema_version/package_id/generated_at/source/report/counts/stats/samples/contents/meta`）；
  3. **schema 校验**：`validate_report_package()` 逐字段检查类型与必填键，缺字段/类型不对
     直接抛 `ReportPackageInvalid`（错误码 `REPORT_PACKAGE_INVALID`），不再把坏包交给报告管线。

数据包结构用 `TypedDict` 建模（不引入 pydantic 依赖）：`ReportPackage` 及其子块给出
`meta/counts/stats/samples` 的键约束，`build_report_package()` 的返回类型即 `ReportPackage`。

窗口口径：
  - `window_start` / `window_end` 为 ISO 日期字符串（含当天）：start 00:00:00 ~ end 23:59:59，
    timezone Asia/Shanghai（与数据包方案一致）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TypedDict

# 同目录模块导入（sidecar 以脚本/模块两种方式启动都可用）
_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

from version import APP_VERSION  # noqa: E402  (版本号唯一来源，见同目录 version.py)

from report_common import (
    CONTENT_SPECS,
    DB_SCHEMA,
    DEFAULT_PLATFORMS,
    LABEL_TO_CODE,
    PLATFORM_LABELS,
    SCHEMA_VERSION,
    SENTIMENT_CN,
    TIMEZONE,
    TZ,
    WINDOW_FIELD_DESCRIPTION,
    _WEEKDAY_CN,
    _file_sha256,
    _normalize_platform,
    _truncate,
    _unique,
    load_poc_modules,
    parse_publish_time,
    parse_window,
)
from report_dataset import load_report_dataset
from report_sentiment import analyze_dataset_sentiment


# ---------------------------------------------------------------------------
# 数据包结构（TypedDict）：字段与 `public-sentiment-data@v1` 一一对应
# ---------------------------------------------------------------------------
class WindowDict(TypedDict):
    mode: str
    start: str
    end: str
    timezone: str
    label: str


class ReportBlock(TypedDict):
    brand: str
    title: str
    window: WindowDict
    keywords: List[str]
    platforms: List[str]


class CountsRow(TypedDict):
    platform: str
    content: int
    comments: int


class CountsBlock(TypedDict):
    content: int
    comments: int
    total: int
    by_platform: List[CountsRow]


class ContentSample(TypedDict):
    """`samples.negative` / `samples.positive` 的单条样例。"""

    platform: str
    content_id: str
    title: str
    summary: str
    url: str
    published_at: str
    metrics: Dict[str, int]
    sentiment: str
    heat: float


class NegativeCommentSample(TypedDict):
    platform: str
    content: str
    likes: int
    published_at: str


class TopCommentSample(TypedDict):
    platform: str
    content_id: str
    content: str
    likes: int
    sentiment: str


class SamplesBlock(TypedDict):
    negative: List[ContentSample]
    negative_comments: List[NegativeCommentSample]
    positive: List[ContentSample]
    top_comments: List[TopCommentSample]
    hot_keywords: List[str]


class ContentDigest(TypedDict):
    """`contents` 列表的单条内容摘要（比样例更精简）。"""

    platform: str
    content_id: str
    title: str
    published_at: str
    sentiment: str
    heat: float
    url: str


class TotalsBlock(TypedDict):
    items: int
    content: int
    comments: int


class StatsBlock(TypedDict):
    """`stats`（compute_stats 产出）：只约束报告层依赖的键，其余键透传。"""

    schema_version: str
    totals: TotalsBlock
    sentiment: Dict[str, Any]
    risk: Dict[str, Any]
    platform_ranking: List[Dict[str, Any]]
    trend: Dict[str, Any]
    data_quality: Dict[str, Any]
    samples: Dict[str, Any]
    keywords: Dict[str, Any]
    warnings: List[str]
    stats_fingerprint: str
    source: Dict[str, Any]


class PackageSource(TypedDict):
    app: str
    app_version: str
    db_schema: str


class TruncatedBlock(TypedDict):
    contents: int
    samples: int
    long_text: int


class FilteringBlock(TypedDict):
    window_field: str
    dedup: str
    exclude: List[str]
    truncated: TruncatedBlock


class MetaBlock(TypedDict):
    stats_fingerprint: str
    filtering: FilteringBlock
    data_freshness: str
    sentiment: Dict[str, Any]


class _ReportPackageRequired(TypedDict):
    schema_version: str
    package_id: str
    generated_at: str
    source: PackageSource
    report: ReportBlock
    counts: CountsBlock
    stats: StatsBlock
    samples: SamplesBlock
    contents: List[ContentDigest]
    meta: MetaBlock


class ReportPackage(_ReportPackageRequired, total=False):
    """完整数据包（`instruction` 可选，其余键必填）。"""

    instruction: str


# ---------------------------------------------------------------------------
# schema 校验
# ---------------------------------------------------------------------------
REPORT_PACKAGE_INVALID = "REPORT_PACKAGE_INVALID"


class ReportPackageInvalid(ValueError):
    """数据包结构不符合 `public-sentiment-data@v1`（缺字段 / 类型不对）。"""

    code = REPORT_PACKAGE_INVALID

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(str(item) for item in problems)
        detail = "；".join(self.problems[:8])
        super().__init__(f"{REPORT_PACKAGE_INVALID}: {detail}")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _need_mapping(value: Any, path: str, problems: List[str]) -> bool:
    if not isinstance(value, dict):
        problems.append(f"{path}：期望 object，实际 {type(value).__name__}")
        return False
    return True


def _need_list(value: Any, path: str, problems: List[str]) -> bool:
    if not isinstance(value, list):
        problems.append(f"{path}：期望 array，实际 {type(value).__name__}")
        return False
    return True


def _need_str(value: Any, path: str, problems: List[str], *, allow_empty: bool = False) -> bool:
    if not isinstance(value, str):
        problems.append(f"{path}：期望 string，实际 {type(value).__name__}")
        return False
    if not allow_empty and not value.strip():
        problems.append(f"{path}：期望非空字符串")
        return False
    return True


def _need_int(value: Any, path: str, problems: List[str]) -> bool:
    if not isinstance(value, int) or isinstance(value, bool):
        problems.append(f"{path}：期望 integer，实际 {type(value).__name__}")
        return False
    return True


def _need_number(value: Any, path: str, problems: List[str]) -> bool:
    if not _is_number(value):
        problems.append(f"{path}：期望 number，实际 {type(value).__name__}")
        return False
    return True


def _validate_window(window: Any, problems: List[str]) -> None:
    if not _need_mapping(window, "report.window", problems):
        return
    for key in ("mode", "start", "end", "timezone", "label"):
        _need_str(window.get(key), f"report.window.{key}", problems)


def _validate_counts(counts: Any, problems: List[str]) -> None:
    if not _need_mapping(counts, "counts", problems):
        return
    if not all(_need_int(counts.get(key), f"counts.{key}", problems)
               for key in ("content", "comments", "total")):
        return
    if counts["total"] != counts["content"] + counts["comments"]:
        problems.append(
            f"counts.total：应等于 content+comments（{counts['total']} != "
            f"{counts['content']}+{counts['comments']}）"
        )
    rows = counts.get("by_platform")
    if not _need_list(rows, "counts.by_platform", problems):
        return
    for index, row in enumerate(rows):
        path = f"counts.by_platform[{index}]"
        if not _need_mapping(row, path, problems):
            continue
        _need_str(row.get("platform"), f"{path}.platform", problems)
        _need_int(row.get("content"), f"{path}.content", problems)
        _need_int(row.get("comments"), f"{path}.comments", problems)


def _validate_stats(stats: Any, problems: List[str]) -> None:
    if not _need_mapping(stats, "stats", problems):
        return
    if not stats:
        problems.append("stats：空对象（应包含 totals/sentiment/risk/trend 等统计块）")
        return
    _need_str(stats.get("schema_version"), "stats.schema_version", problems)
    _need_str(stats.get("stats_fingerprint"), "stats.stats_fingerprint", problems)
    totals = stats.get("totals")
    if _need_mapping(totals, "stats.totals", problems):
        for key in ("items", "content", "comments"):
            _need_int(totals.get(key), f"stats.totals.{key}", problems)
        if isinstance(totals.get("items"), int) and not isinstance(totals.get("items"), bool):
            expected = None
            if isinstance(totals.get("content"), int) and isinstance(totals.get("comments"), int):
                expected = totals["content"] + totals["comments"]
            if expected is not None and totals["items"] != expected:
                problems.append(
                    f"stats.totals.items：应等于 content+comments（{totals['items']} != {expected}）"
                )
    sentiment = stats.get("sentiment")
    if _need_mapping(sentiment, "stats.sentiment", problems):
        overall = sentiment.get("overall")
        if _need_mapping(overall, "stats.sentiment.overall", problems):
            for label in ("positive", "neutral", "negative"):
                bucket = overall.get(label)
                if _need_mapping(bucket, f"stats.sentiment.overall.{label}", problems):
                    _need_int(bucket.get("count"), f"stats.sentiment.overall.{label}.count", problems)
                    _need_number(bucket.get("ratio"), f"stats.sentiment.overall.{label}.ratio", problems)
    for key in ("risk", "trend", "data_quality", "samples", "keywords", "source"):
        _need_mapping(stats.get(key), f"stats.{key}", problems)
    for key in ("platform_ranking", "warnings"):
        _need_list(stats.get(key), f"stats.{key}", problems)
    if isinstance(stats.get("warnings"), list):
        for index, item in enumerate(stats["warnings"]):
            if not isinstance(item, str):
                problems.append(f"stats.warnings[{index}]：期望 string，实际 {type(item).__name__}")


def _validate_samples(samples: Any, problems: List[str]) -> None:
    if not _need_mapping(samples, "samples", problems):
        return
    for key in ("negative", "negative_comments", "positive", "top_comments", "hot_keywords"):
        if key not in samples:
            problems.append(f"samples.{key}：缺失")
    for key in ("negative", "negative_comments", "positive", "top_comments"):
        items = samples.get(key)
        if items is None or not _need_list(items, f"samples.{key}", problems):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                problems.append(f"samples.{key}[{index}]：期望 object，实际 {type(item).__name__}")
    keywords = samples.get("hot_keywords")
    if isinstance(keywords, list):
        for index, item in enumerate(keywords):
            if not isinstance(item, str):
                problems.append(f"samples.hot_keywords[{index}]：期望 string，实际 {type(item).__name__}")


def _validate_contents(contents: Any, problems: List[str]) -> None:
    if not _need_list(contents, "contents", problems):
        return
    for index, item in enumerate(contents):
        path = f"contents[{index}]"
        if not _need_mapping(item, path, problems):
            continue
        _need_str(item.get("content_id"), f"{path}.content_id", problems)
        _need_str(item.get("sentiment"), f"{path}.sentiment", problems)


def _validate_meta(meta: Any, problems: List[str]) -> None:
    if not _need_mapping(meta, "meta", problems):
        return
    if _need_str(meta.get("stats_fingerprint"), "meta.stats_fingerprint", problems):
        if not str(meta["stats_fingerprint"]).startswith("sha256:"):
            problems.append("meta.stats_fingerprint：应为 'sha256:<hex>' 形式")
    _need_str(meta.get("data_freshness"), "meta.data_freshness", problems, allow_empty=True)
    _need_mapping(meta.get("sentiment"), "meta.sentiment", problems)
    filtering = meta.get("filtering")
    if _need_mapping(filtering, "meta.filtering", problems):
        _need_str(filtering.get("window_field"), "meta.filtering.window_field", problems)
        _need_str(filtering.get("dedup"), "meta.filtering.dedup", problems)
        _need_list(filtering.get("exclude"), "meta.filtering.exclude", problems)
        truncated = filtering.get("truncated")
        if _need_mapping(truncated, "meta.filtering.truncated", problems):
            for key in ("contents", "samples", "long_text"):
                _need_int(truncated.get(key), f"meta.filtering.truncated.{key}", problems)


def validate_report_package(package: Any) -> ReportPackage:
    """
    校验数据包结构（`public-sentiment-data@v1`），返回同一对象；不合格抛 `ReportPackageInvalid`。

    校验范围：顶层必填键与类型、`report.window`、`counts`（含 total = content+comments、by_platform 行）、
    `stats`（totals/sentiment/risk/trend/data_quality/samples/keywords/platform_ranking/warnings/
    stats_fingerprint）、`samples`（五类）、`contents` 条目、`meta`（fingerprint/filtering/sentiment）。
    一次性收集所有问题，便于定位。
    """
    problems: List[str] = []
    if not _need_mapping(package, "package", problems):
        raise ReportPackageInvalid(problems)

    if _need_str(package.get("schema_version"), "schema_version", problems):
        if package["schema_version"] != SCHEMA_VERSION:
            problems.append(
                f"schema_version：期望 {SCHEMA_VERSION}，实际 {package['schema_version']!r}"
            )
    _need_str(package.get("package_id"), "package_id", problems)
    _need_str(package.get("generated_at"), "generated_at", problems)

    source = package.get("source")
    if _need_mapping(source, "source", problems):
        for key in ("app", "app_version", "db_schema"):
            _need_str(source.get(key), f"source.{key}", problems)

    report = package.get("report")
    if _need_mapping(report, "report", problems):
        _need_str(report.get("brand"), "report.brand", problems, allow_empty=True)
        _need_str(report.get("title"), "report.title", problems, allow_empty=True)
        _validate_window(report.get("window"), problems)
        _need_list(report.get("keywords"), "report.keywords", problems)
        _need_list(report.get("platforms"), "report.platforms", problems)

    _validate_counts(package.get("counts"), problems)
    _validate_stats(package.get("stats"), problems)
    _validate_samples(package.get("samples"), problems)
    _validate_contents(package.get("contents"), problems)
    _validate_meta(package.get("meta"), problems)

    instruction = package.get("instruction")
    if instruction is not None and not isinstance(instruction, str):
        problems.append(f"instruction：期望 string，实际 {type(instruction).__name__}")

    if problems:
        raise ReportPackageInvalid(problems)
    return package


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
# 数据包组装（样例 / 摘要 / counts）
# ---------------------------------------------------------------------------
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


def _resolve_app_db_path(db_path: str, *, data_dir: str = "", app_db_path: str = "") -> str:
    """确定 app.db 路径：显式参数 > data_dir/app.db > <db_path 的上两级>/app.db。"""
    if app_db_path:
        return os.path.abspath(app_db_path)
    if data_dir:
        return os.path.abspath(os.path.join(data_dir, "app.db"))
    db_parent = Path(os.path.abspath(db_path)).parent  # <data-dir>/crawler
    return str(db_parent.parent / "app.db")


# ---------------------------------------------------------------------------
# 数据包组装
# ---------------------------------------------------------------------------
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
) -> ReportPackage:
    """
    构建「公共舆情数据包 v1」（`public-sentiment-data@v1`）。

    参数：
      db_path            MediaCrawler sqlite（只读）；
      window_start/end   发布时间窗口 ISO 日期（含当天，Asia/Shanghai）；
      keywords           关键词列表（内容 source_keyword 召回 + 标题/正文/nickname 相关度过滤）；
      platforms          平台 code 子集，默认全部 7 个；
      sentiment_analyzer 批量情感回调（见 `report_sentiment` docstring），None/异常按 neutral 兜底；
      report_date        报告日期，默认 = window_end；
      data_dir           exe 数据目录（用于定位 app.db；默认由 db_path 推导 <data-dir>）；
      app_db_path        显式 app.db 路径（优先级最高，测试可用临时库）；
      batch_size         情感分析每批文本数（≤200）；
      include_contents   是否在包中附带 contents 摘要（≤max_contents）。

    返回：`ReportPackage`（结构见本模块 TypedDict）；返回前会过 `validate_report_package()`，
    结构不合格抛 `ReportPackageInvalid`（REPORT_PACKAGE_INVALID）。
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
    return validate_report_package(package)


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

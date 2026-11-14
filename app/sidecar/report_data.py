# -*- coding: utf-8 -*-
"""
report_data.py — exe「生成周报」数据层**门面**（P1-1 拆分后的对外入口）

本模块只做**重新导出**，实现已按职责拆成三个子模块：

  - `report_dataset.py`    SQLite → `NormalizedDataset`（读内容/评论、窗口过滤、去重、相关性标记）
  - `report_package.py`    `public-sentiment-data@v1` 组装 + schema 校验（`REPORT_PACKAGE_INVALID`）
  - `report_sentiment.py`  情感批处理 / `sentiment_cache` 缓存 / 失败 neutral 兜底

另有一个公共底座 `report_common.py`（平台注册表 `DEFAULT_PLATFORMS` / `PLATFORM_LABELS`、
时间与文本工具、只读 DB 打开、报告流水线定位）。

对外名字保持不变（测试与 `report_manager` / `run_feed` / `exporter` 依赖）：
  `build_report_package` / `load_report_dataset` / `load_sentiment_cache` /
  `upsert_sentiment` / `upsert_sentiments` / `normalize_sentiment_label` /
  `DEFAULT_PLATFORMS` / `PLATFORM_LABELS` / `SENTIMENT_CN` / `SCHEMA_VERSION` /
  `APP_VERSION`（来自 `version.py`）/ `CONTENT_SPECS` / `COMMENT_SPECS` / `TZ` /
  `parse_publish_time` / `parse_window` / `poc_pipeline_dir` / `load_poc_modules` 等。

情感回调契约（与 app-sidecar-dev 对齐）：
  analyzer(texts: list[{"id": str, "text": str}]) -> list[{"id": str,
                                                          "sentiment": "positive|neutral|negative|正面|中性|负面",
                                                          "confidence": number, "reason": str}]

窗口口径：
  - `window_start` / `window_end` 为 ISO 日期字符串（含当天）：start 00:00:00 ~ end 23:59:59，
    timezone Asia/Shanghai（与数据包方案一致）；
  - 内容按**发布时间**进窗口；评论只取“窗口内内容的评论”（评论时间不单独设窗口，父内容在窗口内即可）。

命令行（调试/联调）：
  python report_data.py --db-path <sqlite> --window-start 2026-11-03 --window-end 2026-11-09
"""

from __future__ import annotations

import sys
from pathlib import Path

# 同目录模块导入（sidecar 以脚本/模块两种方式启动都可用）
_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

from version import APP_VERSION  # noqa: E402

from report_common import (  # noqa: E402
    COMMENT_SPECS,
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
    _SENTIMENT_ALIASES,
    _WEEKDAY_CN,
    _db_uri,
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
    poc_pipeline_dir,
)
from report_dataset import (  # noqa: E402
    _dedupe_latest,
    _haystack,
    _keyword_relevant,
    _keyword_selected,
    _row_author,
    _row_interactions,
    _row_text,
    _row_time,
    _row_title,
    _row_url,
    load_report_dataset,
)
from report_sentiment import (  # noqa: E402
    SENTIMENT_CACHE_SCHEMA,
    SENTIMENT_CACHE_TABLE,
    _sentiment_text,
    analyze_dataset_sentiment,
    connect_app_db,
    load_sentiment_cache,
    normalize_sentiment_label,
    upsert_sentiment,
    upsert_sentiments,
)
from report_package import (  # noqa: E402
    REPORT_PACKAGE_INVALID,
    ContentDigest,
    ContentSample,
    CountsBlock,
    CountsRow,
    FilteringBlock,
    MetaBlock,
    NegativeCommentSample,
    PackageSource,
    ReportBlock,
    ReportPackage,
    ReportPackageInvalid,
    SamplesBlock,
    StatsBlock,
    TopCommentSample,
    TotalsBlock,
    TruncatedBlock,
    WindowDict,
    _build_counts,
    _build_contents,
    _build_samples,
    _empty_stats,
    _event_metrics,
    _platform_code,
    _resolve_app_db_path,
    _sample_from_content,
    _stub_analyzer,
    _window_label,
    build_report_package,
    main,
    validate_report_package,
)


__all__ = [
    # ---- 数据包组装 / 校验 ----
    "build_report_package",
    "validate_report_package",
    "ReportPackage",
    "ReportPackageInvalid",
    "REPORT_PACKAGE_INVALID",
    "ReportBlock",
    "WindowDict",
    "CountsBlock",
    "CountsRow",
    "SamplesBlock",
    "ContentSample",
    "ContentDigest",
    "NegativeCommentSample",
    "TopCommentSample",
    "StatsBlock",
    "TotalsBlock",
    "PackageSource",
    "FilteringBlock",
    "TruncatedBlock",
    "MetaBlock",
    # ---- 数据集 ----
    "load_report_dataset",
    # ---- 情感 ----
    "load_sentiment_cache",
    "upsert_sentiment",
    "upsert_sentiments",
    "analyze_dataset_sentiment",
    "normalize_sentiment_label",
    "connect_app_db",
    "SENTIMENT_CACHE_TABLE",
    "SENTIMENT_CACHE_SCHEMA",
    # ---- 常量 / 平台注册表 ----
    "APP_VERSION",
    "SCHEMA_VERSION",
    "DB_SCHEMA",
    "TIMEZONE",
    "TZ",
    "DEFAULT_PLATFORMS",
    "PLATFORM_LABELS",
    "LABEL_TO_CODE",
    "SENTIMENT_CN",
    "CONTENT_SPECS",
    "COMMENT_SPECS",
    "WINDOW_FIELD_DESCRIPTION",
    # ---- 工具（时间 / 文本 / 关键词 / DB）----
    "parse_publish_time",
    "parse_window",
    "keyword_tokens",
    "open_media_db",
    "poc_pipeline_dir",
    "load_poc_modules",
    "main",
    # ---- 内部工具（run_feed / 测试直接引用，保持可用）----
    "_is_blank",
    "_text",
    "_to_int",
    "_to_epoch_ms",
    "_truncate",
    "_unique",
    "_fmt_dt",
    "_normalize_platform",
    "_file_sha256",
    "_db_uri",
    "_read_rows",
    "_table_exists",
    "_SENTIMENT_ALIASES",
    "_WEEKDAY_CN",
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
    "_sentiment_text",
    "_build_counts",
    "_build_contents",
    "_build_samples",
    "_empty_stats",
    "_event_metrics",
    "_platform_code",
    "_resolve_app_db_path",
    "_sample_from_content",
    "_stub_analyzer",
    "_window_label",
]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

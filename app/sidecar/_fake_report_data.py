# -*- coding: utf-8 -*-
"""selftest 替身：report_data.build_report_package（真实模块见同目录 ``report_data.py``）。

只在 sidecar 自测中通过 ``SIDECAR_REPORT_DATA_MODULE=_fake_report_data`` 注入，不参与生产。
返回一个结构合法的最小 ``public-sentiment-data@v1`` 数据包：
- 会真实调用传入的 ``sentiment_analyzer``（即 LocalLLMClient，走本地 fake LLM），
  用于验证 sidecar → 用户模型 API 的情感批量打标调用链；
- analyzer 异常时按 neutral 兜底（只记录，不抛错）。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Sequence

DEFAULT_PLATFORMS = ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]


def build_report_package(
    db_path: str,
    window_start: str,
    window_end: str,
    keywords: Sequence[str] = (),
    platforms: Sequence[str] = DEFAULT_PLATFORMS,
    sentiment_analyzer: Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]] | None = None,
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
) -> Dict[str, Any]:
    """返回最小合法数据包；真实统计/情感缓存由 report_data.py 实现。"""
    sampled_texts = [
        {"id": "fake_c1", "text": "示例科技在基础设施安全监测领域表现积极，好评较多"},
        {"id": "fake_c2", "text": "有用户反馈相关项目交付进度偏慢，体验一般"},
        {"id": "fake_c3", "text": "某某示例科技获得政策支持，行业关注度上升"},
    ]
    sentiment_rows: List[Dict[str, Any]] = []
    analyzer_error = ""
    if sentiment_analyzer is not None:
        try:
            sentiment_rows = sentiment_analyzer(sampled_texts) or []
        except Exception as exc:  # noqa: BLE001 - 与真实 report_data 一致：fallback 不阻断
            analyzer_error = f"{type(exc).__name__}: {exc}"
            sentiment_rows = []
    analyzed = len(sentiment_rows)
    positive = sum(1 for row in sentiment_rows if row.get("sentiment") == "positive")
    negative = sum(1 for row in sentiment_rows if row.get("sentiment") == "negative")
    neutral = analyzed - positive - negative
    warnings = []
    if analyzer_error:
        warnings.append(f"情感分析不可用，按 neutral 兜底：{analyzer_error}")
    elapsed = int(time.time() * 1000)
    return {
        "schema_version": "public-sentiment-data@v1",
        "package_id": f"pkg_fake_{elapsed}",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {"app": "opinionlens-public-opinion", "app_version": "selftest-fake"},
        "report": {
            "brand": brand,
            "title": title,
            "window": {
                "mode": "publish_time",
                "start": str(window_start),
                "end": str(window_end),
                "timezone": "Asia/Shanghai",
                "label": f"{window_start}~{window_end}",
            },
            "keywords": list(keywords),
            "platforms": list(platforms),
        },
        "counts": {
            "content": 3,
            "comments": 1,
            "total": 4,
            "by_platform": [
                {"platform": code, "content": 0, "comments": 0} for code in platforms
            ],
        },
        "stats": {
            "data_quality": {
                "sentiment": {
                    "analyzed": analyzed,
                    "positive": positive,
                    "neutral": neutral,
                    "negative": negative,
                    "fallback": bool(analyzer_error),
                }
            },
            "warnings": warnings,
        },
        "samples": {
            "negative": [],
            "positive": [],
            "top_comments": [],
            "hot_keywords": [str(item) for item in list(keywords)[:5]],
        },
        "contents": [],
        "meta": {
            "stats_fingerprint": "sha256:fake_" + str(elapsed),
            "filtering": {
                "window_start": str(window_start),
                "window_end": str(window_end),
                "platforms": list(platforms),
            },
            "data_freshness": "selftest-fake",
            "sentiment_batches": 0 if not sentiment_rows else (analyzed + batch_size - 1) // batch_size,
        },
        "_fake": {
            "db_path": str(db_path),
            "data_dir": str(data_dir),
            "app_db_path": str(app_db_path),
            "report_date": str(report_date or window_end),
            "sentiment_rows": sentiment_rows,
            "analyzer_error": analyzer_error,
        },
    }


def load_sentiment_cache(*args: Any, **kwargs: Any) -> Dict[str, Any]:  # pragma: no cover - 接口占位
    """真实 report_data 才有；替身不缓存（sidecar 编排层不调用）。"""
    return {}


def upsert_sentiment(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - 接口占位
    return None

# -*- coding: utf-8 -*-
"""
test_report_data.py — `report_data.py` 离线自测（不联网、不启动服务）

覆盖：
  1. 窗口过滤（含边界 00:00:00 / 23:59:59）、关键词召回 + 文本相关度过滤、off_brand 排除；
  2. 时间归一（毫秒/秒/字符串）、缺时间行计入 data_quality；
  3. 评论只跟随窗口内父内容（父内容不在窗口 → 评论不入包；评论时间异常回退父时间）；
  4. 去重（platform+content_id / comment_id，保留最新 last_modify_ts 行）；
  5. 构造 POC NormalizedDataset + compute_stats：
     - counts 与 stats.totals 一致；stats 覆盖 7 平台（含贴吧）；stats_fingerprint 可复算；
  6. 情感分析：
     - analyzer 批量调用、文本去重、结果写回 stats/samples 并缓存到 sentiment_cache；
     - 第二次调用命中缓存 → analyzer 不被再次调用；stats/fingerprint 稳定；
     - analyzer 抛异常 → neutral 兜底 + warnings（不阻断），fallback 不写缓存；
     - analyzer 未配置 → neutral 兜底 + warning；
  7. 数据包 `public-sentiment-data@v1` 结构（report/counts/stats/samples/contents/meta）；
  8. 窗口内无数据 → 返回合法空包（counts=0 / stats.totals=0）；
  9. load_sentiment_cache / upsert_sentiment 基本行为；情感标签归一化。

运行：
  MediaCrawler\\.venv\\Scripts\\python.exe app\\sidecar\\test_report_data.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]  # tests/python/x.py -> 仓库根
FIXTURES_DIR = ROOT / "tests" / "fixtures"
SIDECAR_DIR = ROOT / "app" / "sidecar"
ENGINE_DIR = ROOT / "app" / "engine"
for _path in (FIXTURES_DIR, SIDECAR_DIR, ENGINE_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
HERE = str(SIDECAR_DIR)  # 读取/引用产品模块仍按 app/sidecar 解析
PROJECT_ROOT = str(ROOT)

from report_data import (  # noqa: E402
    DEFAULT_PLATFORMS,
    PLATFORM_LABELS,
    SENTIMENT_CACHE_TABLE,
    SENTIMENT_CN,
    build_report_package,
    load_poc_modules,
    load_report_dataset,
    load_sentiment_cache,
    normalize_sentiment_label,
    parse_publish_time,
    parse_window,
    poc_pipeline_dir,
    upsert_sentiment,
)
from exporter import format_time  # noqa: E402

TZ = timezone(timedelta(hours=8))
WINDOW_START = "2026-11-03"
WINDOW_END = "2026-11-09"
KEYWORDS = ["示例科技", "示例"]

_failures = 0
_passes = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global _failures, _passes
    if condition:
        _passes += 1
    else:
        _failures += 1
        print(f"  [FAIL] {name}" + (f"：{detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n[{title}]")


# ---------------------------------------------------------------------------
# fixture sqlite（当前 MediaCrawler ORM 的最小列集合）
# ---------------------------------------------------------------------------
TABLE_COLUMNS: Dict[str, List[str]] = {
    "xhs_note": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "title", "desc",
                 "time", "last_update_time", "liked_count", "collected_count", "comment_count",
                 "share_count", "note_url", "source_keyword"],
    "xhs_note_comment": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "note_id",
                         "content", "create_time", "like_count"],
    "douyin_aweme": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "aweme_id", "title", "desc",
                     "create_time", "liked_count", "comment_count", "share_count", "collected_count",
                     "aweme_url", "source_keyword"],
    "douyin_aweme_comment": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id",
                             "aweme_id", "content", "create_time", "like_count"],
    "kuaishou_video": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "video_id", "title", "desc",
                       "create_time", "liked_count", "viewd_count", "video_url", "source_keyword"],
    "kuaishou_video_comment": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id",
                               "video_id", "content", "create_time"],
    "bilibili_video": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "video_id", "title", "desc",
                       "create_time", "liked_count", "video_comment", "video_share_count",
                       "video_favorite_count", "video_coin_count", "video_play_count",
                       "video_url", "source_keyword"],
    "bilibili_video_comment": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id",
                               "video_id", "content", "create_time", "like_count"],
    "weibo_note": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "content",
                   "create_time", "create_date_time", "liked_count", "comments_count",
                   "shared_count", "note_url", "source_keyword"],
    "weibo_note_comment": ["creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id",
                           "note_id", "content", "create_time", "create_date_time",
                           "comment_like_count"],
    "tieba_note": ["creator_hash", "user_nickname", "add_ts", "last_modify_ts", "note_id", "title", "desc",
                   "publish_time", "note_url", "source_keyword", "total_replay_num"],
    "tieba_comment": ["creator_hash", "user_nickname", "add_ts", "last_modify_ts", "comment_id",
                      "note_id", "content", "publish_time"],
    "zhihu_content": ["creator_hash", "user_nickname", "add_ts", "last_modify_ts", "content_id", "title",
                      "desc", "content_text", "created_time", "content_url", "voteup_count",
                      "comment_count", "source_keyword"],
    "zhihu_comment": ["creator_hash", "user_nickname", "add_ts", "last_modify_ts", "comment_id",
                      "content_id", "content", "publish_time", "like_count"],
}


def _create_table(conn: sqlite3.Connection, table: str) -> None:
    columns = TABLE_COLUMNS[table]
    defs = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    for column in columns:
        if column in ("add_ts", "last_modify_ts"):
            defs.append(f'"{column}" INTEGER')
        else:
            defs.append(f'"{column}" TEXT')
    conn.execute(f'CREATE TABLE "{table}" ({", ".join(defs)})')


def _insert(conn: sqlite3.Connection, table: str, rows: Sequence[Dict[str, Any]]) -> None:
    columns = TABLE_COLUMNS[table]
    insert_columns = [column for column in columns if column in set().union(*[set(r) for r in rows])]
    sql = f'INSERT INTO "{table}" ({",".join(chr(34)+c+chr(34) for c in insert_columns)}) ' \
          f'VALUES ({",".join(["?"] * len(insert_columns))})'
    conn.executemany(sql, [[row.get(column) for column in insert_columns] for row in rows])


def _epoch_seconds(text: str) -> int:
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())


def _epoch_ms(text: str) -> int:
    return _epoch_seconds(text) * 1000


def build_fixture_media_db(path: str) -> None:
    """构造覆盖 7 平台的最小 MediaCrawler 表结构 + 窗口内/外/异常数据。"""
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    try:
        for table in TABLE_COLUMNS:
            _create_table(conn, table)
        common = {"creator_hash": "u1", "nickname": "作者", "add_ts": _epoch_ms("2026-11-09 10:00:00"),
                  "last_modify_ts": _epoch_ms("2026-11-09 10:00:00")}
        # xhs：窗口内 2 条（含起始边界）、窗口外 1、off_brand 1、缺时间 1、关键词不匹配 1、去重 1
        _insert(conn, "xhs_note", [
            dict(common, note_id="x1", title="示例科技新品发布", desc="正文内容 正面",
                 time="2026-11-05 10:00:00", note_url="https://xhs/1", source_keyword="示例科技",
                 liked_count=10, comment_count=2, collected_count=3, share_count=4),
            dict(common, note_id="x2", title="示例科技获奖", desc="获奖新闻",
                 time="2026-11-03 00:00:00", note_url="https://xhs/2", source_keyword="示例",
                 liked_count=5),
            dict(common, note_id="x3", title="示例科技 窗口外", desc="窗口外内容",
                 time="2026-11-10 00:00:00", note_url="https://xhs/3", source_keyword="示例科技"),
            dict(common, note_id="x4", title="行业新闻", desc="完全无关内容",
                 time="2026-11-04 12:00:00", note_url="https://xhs/4", source_keyword="示例科技"),
            dict(common, note_id="x5", title="示例科技 缺时间", desc="",
                 time="", note_url="https://xhs/5", source_keyword="示例科技"),
            dict(common, note_id="x6", title="示例科技 关键词不匹配", desc="",
                 time="2026-11-06 08:00:00", note_url="https://xhs/6", source_keyword="其他词"),
            dict(common, note_id="x1", title="示例科技新品发布（旧快照）", desc="旧",
                 time="2026-11-05 10:00:00", note_url="https://xhs/1old", source_keyword="示例科技",
                 last_modify_ts=_epoch_ms("2026-11-05 09:00:00")),
        ])
        _insert(conn, "xhs_note_comment", [
            dict(common, comment_id="c1", note_id="x1", content="加油 正面", create_time="2026-11-05 11:00:00",
                 like_count=3),
            dict(common, comment_id="c2", note_id="x3", content="窗口外父内容的评论",
                 create_time="2026-11-05 11:00:00", like_count=0),
            dict(common, comment_id="c3", note_id="x1", content="评论时间缺失", create_time="", like_count=0),
        ])
        # 抖音：create_time 为秒级字符串
        _insert(conn, "douyin_aweme", [
            dict(common, aweme_id="dy1", title="示例科技 抖音视频", desc="负面 吐槽",
                 create_time=str(_epoch_seconds("2026-11-07 09:00:00")), aweme_url="https://dy/1",
                 source_keyword="示例科技", liked_count=8),
        ])
        _insert(conn, "douyin_aweme_comment", [
            dict(common, comment_id="dyc1", aweme_id="dy1", content="视频评论",
                 create_time="2026-11-07 10:00:00", like_count=2),
        ])
        # 快手：create_time 为毫秒整数
        _insert(conn, "kuaishou_video", [
            dict(common, video_id="ks1", title="示例科技 快手合作", desc="合作内容",
                 create_time=_epoch_ms("2026-11-08 09:00:00"), video_url="https://ks/1",
                 source_keyword="示例科技", liked_count=6, viewd_count=100),
        ])
        # B站：create_time 秒级 int
        _insert(conn, "bilibili_video", [
            dict(common, video_id="bili1", title="示例科技 宣传片", desc="",
                 create_time=_epoch_seconds("2026-11-08 10:00:00"), video_url="https://bili/1",
                 source_keyword="示例", liked_count=20, video_play_count=300),
        ])
        # 微博：create_time 秒级 + create_date_time 字符串；一条 23:59:59 边界
        _insert(conn, "weibo_note", [
            dict(common, note_id="wb1", content="示例科技 微博 正面 获奖",
                 create_time=_epoch_seconds("2026-11-09 23:59:59"),
                 create_date_time="2026-11-09 23:59:59", note_url="https://wb/1",
                 source_keyword="示例科技", liked_count=12),
        ])
        _insert(conn, "weibo_note_comment", [
            dict(common, comment_id="wbc1", note_id="wb1", content="微博评论",
                 create_time="2026-11-09 22:00:00", comment_like_count=4),
        ])
        # 贴吧：publish_time 字符串
        _insert(conn, "tieba_note", [
            dict(common, user_nickname="贴吧网友", note_id="tb1", title="示例科技 讨论帖", desc="贴吧内容",
                 publish_time="2026-11-08 20:00:00", note_url="https://tieba/1",
                 source_keyword="示例科技", total_replay_num=5),
        ])
        _insert(conn, "tieba_comment", [
            dict(common, user_nickname="吧友", comment_id="tbc1", note_id="tb1", content="贴吧评论",
                 publish_time="2026-11-08 21:00:00"),
        ])
        # 知乎：created_time 字符串
        _insert(conn, "zhihu_content", [
            dict(common, user_nickname="知乎用户", content_id="zh1", title="示例科技 知乎问答",
                 desc="问答描述", content_text="知乎正文", created_time="2026-11-06 18:00:00",
                 content_url="https://zhihu/1", source_keyword="示例", voteup_count=7, comment_count=1),
        ])
        _insert(conn, "zhihu_comment", [
            dict(common, user_nickname="知乎用户", comment_id="zhc1", content_id="zh1",
                 content="知乎评论", publish_time="2026-11-06 19:00:00", like_count=1),
        ])
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 测试用 analyzer
# ---------------------------------------------------------------------------
class FakeAnalyzer:
    def __init__(self, fail: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail = fail

    def __call__(self, texts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.calls.append({"count": len(texts), "ids": [item["id"] for item in texts]})
        if self.fail:
            raise RuntimeError("模拟 8000 情感接口失败")
        results = []
        for item in texts:
            text = str(item.get("text") or "")
            if "负面" in text:
                sentiment = "negative"
            elif "正面" in text or "获奖" in text or "合作" in text:
                sentiment = "positive"
            else:
                sentiment = "neutral"
            results.append({"id": item["id"], "sentiment": sentiment,
                            "confidence": 0.9, "reason": f"stub:{sentiment}"})
        return results


def _sentiment_distribution(package: Dict[str, Any]) -> Dict[str, int]:
    stats = package["stats"]["sentiment"]["overall"]
    return {key: stats[key]["count"] for key in ("positive", "neutral", "negative")}


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stats_schema_errors(stats: Dict[str, Any]) -> List[str]:
    """用 POC 自带 stats.schema.json + MiniValidator 校验（与 8000 侧同一份 schema）。"""
    poc = load_poc_modules()
    if poc["poc_dir"] not in sys.path:
        sys.path.insert(0, poc["poc_dir"])
    import schema_validator  # noqa: PLC0415

    schema_path = os.path.join(poc["poc_dir"], "stats.schema.json")
    with open(schema_path, "r", encoding="utf-8") as fh:
        root_schema = json.load(fh)
    return schema_validator.MiniValidator(root_schema).validate(stats)


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------
def test_time_helpers() -> None:
    section("1) 时间/情感标签工具")
    check("秒级时间戳解析", parse_publish_time(_epoch_seconds("2026-11-05 10:00:00"))
          == datetime(2026, 11, 5, 10, 0, 0))
    check("毫秒时间戳解析", parse_publish_time(_epoch_ms("2026-11-05 10:00:00"))
          == datetime(2026, 11, 5, 10, 0, 0))
    check("字符串时间解析", parse_publish_time("2026-11-05 10:00:00") == datetime(2026, 11, 5, 10, 0, 0))
    check("日期字符串解析", parse_publish_time("2026-11-05") == datetime(2026, 11, 5))
    check("ISO8601 解析", parse_publish_time("2026-11-05T10:00:00+08:00") == datetime(2026, 11, 5, 10, 0, 0))
    check("非法时间返回 None", parse_publish_time("not-a-time") is None and parse_publish_time("") is None)
    start, end = parse_window("2026-11-03", "2026-11-09")
    check("窗口 start=00:00:00", start == datetime(2026, 11, 3, 0, 0, 0))
    check("窗口 end=23:59:59", end == datetime(2026, 11, 9, 23, 59, 59))
    try:
        parse_window("2026-11-09", "2026-11-03")
        check("窗口起止顺序校验", False)
    except ValueError:
        check("窗口起止顺序校验", True)
    check("情感标签中文/英文归一",
          normalize_sentiment_label("正面") == "positive"
          and normalize_sentiment_label("negative") == "negative"
          and normalize_sentiment_label("【中性】") == "neutral"
          and normalize_sentiment_label("unknown") is None)
    check("POC 流水线目录可定位", os.path.isfile(os.path.join(poc_pipeline_dir(), "compute_stats.py")),
          poc_pipeline_dir())


def test_dataset_loading(media_db: str) -> None:
    section("2) 窗口/关键词/去重/评论关联 → NormalizedDataset")
    dataset, quality = load_report_dataset(
        media_db, window_start=WINDOW_START, window_end=WINDOW_END, keywords=KEYWORDS,
    )
    content_ids = {(item["platform"], item["content_id"]) for item in dataset.contents}
    check("窗口内内容条数（xhs2+dy1+ks1+bili1+wb1+tieba1+zhihu1=8）",
          len(dataset.contents) == 8, str(len(dataset.contents)))
    check("内容平台覆盖 7 平台", {item["platform"] for item in dataset.contents} == set(PLATFORM_LABELS.values()),
          str(sorted({item["platform"] for item in dataset.contents})))
    check("窗口外内容被排除（x3）", ("小红书", "x3") not in content_ids)
    check("off_brand 内容被排除（x4）", ("小红书", "x4") not in content_ids)
    check("关键词不匹配内容被排除（x6）", ("小红书", "x6") not in content_ids)
    check("缺发布时间内容被排除（x5）", ("小红书", "x5") not in content_ids)
    check("重复 content_id 保留最新快照（x1 标题为新）",
          any(item["content_id"] == "x1" and "新品发布" in item["title"] and "旧快照" not in item["title"]
              for item in dataset.contents))
    check("去重计数记录", quality["duplicate_contents_removed"] == 1,
          str(quality["duplicate_contents_removed"]))
    check("缺时间计数记录", quality["time_parse_failures"] >= 1, str(quality["time_parse_failures"]))
    # 评论：c1/c3 跟随 x1；c2 父内容 x3 不在窗口 → 排除；wb/zhihu 等评论 4 条
    comment_ids = {(item["platform"], item["comment_id"]) for item in dataset.comments}
    check("评论仅跟随窗口内父内容（c1 在、c2 不在）",
          ("小红书", "c1") in comment_ids and ("小红书", "c2") not in comment_ids)
    check("评论时间缺失回退父内容时间（c3）", ("小红书", "c3") in comment_ids)
    check("评论总条数（xhs2+dy1+wb1+tieba1+zhihu1=6）", len(dataset.comments) == 6,
          str(len(dataset.comments)))
    check("评论父内容均在内容集合内",
          all((item["platform"], item["content_id"]) in content_ids for item in dataset.comments))
    check("时间字段统一格式化",
          all(len(item["published_at"]) == 19 and item["published_at"][4] == "-"
              for item in dataset.contents + dataset.comments))
    check("边界内容 2026-11-03 00:00:00 与 2026-11-09 23:59:59 均包含",
          {"x2", "wb1"}.issubset({item["content_id"] for item in dataset.contents}))
    check("数据质量窗口信息完整",
          quality["window_filter"]["mode"] == "publish_time"
          and quality["window_filter"]["start"] == WINDOW_START
          and quality["window_filter"]["end"] == WINDOW_END)


def test_package_and_sentiment(media_db: str, app_db: str) -> None:
    section("3) build_report_package + 情感分析 + 缓存")
    analyzer = FakeAnalyzer()
    package = build_report_package(
        media_db, WINDOW_START, WINDOW_END, KEYWORDS, DEFAULT_PLATFORMS, analyzer,
        app_db_path=app_db, report_date=WINDOW_END, sentiment_model="stub-8000",
    )
    # --- 包结构 ---
    check("schema_version = public-sentiment-data@v1", package["schema_version"] == "public-sentiment-data@v1")
    check("包必填字段齐全",
          all(key in package for key in
              ("package_id", "generated_at", "source", "report", "counts", "stats", "samples",
               "contents", "meta")))
    check("report.window 使用传入日期", package["report"]["window"]["start"] == WINDOW_START
          and package["report"]["window"]["end"] == WINDOW_END)
    check("report.keywords 保留", package["report"]["keywords"] == KEYWORDS)
    check("report.platforms = 7 平台 code", package["report"]["platforms"] == DEFAULT_PLATFORMS)
    counts = package["counts"]
    check("counts 与 stats.totals 一致",
          counts["total"] == package["stats"]["totals"]["items"],
          f"counts={counts['total']} stats={package['stats']['totals']['items']}")
    check("counts.content/comments 与 dataset 一致",
          counts["content"] == 8 and counts["comments"] == 6, json.dumps(counts, ensure_ascii=False))
    by_platform = {row["platform"]: row for row in counts["by_platform"]}
    check("by_platform 覆盖 7 平台", set(by_platform) == set(DEFAULT_PLATFORMS))
    check("贴吧进入 stats 平台排名（POC 已扩展）",
          any(row["platform"] == "贴吧" for row in package["stats"]["platform_ranking"]),
          str([row["platform"] for row in package["stats"]["platform_ranking"]]))
    check("stats_fingerprint 格式正确",
          package["meta"]["stats_fingerprint"].startswith("sha256:")
          and len(package["meta"]["stats_fingerprint"]) == 71)
    # fingerprint 可复算（用 POC 算法）
    poc = load_poc_modules()
    check("stats_fingerprint 与 POC 算法一致",
          package["meta"]["stats_fingerprint"] == "sha256:" + poc["stats_fingerprint"](package["stats"]))
    # 8000 真实联调回归：stats.stats_fingerprint 必须为纯 hex（不带 sha256: 前缀），source.excel_sha256 必须 64 位 hex
    stats = package["stats"]
    stats_fp = str(stats.get("stats_fingerprint") or "")
    check("stats.stats_fingerprint 为纯 hex（8000 校验要求）",
          bool(__import__("re").fullmatch(r"[a-f0-9]{64}", stats_fp)), stats_fp)
    check("stats.stats_fingerprint = POC 纯 hex 指纹",
          stats_fp == poc["stats_fingerprint"](stats))
    check("meta.stats_fingerprint = sha256: + stats 纯 hex",
          package["meta"]["stats_fingerprint"] == "sha256:" + stats_fp)
    stats_sha = str((stats.get("source") or {}).get("excel_sha256") or "")
    check("stats.source.excel_sha256 为 64 位 hex（8000 校验要求）",
          bool(__import__("re").fullmatch(r"[a-f0-9]{64}", stats_sha)), stats_sha)
    check("stats.source.excel_sha256 = DB 文件 sha256",
          stats_sha == _sha256_file(media_db))
    schema_errors = _stats_schema_errors(stats)
    check("stats 通过 POC stats.schema.json（与 8000 同 schema）",
          schema_errors == [], "; ".join(schema_errors[:5]))
    check("samples 含五类（新增负面评论样例）",
          set(package["samples"]) == {"negative", "negative_comments", "positive", "top_comments", "hot_keywords"})
    check("samples/contents 条数有界",
          len(package["samples"]["negative"]) <= 20 and len(package["samples"]["positive"]) <= 20
          and len(package["samples"]["top_comments"]) <= 20 and len(package["contents"]) <= 500)
    check("contents 情感为中文", all(item["sentiment"] in {"正面", "中性", "负面"} for item in package["contents"]))
    check("数据包可 JSON 序列化", isinstance(json.dumps(package, ensure_ascii=False), str))

    # --- 情感分析调用与缓存 ---
    sentiment_info = package["meta"]["sentiment"]
    check("analyzer 被调用（>=1 批）", len(analyzer.calls) >= 1, str(len(analyzer.calls)))
    check("每批 <= batch_size(50)", all(call["count"] <= 50 for call in analyzer.calls))
    check("情感分析覆盖全部条目", sentiment_info["analyzed_items"] + sentiment_info["fallback_neutral_items"] == 14,
          json.dumps(sentiment_info, ensure_ascii=False))
    dist = _sentiment_distribution(package)
    check("正面/中性/负面均有统计（stub 数据）", dist["positive"] >= 2 and dist["negative"] >= 1,
          str(dist))
    cache = load_sentiment_cache(app_db)
    check("缓存写入成功", len(cache) > 0, str(len(cache)))
    check("缓存 model 写入", all(row["model"] == "stub-8000" for row in cache.values()))
    check("缓存 sentiment 为规范化英文",
          all(row["sentiment"] in {"positive", "neutral", "negative"} for row in cache.values()))
    with sqlite3.connect(app_db) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (SENTIMENT_CACHE_TABLE,)
        ).fetchone()
        check("app.db 存在 sentiment_cache 表", table is not None)
    check("texts 去重（analyzer 收到唯一 id 数 = cache 条目数）",
          len({text_id for call in analyzer.calls for text_id in call["ids"]}) == len(cache),
          f"{len({text_id for call in analyzer.calls for text_id in call['ids']})} vs {len(cache)}")

    # --- 缓存命中：analyzer 不应再被调用 ---
    class ExplodingAnalyzer:
        def __call__(self, texts):  # noqa: ANN001
            raise AssertionError("缓存命中时不应调用 analyzer")

    package2 = build_report_package(
        media_db, WINDOW_START, WINDOW_END, KEYWORDS, DEFAULT_PLATFORMS, ExplodingAnalyzer(),
        app_db_path=app_db, report_date=WINDOW_END, sentiment_model="stub-8000",
    )
    check("缓存命中时不调用 analyzer", package2["meta"]["sentiment"]["cache_hits"] ==
          package2["meta"]["sentiment"]["analyzed_items"] == 14,
          json.dumps(package2["meta"]["sentiment"], ensure_ascii=False))
    check("缓存命中后 sentiment 分布一致", _sentiment_distribution(package2) == dist)
    check("缓存命中后 fingerprint 一致",
          package2["meta"]["stats_fingerprint"] == package["meta"]["stats_fingerprint"])

    # --- analyzer 失败 → neutral 兜底 + warning，不阻断 ---
    failing = FakeAnalyzer(fail=True)
    with tempfile.TemporaryDirectory(prefix="rd_fail_") as tmp_dir:
        fail_app_db = os.path.join(tmp_dir, "app.db")
        package3 = build_report_package(
            media_db, WINDOW_START, WINDOW_END, KEYWORDS, DEFAULT_PLATFORMS, failing,
            app_db_path=fail_app_db, report_date=WINDOW_END,
        )
        info3 = package3["meta"]["sentiment"]
        check("analyzer 失败仍返回数据包", package3["counts"]["total"] == 14)
        check("analyzer 失败按 neutral 兜底", info3["fallback_neutral_items"] == 14,
              json.dumps(info3, ensure_ascii=False))
        check("analyzer 失败记录 errors/warnings",
              bool(info3["errors"]) and any("情感分析" in str(w) for w in package3["stats"]["warnings"]))
        check("失败兜底不写缓存（新 app.db 无缓存）", load_sentiment_cache(fail_app_db) == {})

    # --- analyzer 未配置 → neutral 兜底 ---
    with tempfile.TemporaryDirectory(prefix="rd_none_") as tmp_dir:
        none_app_db = os.path.join(tmp_dir, "app.db")
        package4 = build_report_package(
            media_db, WINDOW_START, WINDOW_END, KEYWORDS, DEFAULT_PLATFORMS, None,
            app_db_path=none_app_db, report_date=WINDOW_END,
        )
        info4 = package4["meta"]["sentiment"]
        check("未配置 analyzer 按 neutral 兜底", info4["fallback_neutral_items"] == 14,
              json.dumps(info4, ensure_ascii=False))
        check("未配置 analyzer 记录 warning",
              any("未配置" in str(w) for w in package4["stats"]["warnings"]))

    # --- 平台子集过滤 ---
    subset = build_report_package(
        media_db, WINDOW_START, WINDOW_END, KEYWORDS, ["xhs", "tieba"], FakeAnalyzer(),
        app_db_path=app_db, report_date=WINDOW_END,
    )
    check("平台子集过滤生效",
          {row["platform"] for row in subset["counts"]["by_platform"]} == {"xhs", "tieba"}
          and subset["counts"]["total"] == 6,
          json.dumps(subset["counts"], ensure_ascii=False))


def test_empty_and_edge(media_db: str) -> None:
    section("4) 空窗口 / 非法窗口 / 无 analyzer 缓存函数")
    with tempfile.TemporaryDirectory(prefix="rd_empty_") as tmp_dir:
        empty_app_db = os.path.join(tmp_dir, "app.db")
        package = build_report_package(
            media_db, "2020-01-01", "2020-01-02", KEYWORDS, DEFAULT_PLATFORMS, FakeAnalyzer(),
            app_db_path=empty_app_db, report_date="2020-01-02",
        )
        check("空窗口返回合法包", package["schema_version"] == "public-sentiment-data@v1")
        check("空窗口 counts=0", package["counts"]["total"] == 0 and package["stats"]["totals"]["items"] == 0)
        check("空窗口 samples/contents 为空",
              package["samples"]["negative"] == [] and package["contents"] == [])
        empty_stats = package["stats"]
        check("空窗口 stats_fingerprint 仍存在（纯 hex）",
              bool(__import__("re").fullmatch(r"[a-f0-9]{64}",
                                              str(empty_stats.get("stats_fingerprint") or ""))))
        check("空窗口 stats.source.excel_sha256 仍为 64 位 hex",
              bool(__import__("re").fullmatch(r"[a-f0-9]{64}",
                                              str((empty_stats.get("source") or {}).get("excel_sha256") or ""))))
        empty_errors = _stats_schema_errors(empty_stats)
        check("空窗口 stats 通过 stats.schema.json", empty_errors == [],
              "; ".join(empty_errors[:5]))
    try:
        build_report_package(media_db, "2026-11-09", "2026-11-03", KEYWORDS, DEFAULT_PLATFORMS, None)
        check("非法窗口应抛 ValueError", False)
    except ValueError:
        check("非法窗口应抛 ValueError", True)

    section("5) 情感缓存工具函数")
    with tempfile.TemporaryDirectory(prefix="rd_cache_") as tmp_dir:
        app_db = os.path.join(tmp_dir, "app.db")
        check("空缓存读取返回 {}", load_sentiment_cache(app_db) == {})
        upsert_sentiment(app_db, platform="xhs", kind="content", item_id="x1",
                         text_hash="hash1", sentiment="正面", reason="测试", model="m1")
        cache = load_sentiment_cache(app_db)
        check("upsert_sentiment 写入并归一化中文",
              cache.get(("xhs", "content", "x1"), {}).get("sentiment") == "positive",
              str(cache.get(("xhs", "content", "x1"))))
        check("load_sentiment_cache 平台过滤",
              load_sentiment_cache(app_db, platforms=["dy"]) == {})
        extra = load_sentiment_cache(app_db, platforms=["xhs"])
        check("load_sentiment_cache 按平台命中", len(extra) == 1)


def test_poc_modules() -> None:
    section("6) POC 模块加载")
    poc = load_poc_modules()
    check("加载 NormalizedDataset", hasattr(poc["NormalizedDataset"], "__dataclass_fields__"))
    check("compute_stats 可调用", callable(poc["compute_stats"]))
    check("stats_fingerprint 可调用", callable(poc["stats_fingerprint"]))
    check("POC PLATFORM_ORDER 已扩展贴吧",
          "贴吧" in list(poc["compute_stats_module"].PLATFORM_ORDER),
          str(poc["compute_stats_module"].PLATFORM_ORDER))


# ---------------------------------------------------------------------------
# 7/8/9：P1-1 拆分后的门面契约 / 数据包 schema 校验 / 关键统计值不变
# ---------------------------------------------------------------------------
FACADE_NAMES = [
    "build_report_package", "load_report_dataset", "load_sentiment_cache",
    "upsert_sentiment", "upsert_sentiments", "normalize_sentiment_label",
    "DEFAULT_PLATFORMS", "PLATFORM_LABELS", "SENTIMENT_CN", "SCHEMA_VERSION",
    "APP_VERSION", "CONTENT_SPECS", "COMMENT_SPECS", "TZ", "parse_publish_time",
    "parse_window", "poc_pipeline_dir", "load_poc_modules",
]


def test_facade_surface() -> None:
    section("7) 拆分后 report_data 门面：对外名字齐全且指向子模块实现")
    import importlib

    report_data = importlib.import_module("report_data")
    report_dataset = importlib.import_module("report_dataset")
    report_package = importlib.import_module("report_package")
    report_sentiment = importlib.import_module("report_sentiment")

    missing = [name for name in FACADE_NAMES if not hasattr(report_data, name)]
    check("门面导出名齐全（inspect 断言）", missing == [], str(missing))
    declared = set(getattr(report_data, "__all__", []) or [])
    check("门面 __all__ 覆盖关键公共名", all(name in declared for name in FACADE_NAMES),
          str([name for name in FACADE_NAMES if name not in declared]))

    checks = [
        ("build_report_package", report_package.build_report_package),
        ("validate_report_package", report_package.validate_report_package),
        ("load_report_dataset", report_dataset.load_report_dataset),
        ("load_sentiment_cache", report_sentiment.load_sentiment_cache),
        ("upsert_sentiment", report_sentiment.upsert_sentiment),
        ("upsert_sentiments", report_sentiment.upsert_sentiments),
        ("normalize_sentiment_label", report_sentiment.normalize_sentiment_label),
    ]
    same = [name for name, impl in checks if getattr(report_data, name) is not impl]
    check("门面名字指向子模块同一实现（不是复制体）", same == [], str(same))
    check("report_data.py 只剩门面（不含实现函数体）",
          "def build_report_package" not in open(
              os.path.join(HERE, "report_data.py"), encoding="utf-8").read())
    for module, functions in (
        ("report_dataset.py", ("load_report_dataset",)),
        ("report_package.py", ("build_report_package", "validate_report_package")),
        ("report_sentiment.py", ("analyze_dataset_sentiment", "load_sentiment_cache")),
        ("report_common.py", ("load_poc_modules", "parse_publish_time")),
    ):
        path = os.path.join(HERE, module)
        body = open(path, encoding="utf-8").read()
        check(f"{module} 存在且实现目标函数", os.path.isfile(path)
              and all(f"def {name}(" in body for name in functions))
    check("数据包结构用 TypedDict 建模（ReportPackage 有 __required_keys__）",
          isinstance(getattr(report_package.ReportPackage, "__required_keys__", None), frozenset),
          str(type(getattr(report_package.ReportPackage, "__required_keys__", None))))
    required_keys = set(report_package.ReportPackage.__required_keys__)
    check("ReportPackage 必填键覆盖 schema_version/counts/stats/samples/meta",
          {"schema_version", "package_id", "generated_at", "source", "report", "counts",
           "stats", "samples", "contents", "meta"} <= required_keys, str(sorted(required_keys)))
    check("ReportPackage.instruction 为可选键",
          "instruction" in set(report_package.ReportPackage.__optional_keys__),
          str(report_package.ReportPackage.__optional_keys__))
    for block in ("CountsBlock", "StatsBlock", "SamplesBlock", "MetaBlock", "TotalsBlock"):
        check(f"TypedDict {block} 存在且有关键约束",
              bool(getattr(report_package, block).__annotations__), block)


def test_package_schema_guard(media_db: str) -> None:
    section("8) 数据包 schema 校验：缺字段/类型不对 → REPORT_PACKAGE_INVALID")
    import copy

    from report_package import REPORT_PACKAGE_INVALID, ReportPackageInvalid, validate_report_package

    with tempfile.TemporaryDirectory(prefix="rd_schema_") as tmp_dir:
        package = build_report_package(
            media_db, WINDOW_START, WINDOW_END, KEYWORDS, DEFAULT_PLATFORMS, None,
            app_db_path=os.path.join(tmp_dir, "app.db"), report_date=WINDOW_END,
        )
    check("build_report_package 返回值通过 schema 校验",
          validate_report_package(package) is package)

    def _drop(path: str):
        def mutate(pkg: Dict[str, Any]) -> None:
            parts = path.split(".")
            target = pkg
            for part in parts[:-1]:
                target = target[part]
            target.pop(parts[-1], None)
        return mutate

    def _set(path: str, value: Any):
        def mutate(pkg: Dict[str, Any]) -> None:
            parts = path.split(".")
            target = pkg
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
        return mutate

    cases = [
        ("缺 counts", _drop("counts")),
        ("缺 stats", _drop("stats")),
        ("缺 counts.total", _drop("counts.total")),
        ("缺 counts.by_platform", _drop("counts.by_platform")),
        ("缺 stats.totals", _drop("stats.totals")),
        ("缺 meta.filtering", _drop("meta.filtering")),
        ("缺 samples.hot_keywords", _drop("samples.hot_keywords")),
        ("counts.total 类型不对", _set("counts.total", "14")),
        ("stats.totals.items 类型不对", _set("stats.totals.items", "14")),
        ("meta.stats_fingerprint 缺前缀", _set("meta.stats_fingerprint", "deadbeef")),
        ("report.window 缺 label", _drop("report.window.label")),
        ("schema_version 不匹配", _set("schema_version", "public-sentiment-data@v2")),
    ]
    for name, mutate in cases:
        broken = copy.deepcopy(package)
        mutate(broken)
        try:
            validate_report_package(broken)
            check(f"schema 校验拒绝：{name}", False, "未抛错")
        except ReportPackageInvalid as exc:
            check(f"schema 校验拒绝：{name}", exc.code == REPORT_PACKAGE_INVALID,
                  f"{exc.code}: {exc}")

    try:
        validate_report_package([])
        check("schema 校验拒绝非 dict 数据包", False, "未抛错")
    except ReportPackageInvalid as exc:
        check("schema 校验拒绝非 dict 数据包", exc.code == REPORT_PACKAGE_INVALID, str(exc))
    check("错误码常量为 REPORT_PACKAGE_INVALID", REPORT_PACKAGE_INVALID == "REPORT_PACKAGE_INVALID")
    check("异常问题列表可读（含字段路径）",
          any("counts" in str(item) for item in ReportPackageInvalid(["counts：缺失"]).problems))


def test_split_equivalence(media_db: str) -> None:
    section("9) 拆分后关键统计值不变（counts / totals / trend / samples）")
    with tempfile.TemporaryDirectory(prefix="rd_split_") as tmp_dir:
        package = build_report_package(
            media_db, WINDOW_START, WINDOW_END, KEYWORDS, DEFAULT_PLATFORMS, None,
            app_db_path=os.path.join(tmp_dir, "app.db"), report_date=WINDOW_END,
        )
    counts = package["counts"]
    check("counts 关键值（8 内容 / 6 评论 / 14 合计）",
          (counts["content"], counts["comments"], counts["total"]) == (8, 6, 14),
          json.dumps(counts, ensure_ascii=False)[:160])
    expected_by_platform = [
        {"platform": "xhs", "content": 2, "comments": 2},
        {"platform": "dy", "content": 1, "comments": 1},
        {"platform": "ks", "content": 1, "comments": 0},
        {"platform": "bili", "content": 1, "comments": 0},
        {"platform": "wb", "content": 1, "comments": 1},
        {"platform": "tieba", "content": 1, "comments": 1},
        {"platform": "zhihu", "content": 1, "comments": 1},
    ]
    check("counts.by_platform 与拆分前一致", counts["by_platform"] == expected_by_platform,
          json.dumps(counts["by_platform"], ensure_ascii=False))
    totals = package["stats"]["totals"]
    check("stats.totals 关键值（14/8/6，7 平台）",
          (totals["items"], totals["content"], totals["comments"], totals["platforms"]) == (14, 8, 6, 7),
          json.dumps(totals, ensure_ascii=False)[:160])
    trend = package["stats"]["trend"]
    check("stats.trend 天数 = 7（窗口 11-03 ~ 11-09）", len(trend["days"]) == 7, str(len(trend["days"])))
    check("stats.report.period 覆写为报告窗口",
          package["stats"]["report"]["period"] == {"start": WINDOW_START, "end": WINDOW_END,
                                                   "label": "2026-11-03 ~ 2026-11-09"},
          json.dumps(package["stats"]["report"]["period"], ensure_ascii=False))
    check("平台排名覆盖 7 平台", len(package["stats"]["platform_ranking"]) == 7,
          str(len(package["stats"]["platform_ranking"])))
    check("无 analyzer 时情感全部 neutral 兜底",
          package["stats"]["sentiment"]["overall"]["neutral"]["count"] == 14
          and package["meta"]["sentiment"]["fallback_neutral_items"] == 14,
          json.dumps(package["stats"]["sentiment"]["overall"], ensure_ascii=False))
    check("samples 五类齐全且条数有界",
          set(package["samples"]) == {"negative", "negative_comments", "positive",
                                      "top_comments", "hot_keywords"}
          and len(package["samples"]["top_comments"]) <= 20)
    check("contents 摘要 = 8 条（未截断）",
          len(package["contents"]) == 8
          and package["meta"]["filtering"]["truncated"]["contents"] == 0,
          f"{len(package['contents'])}")
    check("数据包仍可 JSON 序列化", isinstance(json.dumps(package, ensure_ascii=False), str))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 80)
    print("report_data.py 离线自测（DB→POC stats→数据包 + 情感缓存，不联网）")
    print("=" * 80)
    tmp_dir = tempfile.mkdtemp(prefix="report_data_test_")
    try:
        media_db = os.path.join(tmp_dir, "media_crawler.db")
        build_fixture_media_db(media_db)
        app_db = os.path.join(tmp_dir, "app.db")
        print(f"fixture media db: {media_db}")
        print(f"temp app db: {app_db}")
        test_time_helpers()
        test_dataset_loading(media_db)
        test_package_and_sentiment(media_db, app_db)
        test_empty_and_edge(media_db)
        test_poc_modules()
        test_facade_surface()
        test_package_schema_guard(media_db)
        test_split_equivalence(media_db)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("\n" + "=" * 80)
    if _failures:
        print(f"测试失败：{_failures} 项（通过 {_passes} 项）")
        return 1
    print(f"全部通过：{_passes} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

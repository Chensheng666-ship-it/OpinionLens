# -*- coding: utf-8 -*-
"""
compute_stats.py — 确定性统计计算（本流水线唯一的“数字来源”）

输入：excel_loader.load_excel() 输出的规范化明细（或直接传 Excel 路径）
输出：stats JSON（结构见同目录 stats.schema.json）

硬性约定：
  * 本模块内所有数字都由代码计算，不调用任何模型 / 不读取任何人工填写的数字；
  * 模型只能消费 stats JSON 写“叙述”，不能修改、补充或推断任何统计值；
  * 所有口径（声量定义、热度分公式、情感阈值、趋势窗口）在 docs 中逐条说明。

统计内容：
  1) totals        总量 / 内容数 / 评论数 / 互动合计 / 平台数
  2) sentiment     总体、内容、评论、分平台情感分布（positive/neutral/negative）
  3) risk          风险等级（规则：负面占比阈值 + 重点负面事件数）
  4) platform_ranking  重点平台声量排名（内容+评论）
  5) trend         趋势窗口内「按天 × 平台」声量（内容+评论）
  6) negative_events / positive_events  Top 事件（含摘要、链接、互动量、热度分）
  7) keywords      热词 TopN（jieba 分词 + 停用词，确定性）
  8) comments      评论量统计、评论情感、高赞评论
  9) data_quality  数据质量与样本相关性
 10) samples       给模型做叙述的代表样本（有界，限制 token）
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import logging
import math
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from excel_loader import (
    PIPELINE_VERSION,
    PLATFORM_ORDER,
    SCHEMA_VERSION,
    NormalizedDataset,
    dataset_fingerprint,
    load_excel,
)

# ---------------------------------------------------------------------------
# 口径常量（全部在 docs 中解释）
# ---------------------------------------------------------------------------
BRAND_TERMS = ["示例科技", "示例品牌", "示例网络科技", "示例", "示例近似科技"]  # 末项为媒体常见同音错字别名
# 品牌关键人物（用于判断“内容是否与品牌有关联”，而不是只按关键词是否出现）
KEY_PEOPLE_TERMS = ["示例人物"]
# 品牌词出现在正文前 N 个字符内，才认为这条内容“与品牌强相关”
BRAND_CONTEXT_WINDOW = 400

# 热度分：互动量加权 + 时间衰减
ENGAGEMENT_WEIGHTS = {
    "likes": 1.0,
    "comments": 2.0,
    "shares": 3.0,
    "collects": 1.0,
    "coins": 1.0,
    "plays": 0.002,   # 播放量仅作弱信号，避免 B站/快手 播放量压过真实互动
}
HEAT_INTERACTION_WEIGHT = 0.8
HEAT_RECENCY_WEIGHT = 0.2
HEAT_RECENCY_HALFLIFE_DAYS = 30.0

# 风险等级阈值（按负面占比）
RISK_THRESHOLDS = {"low": 0.08, "medium": 0.15}  # <8% 低；8%~15% 中；>15% 高

# 负面事件处理状态规则（POC：无工单系统数据时按数据规则占位）
HANDLING_ARCHIVE_DAYS = 180
HANDLING_WATCH_DAYS = 14

# 热词停用词（中文常见虚词 / 平台词 / 泛化词 / 品牌词另列）
STOPWORDS = set(
    """
    的 了 是 在 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有 看 好
    自己 这 那 他 她 它 们 我们 你们 他们 这个 那个 这些 那些 这样 那样 什么 怎么
    如何 为什么 可以 需要 应该 能够 已经 还是 就是 因为 所以 但是 如果 虽然 而且
    然后 以及 或者 并且 不过 只是 其实 现在 今天 昨天 明天 今年 去年 明年 目前
    之后 之前 时候 时间 问题 情况 方面 进行 通过 相关 内容 视频 文章 发布 网友
    表示 认为 觉得 感觉 知道 看看 看到 发现 出现 成为 作为 由于 对于 关于 根据
    大家 很多 一些 一点 非常 特别 真的 直接 比较 可能 主要 重要 不同 各种 所有
    公司 企业 平台 产品 服务 用户 人员 工作 行业 市场 发展 项目 业务 系统 技术
    中国 全国 国家 政府 社会 经济 城市 地方 单位 部门 组织 集团 有限 有限公司
    第一 第二 第三 左右 超过 以上 以下 以及 而且 还有 不是 不会 不能 可能 需要
    哈哈哈 哈哈 真的服 无语 谢谢 感谢 支持 爱了 收藏 点赞 关注 转发 评论
    的 地 得 着 过 把 被 让 给 从 向 往 跟 同 与 及 或 而 则 就 还 又 再 才 只
    啊 呀 吧 呢 吗 哦 嗯 哈 呗 啦 嘛 哇 诶 咦 哎 唉 咳 呵
    推荐 优秀 国际 主要 超过 发现 机会 提升 核心 需求 运营 研发 年度 目前
    其中 进入 成为 打造 合作 举行 开展 表示 介绍 提供 建设 推进 实现 完成
    增长 排名 名单 公布 荣获 获得 记者 报道 新闻 来源 责任编辑 编辑 视频 直播
    亿元 万元 千万 百万 数据 统计 显示 同比 环比 增长 下降 上涨 最新 相关
    品牌 荣誉 奖项 称号 表彰 活动 大会 会议 企业 工作 生产 经营 管理 改革
    """.split()
)
# 品牌主体词在“关联热词”中剔除（单独统计）
BRAND_STOPWORDS = {"示例", "科技", "示例科技", "有限公司", "公司", "股份", "集团"}

KEYWORDS_TOP_N = 20
RELATED_KEYWORDS_TOP_N = 15
EVENTS_TOP_N = 5
SAMPLES_TOP_N = 5
COMMENTS_TOP_N = 5

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def _ratio(part: int, total: int, digits: int = 4) -> float:
    if not total:
        return 0.0
    return round(part / total, digits)


def _is_sentiment_scored(item: Dict[str, Any]) -> bool:
    return item.get("sentiment_scored") is not False


def _sentiment_block(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    # 模型未打分的条目只记数，不进分母：否则坏回复/打分失败会按中性稀释负面占比。
    scored = [item for item in items if _is_sentiment_scored(item)]
    counter = collections.Counter(item["sentiment"] for item in scored)
    total = len(scored)
    return {
        "positive": {"count": counter.get("positive", 0), "ratio": _ratio(counter.get("positive", 0), total)},
        "neutral": {"count": counter.get("neutral", 0), "ratio": _ratio(counter.get("neutral", 0), total)},
        "negative": {"count": counter.get("negative", 0), "ratio": _ratio(counter.get("negative", 0), total)},
        "total": total,
        "unscored": len(items) - total,
    }


def _dominant_sentiment(items: List[Dict[str, Any]]) -> str:
    counter = collections.Counter(item["sentiment"] for item in items if _is_sentiment_scored(item))
    if not counter:
        return "中性为主"
    label = max(counter.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return {"positive": "正面为主", "negative": "负面为主", "neutral": "中性为主"}[label]


def engagement_of(interactions: Dict[str, int]) -> float:
    """互动量加权和（热度分的互动分项）。公式见 docs。"""
    return float(sum(v * ENGAGEMENT_WEIGHTS.get(k, 0.0) for k, v in (interactions or {}).items()))


def match_brand_terms(text: str) -> List[str]:
    hits = [term for term in BRAND_TERMS if term and term in (text or "")]
    return hits


def brand_context_of(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    判断内容与品牌的“关联强度”：
      - title_hit:      标题直接出现品牌词 / 关键人物；
      - first_pos:      正文首次出现品牌词的字符位置（-1 表示未出现）；
      - key_people_hit: 正文出现关键人物（如董事长姓名）；
      - context:        标题命中 / 关键人物命中 / 品牌词出现在正文前 400 字内。
    用于让“重点正面舆情”优先选择品牌相关度高的内容，避免把“百人名单里顺带提到”的内容
    当成品牌正面事件（样例数据中确实存在该类噪声）。
    """
    title = item.get("title") or ""
    text = item.get("text") or ""
    full = f"{title}\n{text}"
    all_terms = BRAND_TERMS + KEY_PEOPLE_TERMS
    title_hit = any(term and term in title for term in all_terms)
    key_people_hits = [term for term in KEY_PEOPLE_TERMS if term and term in full]
    positions = [full.find(term) for term in all_terms if term and term in full]
    first_pos = min(positions) if positions else -1
    context = bool(title_hit or (0 <= first_pos <= BRAND_CONTEXT_WINDOW))
    return {
        "title_hit": title_hit,
        "first_pos": first_pos,
        "key_people_hit": key_people_hits,
        "context": context,
    }


def detect_entity_risk(text: str) -> Optional[str]:
    """识别同名/近似主体风险（例如“天津示例网络科技”与“某某科技”并非同一法人）。"""
    text = text or ""
    if "示例网络" in text or ("天津" in text and "示例" in text):
        return "疑似同名主体（非某某科技），需人工甄别"
    return None


def _extract_keywords(contents: List[Dict[str, Any]], collected_at: datetime) -> Dict[str, Any]:
    """jieba 分词 + 停用词过滤 + 文档频率统计。"""
    try:
        import jieba
    except ImportError:  # 无 jieba 时退化为 2~4 字 n-gram 统计，保证流水线可运行
        jieba = None
        logging.warning("未安装 jieba，热词退化为 n-gram 统计")

    term_docs: Dict[str, int] = collections.Counter()
    term_freq: Dict[str, int] = collections.Counter()
    term_platforms: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for item in contents:
        text = f"{item.get('title', '')}\n{item.get('text', '')}"
        text = re.sub(r"#([^#]{1,30})#", r" \1 ", text)
        text = re.sub(r"\[[^\]]{1,20}\]", " ", text)      # 表情占位 [笑哭R]
        text = re.sub(r"https?://\S+", " ", text)
        tokens: List[str] = []
        if jieba is not None:
            for token in jieba.cut(text):
                token = token.strip()
                if 2 <= len(token) <= 8 and not re.fullmatch(r"[\W_]+", token) and not re.fullmatch(r"[\d.]+", token):
                    tokens.append(token)
        else:
            for n in (2, 3, 4):
                for idx in range(0, max(0, len(text) - n + 1)):
                    token = text[idx:idx + n]
                    if re.fullmatch(r"[\u4e00-\u9fa5A-Za-z]+", token):
                        tokens.append(token)
        seen = set()
        for token in tokens:
            if token in STOPWORDS:
                continue
            term_freq[token] += 1
            term_platforms[token][item["platform"]] += 1
            if token not in seen:
                term_docs[token] += 1
                seen.add(token)

    total_docs = max(len(contents), 1)

    def pack(terms: List[str]) -> List[Dict[str, Any]]:
        ordered = sorted(terms, key=lambda t: (-term_docs[t], -term_freq[t], t))
        result = []
        for term in ordered:
            platforms = [p for p, _ in term_platforms[term].most_common()]
            result.append(
                {
                    "keyword": term,
                    "doc_count": int(term_docs[term]),
                    "ratio_of_docs": _ratio(term_docs[term], total_docs),
                    "frequency": int(term_freq[term]),
                    "platforms": platforms,
                }
            )
        return result

    all_terms = [t for t in term_docs if term_docs[t] >= 2]
    top = pack(all_terms)[:KEYWORDS_TOP_N]
    related_pool = [t for t in all_terms if t not in BRAND_STOPWORDS and t not in BRAND_TERMS]
    related = pack(related_pool)[:RELATED_KEYWORDS_TOP_N]
    return {
        "method": "jieba 中文分词 + 停用词过滤 + 文档频率排序（确定性）" if jieba else "n-gram 统计（未安装 jieba 时降级）",
        "documents": total_docs,
        "brand_terms": [t for t in BRAND_TERMS],
        "top": top,
        "related": related,
    }


def _heat_scores(contents: List[Dict[str, Any]], collected_at: datetime) -> None:
    """为每条内容写入 engagement / heat_score / heat_rank（原地修改）。"""
    for item in contents:
        item["engagement"] = round(engagement_of(item.get("interactions")), 3)
        published = _parse_dt(item["published_at"])
        item["days_old"] = max(0, (collected_at.date() - published.date()).days)
    max_engagement = max([item["engagement"] for item in contents] + [1.0])
    log_max = math.log1p(max_engagement)
    for item in contents:
        interaction_score = 100.0 * math.log1p(item["engagement"]) / log_max if log_max else 0.0
        recency_score = 100.0 * math.exp(-item["days_old"] / HEAT_RECENCY_HALFLIFE_DAYS)
        item["heat_score"] = round(
            HEAT_INTERACTION_WEIGHT * interaction_score + HEAT_RECENCY_WEIGHT * recency_score, 1
        )
    ordered = sorted(contents, key=lambda x: (-x["heat_score"], x["platform"], x["content_id"]))
    for rank, item in enumerate(ordered, start=1):
        item["heat_rank"] = rank


def _event_payload(item: Dict[str, Any], rank: int, comments_by_content: Dict[str, List[Dict[str, Any]]],
                   collected_at: datetime) -> Dict[str, Any]:
    """把一条内容转成报告用的“事件”结构（含链接、互动、热度、处理状态占位）。"""
    linked_comments = comments_by_content.get(item["content_id"], [])
    recent_negative_comments = [
        c for c in linked_comments
        if c["sentiment"] == "negative"
        and (collected_at - _parse_dt(c["published_at"])).days <= HANDLING_WATCH_DAYS
    ]
    if item["days_old"] > HANDLING_ARCHIVE_DAYS:
        handling_status, handling_code = "历史存量·已沉淀", "archived"
        handling_basis = f"发布时间距采集日 {item['days_old']} 天，超过 {HANDLING_ARCHIVE_DAYS} 天存量阈值"
    elif recent_negative_comments:
        handling_status, handling_code = "持续观察", "watch"
        handling_basis = f"近 {HANDLING_WATCH_DAYS} 天关联负面评论 {len(recent_negative_comments)} 条"
    else:
        handling_status, handling_code = "待评估", "pending"
        handling_basis = "无近 14 天新增负面评论，热度可控"
    entity_risk = detect_entity_risk(item.get("text", "") or item.get("title", ""))
    brand_ctx = brand_context_of(item)
    brand_hits = match_brand_terms((item.get("text", "") or "") + (item.get("title", "") or ""))
    return {
        "event_id": f"{'NEG' if item['sentiment'] == 'negative' else 'POS'}-{rank:02d}",
        "rank": rank,
        "platform": item["platform"],
        "title": item.get("title") or item.get("summary", "")[:30],
        "summary": item.get("summary", ""),
        "url": item.get("url", ""),
        "author": item.get("author", ""),
        "published_at": item["published_at"],
        "published_date": item["published_date"],
        "sentiment_raw": item.get("sentiment_raw", ""),
        "interactions": item.get("interactions", {}),
        "engagement": item.get("engagement", 0.0),
        "heat_score": item.get("heat_score", 0.0),
        "heat_rank": item.get("heat_rank"),
        "days_old": item.get("days_old", 0),
        "brand_match": bool(brand_hits),
        "brand_terms": brand_hits,
        "brand_context": brand_ctx["context"],
        "brand_title_hit": brand_ctx["title_hit"],
        "brand_first_pos": brand_ctx["first_pos"],
        "key_people_hit": brand_ctx["key_people_hit"],
        "entity_risk": entity_risk,
        "linked_comments": {
            "total": len(linked_comments),
            "negative": sum(1 for c in linked_comments if c["sentiment"] == "negative"),
            "recent_negative": len(recent_negative_comments),
        },
        "handling_status": handling_status,
        "handling_code": handling_code,
        "handling_basis": handling_basis,
    }


# ---------------------------------------------------------------------------
# 主计算
# ---------------------------------------------------------------------------
def compute_stats(
    dataset: NormalizedDataset,
    *,
    report_date: str,
    brand: str = "示例科技",
    title: str = "示例科技舆情监测周报",
    mode: str = "snapshot",
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    trend_end: Optional[str] = None,
    trend_days: int = 14,
    version: int = 1,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """计算 stats JSON。mode='snapshot' 全量快照；mode='period' 按发布时间过滤。"""
    if mode not in ("snapshot", "period"):
        raise ValueError("mode 仅支持 snapshot / period")
    if trend_days < 1 or trend_days > 90:
        raise ValueError("trend_days 取值范围 1~90")

    contents = list(dataset.contents)
    comments = list(dataset.comments)

    parse_dt = _parse_dt
    all_times = [parse_dt(x["published_at"]) for x in contents + comments if x.get("published_at")]
    if not all_times:
        raise ValueError("数据中没有可用的发布时间，无法生成统计")
    collected_at = max(all_times)
    data_span_start = min(all_times)

    # ---- 周期过滤（period 模式）----
    if mode == "period":
        if not (period_start and period_end):
            raise ValueError("period 模式必须提供 period_start / period_end")
        start_dt = datetime.strptime(period_start, "%Y-%m-%d")
        end_dt = datetime.strptime(period_end, "%Y-%m-%d") + timedelta(days=1)
        contents = [x for x in contents if start_dt <= parse_dt(x["published_at"]) < end_dt]
        comments = [x for x in comments if start_dt <= parse_dt(x["published_at"]) < end_dt]

    # ---- 趋势窗口 ----
    if trend_end:
        trend_end_dt = datetime.strptime(trend_end, "%Y-%m-%d")
    elif mode == "period":
        trend_end_dt = datetime.strptime(period_end, "%Y-%m-%d")
    else:
        trend_end_dt = collected_at
    trend_start_dt = trend_end_dt - timedelta(days=trend_days - 1)

    # ---- 热度分 ----
    _heat_scores(contents, collected_at)
    comments_by_content: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for comment in comments:
        if comment.get("content_id"):
            comments_by_content[comment["content_id"]].append(comment)

    # ---- 1) totals ----
    by_platform_totals = []
    for platform in PLATFORM_ORDER:
        c_count = sum(1 for x in contents if x["platform"] == platform)
        m_count = sum(1 for x in comments if x["platform"] == platform)
        by_platform_totals.append(
            {"platform": platform, "content": c_count, "comments": m_count, "items": c_count + m_count}
        )
    interaction_totals: Dict[str, int] = collections.Counter()
    for item in contents:
        for key, value in (item.get("interactions") or {}).items():
            interaction_totals[key] += int(value)
    total_items = len(contents) + len(comments)
    totals = {
        "items": total_items,
        "content": len(contents),
        "comments": len(comments),
        "platforms": sum(1 for row in by_platform_totals if row["items"] > 0),
        "content_platforms": sum(1 for row in by_platform_totals if row["content"] > 0),
        "by_platform": by_platform_totals,
        "content_interactions": {
            key: int(interaction_totals.get(key, 0))
            for key in ("likes", "comments", "shares", "collects", "coins", "plays")
        },
        "content_engagement": round(sum(engagement_of(x.get("interactions") or {}) for x in contents), 1),
    }

    # ---- 2) sentiment ----
    sentiment = {
        "overall": _sentiment_block(contents + comments),
        "content": _sentiment_block(contents),
        "comments": _sentiment_block(comments),
        "by_platform": [],
    }
    for platform in PLATFORM_ORDER:
        p_contents = [x for x in contents if x["platform"] == platform]
        p_comments = [x for x in comments if x["platform"] == platform]
        if not p_contents and not p_comments:
            continue
        sentiment["by_platform"].append(
            {
                "platform": platform,
                "overall": _sentiment_block(p_contents + p_comments),
                "content": _sentiment_block(p_contents),
                "comments": _sentiment_block(p_comments),
            }
        )

    overall_negative_ratio = sentiment["overall"]["negative"]["ratio"]
    if overall_negative_ratio < RISK_THRESHOLDS["low"]:
        risk_level, risk_code = "低", "low"
    elif overall_negative_ratio < RISK_THRESHOLDS["medium"]:
        risk_level, risk_code = "中", "medium"
    else:
        risk_level, risk_code = "高", "high"
    content_negative_ratio = sentiment["content"]["negative"]["ratio"]
    negative_events_count = sum(1 for x in contents if x["sentiment"] == "negative")
    comment_negative = sentiment["comments"]["negative"]
    comment_negative_count = int(comment_negative["count"])
    comment_negative_ratio = float(comment_negative["ratio"])
    negative_comments = [x for x in comments if x["sentiment"] == "negative"]

    # 风险等级按「全量样本（内容 + 评论）」负面占比判定；但第四章「重点负面舆情事件」只列负面内容帖。
    # 两者口径不同时必须在 basis / intro / note 里说明，否则会出现「风险等级高但未识别到负面事件」的自相矛盾。
    if risk_code == "low":
        risk_driver = "none"
    elif content_negative_ratio >= RISK_THRESHOLDS["low"] and comment_negative_ratio >= RISK_THRESHOLDS["low"]:
        risk_driver = "both"
    elif content_negative_ratio >= RISK_THRESHOLDS["low"]:
        risk_driver = "content"
    elif comment_negative_ratio >= RISK_THRESHOLDS["low"]:
        risk_driver = "comments"
    else:
        risk_driver = "mixed"
    risk = {
        "level": risk_level,
        "level_code": risk_code,
        "negative_ratio": overall_negative_ratio,
        "content_negative_ratio": content_negative_ratio,
        "comment_negative_ratio": comment_negative_ratio,
        "negative_events": negative_events_count,
        "negative_comments": comment_negative_count,
        "risk_driver": risk_driver,
        "thresholds": RISK_THRESHOLDS,
        "basis": (
            f"负面占比 {overall_negative_ratio:.2%}（其中内容帖 {content_negative_ratio:.2%}、评论 {comment_negative_ratio:.2%}；"
            f"阈值：<{RISK_THRESHOLDS['low']:.0%} 为低、"
            f"{RISK_THRESHOLDS['low']:.0%}~{RISK_THRESHOLDS['medium']:.0%} 为中、>{RISK_THRESHOLDS['medium']:.0%} 为高）；"
            f"负面内容 {negative_events_count} 起、负面评论 {comment_negative_count} 条"
            + (
                f"；{sentiment['overall']['unscored']} 条未获得模型情感结果，不计入占比"
                if sentiment["overall"]["unscored"]
                else ""
            )
        ),
    }

    # ---- 3) platform_ranking ----
    platform_ranking = []
    for platform in PLATFORM_ORDER:
        p_contents = [x for x in contents if x["platform"] == platform]
        p_comments = [x for x in comments if x["platform"] == platform]
        mentions = len(p_contents) + len(p_comments)
        if mentions == 0:
            continue
        platform_ranking.append(
            {
                "platform": platform,
                "mentions": mentions,
                "content": len(p_contents),
                "comments": len(p_comments),
                "share": _ratio(mentions, total_items),
                "content_share": _ratio(len(p_contents), len(contents)),
                "content_dominant_sentiment": _dominant_sentiment(p_contents),
                "negative_ratio": _sentiment_block(p_contents + p_comments)["negative"]["ratio"],
            }
        )
    platform_ranking.sort(key=lambda x: (-x["mentions"], x["platform"]))
    for rank, row in enumerate(platform_ranking, start=1):
        row["rank"] = rank

    # ---- 4) trend（按天 × 平台）----
    days: List[Dict[str, Any]] = []
    window_items = 0
    window_by_platform: Dict[str, int] = collections.Counter()
    window_content = 0
    window_comments = 0
    cursor = trend_start_dt
    while cursor <= trend_end_dt:
        day_key = cursor.strftime("%Y-%m-%d")
        item_day = [x for x in contents + comments if x.get("published_date") == day_key]
        by_platform = {platform: 0 for platform in PLATFORM_ORDER}
        for item in item_day:
            by_platform[item["platform"]] += 1
            window_by_platform[item["platform"]] += 1
        window_items += len(item_day)
        window_content += sum(1 for x in item_day if x["kind"] == "content")
        window_comments += sum(1 for x in item_day if x["kind"] == "comment")
        days.append(
            {
                "date": day_key,
                "label": f"{cursor.month:02d}-{cursor.day:02d} {_WEEKDAY_CN[cursor.weekday()]}",
                "weekday": _WEEKDAY_CN[cursor.weekday()],
                "total": len(item_day),
                "content": sum(1 for x in item_day if x["kind"] == "content"),
                "comments": sum(1 for x in item_day if x["kind"] == "comment"),
                "by_platform": by_platform,
            }
        )
        cursor += timedelta(days=1)
    peak = max(days, key=lambda d: (d["total"], d["date"])) if days else None
    trend = {
        "granularity": "day",
        "window": {
            "start": trend_start_dt.strftime("%Y-%m-%d"),
            "end": trend_end_dt.strftime("%Y-%m-%d"),
            "days": len(days),
        },
        "platforms": [p for p in PLATFORM_ORDER if window_by_platform.get(p, 0) > 0],
        "days": days,
        "window_totals": {
            "items": window_items,
            "content": window_content,
            "comments": window_comments,
            "by_platform": {p: int(window_by_platform.get(p, 0)) for p in PLATFORM_ORDER},
            "daily_average": round(window_items / max(len(days), 1), 2),
        },
        "peak": peak,
        "note": (
            "趋势图展示报告采集截止日前 "
            f"{trend_days} 个自然日按发布时间的声量（内容+评论），"
            "用于观察近期传播节奏；全量样本统计见 totals。"
        ),
    }

    # ---- 5) Top 事件 ----
    negative_items = sorted(
        [x for x in contents if x["sentiment"] == "negative"],
        key=lambda x: (-x["heat_score"], -x["engagement"], x["platform"]),
    )[:EVENTS_TOP_N]
    positive_pool = [x for x in contents if x["sentiment"] == "positive"]
    positive_relevant = [x for x in positive_pool if brand_context_of(x)["context"]]
    positive_ranked = sorted(
        positive_relevant, key=lambda x: (-x["heat_score"], -x["engagement"], x["platform"])
    )
    if len(positive_ranked) < EVENTS_TOP_N:
        # 兜底：品牌相关度高的内容不足 5 条时，用其余正面内容按热度补齐
        rest = sorted(
            [x for x in positive_pool if x not in positive_ranked],
            key=lambda x: (-x["heat_score"], -x["engagement"], x["platform"]),
        )
        positive_ranked = positive_ranked + rest
    positive_items = positive_ranked[:EVENTS_TOP_N]
    negative_events = [
        _event_payload(item, idx + 1, comments_by_content, collected_at)
        for idx, item in enumerate(negative_items)
    ]
    positive_events = [
        _event_payload(item, idx + 1, comments_by_content, collected_at)
        for idx, item in enumerate(positive_items)
    ]

    # ---- 6) 热词 ----
    keywords = _extract_keywords(contents, collected_at)

    # ---- 7) 评论量统计 ----
    comment_by_platform = collections.Counter(x["platform"] for x in comments)
    likes_list = [x["likes"] for x in comments if x.get("likes")]
    top_liked_comments = sorted(comments, key=lambda x: (-(x.get("likes") or 0), x["platform"]))[:COMMENTS_TOP_N]
    comments_stats = {
        "total": len(comments),
        "by_platform": {p: int(comment_by_platform.get(p, 0)) for p in PLATFORM_ORDER if comment_by_platform.get(p, 0)},
        "sentiment": _sentiment_block(comments),
        "avg_likes": round(sum(likes_list) / len(likes_list), 2) if likes_list else 0.0,
        "max_likes": max(likes_list) if likes_list else 0,
        "comment_to_content_ratio": round(len(comments) / max(len(contents), 1), 2),
        "top_liked": [
            {
                "platform": item["platform"],
                "likes": item.get("likes", 0),
                "sentiment": item["sentiment"],
                "summary": item.get("summary", ""),
                "published_date": item.get("published_date", ""),
            }
            for item in top_liked_comments
        ],
    }

    # ---- 8) 数据质量 / 相关性 ----
    text_of = lambda x: (x.get("text", "") or "") + (x.get("title", "") or "")
    brand_matched = [x for x in contents if match_brand_terms(text_of(x))]
    unmatched = [x for x in contents if not match_brand_terms(text_of(x))]
    prominent = [x for x in contents if brand_context_of(x)["context"]]
    in_window_keys = set()
    for item in contents + comments:
        if trend_start_dt <= parse_dt(item["published_at"]) < trend_end_dt + timedelta(days=1):
            unique_id = item.get("content_id") if item["kind"] == "content" else item.get("comment_id")
            in_window_keys.add((item["kind"], item["platform"], unique_id))
    history_items = total_items - window_items
    data_quality = dict(dataset.data_quality)
    data_quality.update(
        {
            "content_fingerprint": dataset_fingerprint(dataset),
            "data_span": {
                "start": data_span_start.strftime("%Y-%m-%d"),
                "end": collected_at.strftime("%Y-%m-%d"),
            },
            "relevance": {
                "brand_matched_content": len(brand_matched),
                "unmatched_content": len(unmatched),
                "brand_match_rate": _ratio(len(brand_matched), max(len(contents), 1)),
                "prominent_content": len(prominent),
                "prominent_content_rate": _ratio(len(prominent), max(len(contents), 1)),
                "positive_selection": {
                    "total_positive_content": len(positive_pool),
                    "brand_relevant_positive": len(positive_relevant),
                    "shown": len(positive_items),
                    "rule": (
                        f"正面事件优先选择「品牌词出现在标题或正文前 {BRAND_CONTEXT_WINDOW} 字内，"
                        "或出现关键人物（如董事长姓名）」的内容，再按热度分排序"
                    ),
                },
                "unmatched_examples": [
                    {"platform": x["platform"], "title": (x.get("title") or x.get("summary", ""))[:60]}
                    for x in unmatched[:5]
                ],
                "note": "未命中品牌词的内容来自平台搜索关联结果，保留在声量统计中，但不作为重点事件推荐。",
            },
            "historical_share": {
                "items_in_trend_window": window_items,
                "items_historical": history_items,
                "historical_ratio": _ratio(history_items, max(total_items, 1)),
            },
        }
    )

    # ---- 9) 代表样本（给模型，控制 token）----
    # 第四章「重点负面舆情事件」只取负面内容帖；评论区负面情绪（尤其无负面内容帖时）需要单独给模型样例，
    # 否则模型会写出「未识别到负面内容、舆情面平稳」与「风险等级高」自相矛盾的叙述。
    negative_comment_samples = sorted(
        negative_comments,
        key=lambda x: (-(x.get("likes") or 0), x.get("published_at", "")),
    )[:SAMPLES_TOP_N]
    samples = {
        "negative": [
            {
                "rank": event["rank"],
                "platform": event["platform"],
                "title": event["title"],
                "summary": event["summary"][:160],
                "published_date": event["published_date"],
                "heat_score": event["heat_score"],
                "handling_status": event["handling_status"],
            }
            for event in negative_events[:SAMPLES_TOP_N]
        ],
        "negative_comments": [
            {
                "platform": item["platform"],
                "content": (item.get("summary") or item.get("text") or "")[:160],
                "likes": int(item.get("likes") or 0),
                "published_date": item.get("published_date", ""),
            }
            for item in negative_comment_samples
        ],
        "positive": [
            {
                "rank": event["rank"],
                "platform": event["platform"],
                "title": event["title"],
                "summary": event["summary"][:160],
                "published_date": event["published_date"],
                "heat_score": event["heat_score"],
            }
            for event in positive_events[:SAMPLES_TOP_N]
        ],
        "top_comments": comments_stats["top_liked"][:SAMPLES_TOP_N],
        "hot_keywords": [k["keyword"] for k in keywords["top"][:10]],
    }

    report_meta = {
        "title": title,
        "brand": brand,
        "report_date": report_date,
        "version": version,
        "scope_mode": mode,
        "period": {
            "start": (period_start if mode == "period" else data_span_start.strftime("%Y-%m-%d")),
            "end": (period_end if mode == "period" else collected_at.strftime("%Y-%m-%d")),
            "label": (
                f"{period_start} 至 {period_end}" if mode == "period"
                else f"数据快照（采集于 {collected_at.strftime('%Y-%m-%d')}，样本最早 {data_span_start.strftime('%Y-%m-%d')}）"
            ),
        },
        "collected_at": collected_at.strftime("%Y-%m-%d %H:%M:%S"),
        "data_span": {
            "start": data_span_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end": collected_at.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "trend_window": trend["window"],
        "copyright_owner": brand,
        "data_source": "多平台采集与情感分析",
        "template_version": "public-sentiment-weekly@v1",
    }
    if extra_meta:
        report_meta.update(extra_meta)

    stats = {
        "schema_version": SCHEMA_VERSION,
        "generator": {
            "name": "report_pipeline/compute_stats.py",
            "version": PIPELINE_VERSION,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "source": dataset.source,
        "report": report_meta,
        "totals": totals,
        "sentiment": sentiment,
        "risk": risk,
        "platform_ranking": platform_ranking,
        "trend": trend,
        "negative_events": negative_events,
        "positive_events": positive_events,
        "keywords": keywords,
        "comments": comments_stats,
        "data_quality": data_quality,
        "samples": samples,
        "warnings": list(dataset.warnings),
    }
    return stats


def stats_fingerprint(stats: Dict[str, Any]) -> str:
    """对 stats 核心数据做指纹（不含生成时间），写入 HTML 便于质检比对。"""
    core = {
        "totals": stats["totals"],
        "sentiment": stats["sentiment"],
        "platform_ranking": stats["platform_ranking"],
        "trend": stats["trend"],
        "negative_events": stats["negative_events"],
        "positive_events": stats["positive_events"],
        "keywords": stats["keywords"],
        "comments": stats["comments"],
    }
    payload = json.dumps(core, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_stats_from_excel(args: argparse.Namespace) -> Dict[str, Any]:
    dataset = load_excel(args.excel)
    stats = compute_stats(
        dataset,
        report_date=args.report_date,
        brand=args.brand,
        title=args.title,
        mode=args.mode,
        period_start=args.period_start,
        period_end=args.period_end,
        trend_end=args.trend_end,
        trend_days=args.trend_days,
        version=args.version,
    )
    stats["stats_fingerprint"] = stats_fingerprint(stats)
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="计算确定性舆情统计（stats JSON）")
    parser.add_argument("--excel", required=True, help="MediaCrawler 舆情 Excel 路径")
    parser.add_argument("--out", default="", help="stats JSON 输出路径（默认打印到 stdout）")
    parser.add_argument("--report-date", default=datetime.now().strftime("%Y-%m-%d"), help="报告日期 YYYY-MM-DD")
    parser.add_argument("--brand", default="示例科技", help="品牌名")
    parser.add_argument("--title", default="示例科技舆情监测周报", help="报告标题")
    parser.add_argument("--mode", default="snapshot", choices=["snapshot", "period"], help="统计口径")
    parser.add_argument("--period-start", default=None, help="period 模式开始日期 YYYY-MM-DD")
    parser.add_argument("--period-end", default=None, help="period 模式结束日期 YYYY-MM-DD")
    parser.add_argument("--trend-end", default=None, help="趋势窗口结束日期（默认=采集日/周期结束日）")
    parser.add_argument("--trend-days", type=int, default=14, help="趋势窗口天数（1~90，默认 14）")
    parser.add_argument("--version", type=int, default=1, help="报告版本号")
    args = parser.parse_args(argv)

    stats = build_stats_from_excel(args)
    text = json.dumps(stats, ensure_ascii=False, indent=2, default=str)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"stats 已写出：{args.out}")
        print(json.dumps({
            "total_items": stats["totals"]["items"],
            "content": stats["totals"]["content"],
            "comments": stats["totals"]["comments"],
            "positive_ratio": stats["sentiment"]["overall"]["positive"]["ratio"],
            "negative_ratio": stats["sentiment"]["overall"]["negative"]["ratio"],
            "risk": stats["risk"]["level"],
            "platform_ranking": [f"{r['platform']}:{r['mentions']}" for r in stats["platform_ranking"]],
            "trend_window_items": stats["trend"]["window_totals"]["items"],
            "stats_fingerprint": stats.get("stats_fingerprint", ""),
        }, ensure_ascii=False, indent=2))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

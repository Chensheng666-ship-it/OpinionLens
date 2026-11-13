# -*- coding: utf-8 -*-
"""
narrator.py — “叙述层”：把 stats JSON 写成人话（唯一允许产出文字内容的环节）

定位：
  * 本模块 = **模型写作通道的本地可执行替身（mock / 降级实现）**：
    无法联网或未配置模型 API 时，用确定性规则生成同等结构的叙述 JSON，
    保证流水线在无网络环境可完整跑通、可复现、可演示；
  * 生产环境可把 `local` 换成 ai_client.FeishangClient / OpenAICompatClient，
    返回同一份 narrative JSON（或完整 HTML，见 SKILL 提示词），渲染器不必改动；
  * 叙述中的**每一个数字都必须来自 stats**，本模块只做字符串拼装，不做任何计算
    （除简单的格式化/取前 N 名），确保“代码管数字、模型管表达”。

输出 narrative JSON 结构见 narrative.schema.json。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

NARRATIVE_SCHEMA_VERSION = "1.0"

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_SENTIMENT_CN = {"positive": "正面", "neutral": "中性", "negative": "负面"}


# ---------------------------------------------------------------------------
# 格式化工具（纯展示，不参与统计）
# ---------------------------------------------------------------------------
def _n(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _pct(ratio: Any, digits: int = 1) -> str:
    try:
        return f"{float(ratio) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _join(items: List[str], sep: str = "、") -> str:
    return sep.join([x for x in items if x])


def _top_names(rows: List[Dict[str, Any]], key: str, n: int = 3) -> str:
    return _join([f"{r['platform']}（{r[key]} 条）" for r in rows[:n]])


def _platform_events(events: List[Dict[str, Any]]) -> str:
    seen: List[str] = []
    for event in events:
        if event["platform"] not in seen:
            seen.append(event["platform"])
    return _join(seen)


# ---------------------------------------------------------------------------
# 各章节叙述生成
# ---------------------------------------------------------------------------
def _build_overview(stats: Dict[str, Any]) -> Dict[str, str]:
    rep, totals, sent, risk = stats["report"], stats["totals"], stats["sentiment"], stats["risk"]
    ranking = stats["platform_ranking"]
    negative_events = stats["negative_events"]
    positive_events = stats["positive_events"]
    overall = sent["overall"]
    fresh_negative = [e for e in negative_events if e["handling_code"] != "archived"]

    top_platform = ranking[0] if ranking else {"platform": "—", "mentions": 0, "share": 0.0}
    headline = (
        "负面以历史存量为主，整体风险可控"
        if not fresh_negative
        else f"存在 {len(fresh_negative)} 起近期新增负面，需重点关注"
    )
    one_line = (
        f"本期共采集到 {_n(totals['items'])} 条提及（内容 {_n(totals['content'])} 条、"
        f"评论 {_n(totals['comments'])} 条），覆盖 {totals['platforms']} 个平台；"
        f"整体呈“中性为主”结构——中性 {_pct(overall['neutral']['ratio'])}、"
        f"正面 {_pct(overall['positive']['ratio'])}、负面 {_pct(overall['negative']['ratio'])}，"
        f"风险等级「{risk['level']}」。声量最高为{top_platform['platform']}"
        f"（{_n(top_platform['mentions'])} 条，占 {_pct(top_platform['share'])}）；"
        f"负面内容 {_n(risk['negative_events'])} 起（负面评论 {_n(sent['comments']['negative']['count'])} 条），"
        + (
            "均发布于 180 天以前，属于历史存量，本期未见新增高热度负面。"
            if not fresh_negative
            else f"其中 {len(fresh_negative)} 起为近 180 天内新增，建议优先跟进。"
        )
        + f"正面内容 {_n(sent['content']['positive']['count'])} 起（本期按热度展示前 {len(positive_events)} 起），"
        "以媒体报道、品牌活动与产品科普为主。"
    )
    if rep["scope_mode"] == "snapshot":
        scope_note = (
            f"数据口径：本报告采用「数据快照」口径——数据采集截止 {rep['collected_at'].split(' ')[0]}，"
            f"样本最早回溯至 {rep['data_span']['start'].split(' ')[0]}，共 {_n(totals['items'])} 条；"
            f"第二章趋势图为采集截止日前 {stats['trend']['window']['days']} 天"
            f"（{stats['trend']['window']['start']} 至 {stats['trend']['window']['end']}）按发布时间的声量，"
            "用于观察近期传播节奏，与全量样本口径不同，详见页脚与流水线验证文档。"
        )
    else:
        scope_note = (
            f"数据口径：按发布时间过滤 {rep['period']['start']} 至 {rep['period']['end']}，"
            f"共 {_n(totals['items'])} 条；负面占比与风险等级按本期数据计算。"
        )
    return {"headline": headline, "one_line_summary": one_line, "scope_note": scope_note}


def _build_trend(stats: Dict[str, Any]) -> Dict[str, str]:
    trend = stats["trend"]
    totals = stats["totals"]
    window = trend["window"]
    wt = trend["window_totals"]
    peak = trend["peak"] or {"label": "—", "total": 0, "by_platform": {}}
    active_days = sum(1 for day in trend["days"] if day["total"] > 0)
    zero_days = len(trend["days"]) - active_days
    active_platforms = [p for p, count in wt["by_platform"].items() if count > 0]
    peak_platforms = [f"{p} {c} 条" for p, c in (peak.get("by_platform") or {}).items() if c > 0]
    share = wt["items"] / totals["items"] if totals["items"] else 0.0
    insight = (
        f"{window['start']} 至 {window['end']} 共记录发布声量 {_n(wt['items'])} 条"
        f"（内容 {_n(wt['content'])} 条、评论 {_n(wt['comments'])} 条），占全量样本 {_pct(share)}，"
        f"日均 {wt['daily_average']} 条；峰值出现在 {peak['label']}（{_n(peak['total'])} 条："
        f"{_join(peak_platforms) or '无平台数据'}）。窗口内 {active_days} 天有新增发布、{zero_days} 天无发布，"
        f"活跃平台为 {_join(active_platforms) or '无'}。"
        + (
            "样本整体以历史存量内容为主，趋势图反映的是近期发布节奏，不代表本周全量讨论量；"
            "生产环境启用采集时间窗（MediaCrawler 时间过滤）后，该图即为自然周 7 天走势。"
            if share < 0.5
            else "趋势图与全量样本口径基本一致，可直接用于判断周内传播节奏。"
        )
    )
    return {"insight": insight}


def _build_sentiment(stats: Dict[str, Any]) -> Dict[str, str]:
    sent = stats["sentiment"]
    risk = stats["risk"]
    overall, content, comments = sent["overall"], sent["content"], sent["comments"]
    insight = (
        f"全量 {_n(overall['total'])} 条样本中，正面 {_n(overall['positive']['count'])} 条"
        f"（{_pct(overall['positive']['ratio'])}）、中性 {_n(overall['neutral']['count'])} 条"
        f"（{_pct(overall['neutral']['ratio'])}）、负面 {_n(overall['negative']['count'])} 条"
        f"（{_pct(overall['negative']['ratio'])}）。"
        f"内容帖正面率 {_pct(content['positive']['ratio'])}、负面率 {_pct(content['negative']['ratio'])}；"
        f"评论正面率 {_pct(comments['positive']['ratio'])}、负面率 {_pct(comments['negative']['ratio'])}，"
        "评论区情感以中性为主。"
        + (
            f"负面占比低于 {_pct(risk['thresholds']['low'], 0)} 的低风险阈值，情感面整体健康；"
            if risk["level_code"] == "low"
            else f"负面占比已超过 {_pct(risk['thresholds']['low'], 0)} 的低风险阈值，需重点关注；"
        )
        + f"评论负面率 {_pct(comments['negative']['ratio'])} 高于内容帖负面率 {_pct(content['negative']['ratio'])}，"
        "说明讨论区仍存在可被放大的负面情绪，建议持续关注。"
    )
    return {"insight": insight}


def _build_platform(stats: Dict[str, Any]) -> Dict[str, str]:
    ranking = stats["platform_ranking"]
    if not ranking:
        return {"insight": "本期无平台声量数据。"}
    top3 = _join([f"{r['platform']}（{_n(r['mentions'])} 条，{_pct(r['share'])}）" for r in ranking[:3]])
    top = ranking[0]
    negative_top = max(ranking, key=lambda r: (r["negative_ratio"], r["mentions"]))
    insight = (
        f"声量前三为 {top3}。{top['platform']}以 {_n(top['mentions'])} 条居首，"
        f"其中内容 {_n(top['content'])} 条、评论 {_n(top['comments'])} 条，内容情感倾向为"
        f"「{top['content_dominant_sentiment']}」。"
        f"{negative_top['platform']}负面占比最高（{_pct(negative_top['negative_ratio'])}），"
        "建议优先复核该平台评论区与历史负面内容，必要时调整监测频次与关键词组合。"
    )
    return {"insight": insight}


def _negative_measure(event: Dict[str, Any]) -> Dict[str, str]:
    platform = event["platform"]
    archived_templates = {
        "小红书": "历史存量笔记，建议纳入负面台账季度复核；若被搜索场景重新推荐，联系作者沟通或走平台申诉。",
        "知乎": "历史问答负面，建议以官方认证账号补充统一回应口径，避免逐条争论，并监控问题热度。",
        "抖音": "历史短视频负面，建议纳入台账并关注评论链路，出现二次传播时联系作者或申诉。",
        "快手": "历史短视频负面，建议纳入台账并关注评论链路，出现二次传播时联系作者或申诉。",
        "微博": "历史微博负面，建议纳入台账并关注转发链路，必要时由品牌账号统一回应。",
        "B站": "历史视频负面，建议纳入台账并关注评论区变化，必要时通过官方账号说明。",
    }
    if event["handling_code"] == "archived":
        base = archived_templates.get(platform, "历史存量内容，建议纳入负面台账定期复核，关注是否有二次传播。")
        measure = f"{base}（发布距今 {event['days_old']} 天，热度分 {event['heat_score']}）"
    elif event["handling_code"] == "watch":
        measure = (
            f"近 14 天仍有负面评论互动，建议客服/品牌在 48 小时内核实事实并确定回应口径，"
            f"持续观察 7 天（当前热度分 {event['heat_score']}）。"
        )
    else:
        measure = f"建议 3 个工作日内完成事实核查与回应口径准备（当前热度分 {event['heat_score']}）。"
    if event.get("entity_risk"):
        measure = f"【主体甄别】{event['entity_risk']}；{measure}"
    return {"event_id": event["event_id"], "measure": measure, "status": event["handling_status"]}


def _positive_follow_up(event: Dict[str, Any]) -> Dict[str, Any]:
    platform = event["platform"]
    engagement = event.get("engagement", 0) or 0
    title = event.get("title", "")
    if "两会" in title or "代表" in title:
        base = "两会/代表委员相关报道，公信力强，建议整理为品牌背书素材并纳入官网、公众号案例库。"
    elif platform == "微博":
        base = "媒体/政务账号发布，传播链路正规，建议沉淀为品牌背书素材，结合节点做二次传播。"
    elif platform == "抖音":
        base = "短视频互动表现较好，建议官方账号转发/合拍，并在评论区置顶品牌与产品信息。"
    elif platform == "知乎":
        base = "问答/文章类正面内容，建议官方账号维护并补充产品与案例信息，提升长尾搜索效果。"
    elif platform == "B站":
        base = "产品科普类内容，建议系列化运营，形成产品知识库。"
    elif platform == "小红书":
        base = "种草/正面笔记，建议与作者互动并评估合作放大。"
    else:
        base = "建议整理为品牌素材，评估二次传播价值。"
    worth = bool(engagement >= 100 or float(event.get("heat_score", 0)) >= 30)
    suffix = "互动量较高，建议优先跟进放大。" if worth else "可纳入素材库，按常规节奏维护。"
    return {
        "event_id": event["event_id"],
        "follow_up": f"{base}{suffix}",
        "worth_amplifying": worth,
    }


def _build_negative(stats: Dict[str, Any]) -> Dict[str, Any]:
    events = stats["negative_events"]
    sent = stats["sentiment"]
    risk = stats["risk"]
    total_negative = int(risk["negative_events"])
    shown = len(events)
    # 负面评论口径：优先用 risk 里的新字段（与风险等级 basis 对齐），旧 stats 回退到 sentiment.comments
    negative_comments = int(
        risk.get("negative_comments", sent["comments"]["negative"]["count"]) or 0
    )
    comment_negative_ratio = float(
        risk.get("comment_negative_ratio", sent["comments"]["negative"]["ratio"]) or 0.0
    )
    overall_negative_ratio = float(
        risk.get("negative_ratio", sent["overall"]["negative"]["ratio"]) or 0.0
    )
    risk_level_code = str(risk.get("level_code") or "")
    archived = [e for e in events if e["handling_code"] == "archived"]
    fresh = [e for e in events if e["handling_code"] != "archived"]
    shown_hint = f"共 {total_negative} 起，按热度分取前 {shown} 起展示" if total_negative > shown else f"共 {total_negative} 起"
    comment_driven = bool(not events and negative_comments > 0 and risk_level_code in ("medium", "high"))
    if not events:
        if comment_driven:
            # 风险等级来自「全量（内容+评论）」负面占比，而第四章只列负面内容帖：必须说明由评论区驱动，
            # 否则会出现「风险高但没有识别到负面事件、舆情面平稳」的自相矛盾。
            intro = (
                f"本期未识别到负面内容帖；但评论区存在 {_n(negative_comments)} 条负面评论"
                f"（占评论 {_pct(comment_negative_ratio)}、占全量样本 {_pct(overall_negative_ratio)}），"
                f"风险等级「{risk['level']}」主要由评论区负面情绪驱动，建议重点排查评论区诉求"
                "（如用工、服务、售后等）并准备回应口径。"
            )
        else:
            intro = "本期未识别到负面内容，舆情面平稳。"
    elif not fresh:
        oldest = max(events, key=lambda e: e["days_old"])
        intro = (
            f"本期识别负面内容{shown_hint}（关联负面评论 {_n(negative_comments)} 条），"
            f"全部为历史存量（最早 {oldest['published_date']}，距今 {oldest['days_old']} 天），"
            "本期无新增负面事件。"
        )
    else:
        intro = (
            f"本期识别负面内容{shown_hint}（关联负面评论 {_n(negative_comments)} 条），"
            f"其中 {len(fresh)} 起为近 180 天内新增，建议优先跟进。"
        )
    entity_risk_events = [e for e in events if e.get("entity_risk")]
    comment_note = (
        f"本期负面互动集中在评论区：负面内容帖 {total_negative} 起、负面评论 {_n(negative_comments)} 条"
        f"（占评论 {_pct(comment_negative_ratio)}）；风险等级按全量（内容 + 评论）负面占比判定。"
        if comment_driven
        else ""
    )
    note = (
        "POC 环境无工单系统数据，「当前状态」由数据规则自动标注：发布时间超过 180 天标记为"
        "「历史存量·已沉淀」；近 14 天仍有负面评论互动的标记为「持续观察」；其余标记为「待评估」。"
        "生产环境接入工单系统后，应以真实处理进度（待处理/处理中/已解决/已忽略）替换。"
        + (
            f"另有 {len(entity_risk_events)} 起负面内容疑似指向同名/关联主体（非某某示例科技），"
            "已在表格中以“主体甄别”标注，建议人工复核后再决定是否纳入本公司负面口径。"
            if entity_risk_events
            else ""
        )
        + comment_note
        + f"风险等级「{risk['level']}」：{risk['basis']}。"
    )
    return {
        "intro": intro,
        "note": note,
        "measures": [_negative_measure(event) for event in events],
    }


def _build_positive(stats: Dict[str, Any]) -> Dict[str, Any]:
    events = stats["positive_events"]
    sent = stats["sentiment"]
    shown = len(events)
    platforms = _platform_events(events)
    intro = (
        f"本期识别正面内容 {_n(sent['content']['positive']['count'])} 起、"
        f"正面评论 {_n(sent['comments']['positive']['count'])} 条；"
        f"按热度分取前 {shown} 起展示（优先选择品牌相关度高的内容），覆盖 {platforms or '—'}。"
    )
    return {
        "intro": intro,
        "follow_ups": [_positive_follow_up(event) for event in events],
    }


def _build_suggestions(stats: Dict[str, Any]) -> List[str]:
    totals, sent, ranking = stats["totals"], stats["sentiment"], stats["platform_ranking"]
    comments = stats["comments"]
    events = stats["negative_events"]
    positive_events = stats["positive_events"]
    suggestions: List[str] = []

    if len(ranking) >= 2:
        top2_share = sum(r["share"] for r in ranking[:2])
        suggestions.append(
            f"平台监测：{ranking[0]['platform']} 与 {ranking[1]['platform']} 合计占声量 "
            f"{_pct(top2_share)}，是讨论主阵地；建议维持每日采集频次，并提高评论抓取上限"
            f"（本期评论 {_n(comments['total'])} 条，评论/内容比 {comments['comment_to_content_ratio']}）。"
        )
    else:
        suggestions.append(
            f"平台监测：本期声量集中在 {ranking[0]['platform'] if ranking else '少数平台'}，"
            "建议补充其他平台的采集覆盖，避免样本偏差。"
        )

    archived = [e for e in events if e["handling_code"] == "archived"]
    fresh = [e for e in events if e["handling_code"] != "archived"]
    total_negative = int(stats["risk"]["negative_events"])
    if fresh:
        suggestions.append(
            f"负面跟进：本期 {len(fresh)} 起负面为近期新增（共 {total_negative} 起），"
            "建议 48 小时内完成事实核查与回应；同时把「当前状态」接入工单系统，形成"
            "「待处理→处理中→已解决→已忽略」闭环。"
        )
    else:
        suggestions.append(
            f"负面台账：{total_negative} 起负面内容均为历史存量，建议按季度复核搜索可见性；"
            "并将「当前状态」接入工单系统，替换 POC 的数据规则占位。"
        )

    entity_risk_events = [e for e in events if e.get("entity_risk")]
    if entity_risk_events:
        suggestions.append(
            f"主体甄别：{len(entity_risk_events)} 起负面疑似指向同名/关联主体，"
            "建议在关键词库中加入排除词（如“示例网络科技”）并在报告中单列说明，避免误计入本公司负面。"
        )

    if positive_events:
        top_positive = positive_events[0]
        suggestions.append(
            f"正面放大：{top_positive['platform']} 的正面内容热度分最高（{top_positive['heat_score']}，"
            f"互动 {_n(top_positive['engagement'])}），建议整理为案例材料，推动行业媒体或政务账号二次转载。"
        )

    keywords = stats["keywords"]["related"][:3]
    if keywords:
        suggestions.append(
            f"内容策划：热词集中在「{_join([k['keyword'] for k in keywords], '」「')}」，"
            "建议围绕结构监测、智慧安全、招聘雇主品牌等方向策划选题，提升正面内容供给。"
        )

    data_quality = stats["data_quality"]
    if data_quality.get("content_id_precision_fixed") or data_quality.get("comment_ids_numeric_risk"):
        suggestions.append(
            f"数据治理：抖音/微博内容 ID 已通过原文链接修复 {_n(data_quality.get('content_id_precision_fixed', 0))} 条；"
            f"评论 ID 仍有 {_n(data_quality.get('comment_ids_numeric_risk', 0))} 条浮点精度风险，"
            "建议采集端将 ID 字段按字符串落库，避免科学计数法丢位。"
        )
    return suggestions[:6]


def _build_risk(stats: Dict[str, Any]) -> Dict[str, str]:
    risk = stats["risk"]
    events = stats["negative_events"]
    fresh = [e for e in events if e["handling_code"] != "archived"]
    max_heat = max([float(e["heat_score"]) for e in events] + [0.0])
    assessment = (
        f"风险等级「{risk['level']}」：负面占比 {_pct(risk['negative_ratio'])}"
        f"（内容负面率 {_pct(risk['content_negative_ratio'])}），负面内容 {len(events)} 起，"
        f"其中近期新增 {len(fresh)} 起；最高热度分 {max_heat}，传播范围有限。"
        + (
            "整体舆情以中性/正面为主，风险可控，建议保持常规监测节奏。"
            if risk["level_code"] == "low"
            else "建议提高监测频次并准备统一回应口径。"
        )
    )
    return {"level": risk["level"], "assessment": assessment}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def build_local_narrative(stats: Dict[str, Any]) -> Dict[str, Any]:
    """用确定性规则生成叙事 JSON（无网络、可复现，等价于 mock 模型输出）。"""
    overview = _build_overview(stats)
    narrative = {
        "schema_version": NARRATIVE_SCHEMA_VERSION,
        "generated_by": "local-rules",
        "model": "deterministic-local",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overview": overview,
        "trend": _build_trend(stats),
        "sentiment": _build_sentiment(stats),
        "platform": _build_platform(stats),
        "negative": _build_negative(stats),
        "positive": _build_positive(stats),
        "suggestions": _build_suggestions(stats),
        "suggestions_note": (
            "以上建议由统计结果驱动生成，数据支撑来自本报告 stats.json；品牌与客服团队可结合业务实际调整执行优先级。"
        ),
        "risk": _build_risk(stats),
        "disclaimer": (
            "本报告所有统计数字由 report_pipeline 代码计算，叙述由叙述层（本地规则/POC mock）基于 stats 生成，"
            "模型不参与任何统计计算；「处理进度」在 POC 中为数据规则占位，生产环境应接入工单系统后替换。"
        ),
    }
    return narrative


_REQUIRED_NARRATIVE_PATHS = [
    ("overview", "headline"),
    ("overview", "one_line_summary"),
    ("overview", "scope_note"),
    ("trend", "insight"),
    ("sentiment", "insight"),
    ("platform", "insight"),
    ("negative", "intro"),
    ("negative", "note"),
    ("negative", "measures"),
    ("positive", "intro"),
    ("positive", "follow_ups"),
    ("risk", "assessment"),
    ("disclaimer",),
]


def validate_narrative(narrative: Dict[str, Any]) -> List[str]:
    """校验叙述 JSON 的完整性（渲染前执行，避免模型少写字段导致双花括号残留）。"""
    errors: List[str] = []
    if not isinstance(narrative, dict):
        return ["narrative 不是 JSON 对象"]
    if not narrative.get("schema_version"):
        errors.append("缺少 schema_version")
    for path in _REQUIRED_NARRATIVE_PATHS:
        node: Any = narrative
        for key in path:
            if not isinstance(node, dict) or key not in node:
                errors.append(f"缺少字段 {'/'.join(path)}")
                node = None
                break
            node = node[key]
        if node is None:
            continue
        if isinstance(node, (str, list, dict)) and not node:
            errors.append(f"字段 {'/'.join(path)} 为空")
    suggestions = narrative.get("suggestions")
    if not isinstance(suggestions, list) or not suggestions:
        errors.append("suggestions 必须是非空数组")
    elif not all(isinstance(x, str) and x.strip() for x in suggestions):
        errors.append("suggestions 中存在空字符串")
    if not isinstance(narrative.get("negative", {}).get("measures"), list):
        errors.append("negative.measures 必须是数组")
    if not isinstance(narrative.get("positive", {}).get("follow_ups"), list):
        errors.append("positive.follow_ups 必须是数组")
    return errors


def merge_narrative(local: Dict[str, Any], remote: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    合并 AI 返回的叙述与本地兜底叙述：AI 字段优先，缺失/空值回落到本地。

    多轮修订场景下，AI 只需返回需要修改的字段即可（partial narrative）。
    """
    if not remote:
        return local
    merged = json.loads(json.dumps(local, ensure_ascii=False))

    def deep_merge(base: Any, extra: Any) -> Any:
        if isinstance(base, dict) and isinstance(extra, dict):
            result = dict(base)
            for key, value in extra.items():
                if key in result:
                    result[key] = deep_merge(result[key], value)
                elif value not in (None, "", [], {}):
                    result[key] = value
            return result
        if isinstance(extra, list) and extra:
            return extra
        if isinstance(extra, str) and extra.strip():
            return extra
        if isinstance(extra, (int, float, bool)) and not isinstance(extra, bool):
            return extra
        return base

    merged = deep_merge(merged, remote)
    if remote.get("generated_by"):
        merged["generated_by"] = remote["generated_by"]
    if remote.get("model"):
        merged["model"] = remote["model"]
    return merged


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="根据 stats.json 生成叙述 JSON（本地规则/mock）")
    parser.add_argument("--stats", required=True, help="stats JSON 路径")
    parser.add_argument("--out", required=True, help="narrative JSON 输出路径")
    args = parser.parse_args(argv)
    with open(args.stats, encoding="utf-8") as fh:
        stats = json.load(fh)
    narrative = build_local_narrative(stats)
    errors = validate_narrative(narrative)
    if errors:
        print("[FAIL] narrative 校验失败：", errors, file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(narrative, ensure_ascii=False, indent=2) + "\n")
    print(f"narrative 已写出：{args.out}")
    print(json.dumps({
        "generated_by": narrative["generated_by"],
        "headline": narrative["overview"]["headline"],
        "suggestions": len(narrative["suggestions"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())

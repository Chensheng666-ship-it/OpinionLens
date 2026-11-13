# -*- coding: utf-8 -*-
"""
render_html.py — 模板渲染器（确定性，不调用模型）

输入：stats JSON（数字来源）+ narrative JSON（叙述来源）+ HTML 模板 + 内联 Chart.js
输出：单文件自包含 HTML（可直接双击打开 / Electron file:// 本地渲染）

关键设计：
  * 模板里所有 {{PLACEHOLDER}} 都由本模块填充；渲染完成后若仍存在未替换占位符直接报错；
  * 所有数字来自 stats JSON，并通过 `data-stat` / `data-value` 属性写入 DOM，
    供 validate_report.py 独立复核（HTML 显示值 == stats 值）；
  * 图表数据集中在 `reportData` JSON 常量中，同样由 stats 生成，供校验脚本逐项比对；
  * Chart.js / CSS / JS 全部内联，不引用任何外部或相对路径资源；
  * 章节使用固定 id：section-overview / section-trend / section-sentiment /
    section-negative / section-platform / section-positive / section-suggestions，
    模板版本写入 <meta name="report-template-version">，供桌面端与报告管线做质检与局部修订。
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

TEMPLATE_VERSION = "public-sentiment-weekly@v1"
LOW_DATA_THRESHOLD = 10
SECTION_IDS = [
    "section-overview",
    "section-trend",
    "section-sentiment",
    "section-negative",
    "section-platform",
    "section-positive",
    "section-suggestions",
]
SECTION_TITLES = [
    "一、本周舆情总览",
    "二、声量趋势分析",
    "三、情感比例分布",
    "四、重点负面舆情事件及处理进度",
    "五、重点平台声量排名",
    "六、本周重点正面舆情",
    "七、下周关注重点与建议",
]

_BADGE_CLASS = {"archived": "badge watch", "watch": "badge doing", "pending": "badge pending"}
_DOMINANT_CLASS = {"正面为主": "positive-bar", "中性为主": "neutral-bar", "负面为主": "negative-bar"}


# ---------------------------------------------------------------------------
# 格式化工具
# ---------------------------------------------------------------------------
def _n(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return html.escape(str(value))


def _pct(ratio: Any, digits: int = 1) -> str:
    try:
        return f"{float(ratio) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _heat(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "0.0"


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


# 报告面向业务读者：不展示采集/分词库/时区/AI 协议等技术名词。
_VISIBLE_TEXT_REPLACEMENTS = (
    ("（Asia/Shanghai）", ""),
    ("(Asia/Shanghai)", ""),
    ("Asia/Shanghai", ""),
    ("MediaCrawler", "多平台采集"),
    ("ai_judge", "情感分析"),
    ("AI Judge", "情感分析"),
    ("report_pipeline", "报告系统"),
)


def _clean_text(value: Any) -> str:
    text = str(value if value is not None else "")
    for old, new in _VISIBLE_TEXT_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def _clean_esc(value: Any) -> str:
    return _esc(_clean_text(value))


def _attr(path: str, value: Any) -> str:
    """把 stats 路径与原始值写入 data 属性，供 validate_report.py 复核。"""
    if isinstance(value, bool):
        raw = "true" if value else "false"
    elif isinstance(value, (int, float)):
        raw = json.dumps(value)
    else:
        raw = str(value)
    return f' data-stat="{_esc(path)}" data-value="{_esc(raw)}"'


def _fmt_interactions(interactions: Dict[str, Any]) -> str:
    labels = [
        ("likes", "赞"), ("comments", "评"), ("shares", "转"),
        ("collects", "藏"), ("coins", "币"), ("plays", "播放"),
    ]
    parts = []
    for key, label in labels:
        value = int(interactions.get(key) or 0) if isinstance(interactions, dict) else 0
        if value:
            parts.append(f"{label} {_n(value)}")
    return " / ".join(parts) if parts else "—"


def _truncate(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# 各区块 HTML
# ---------------------------------------------------------------------------
def _render_overview_cards(stats: Dict[str, Any]) -> str:
    totals = stats["totals"]
    sent = stats["sentiment"]["overall"]
    risk = stats["risk"]
    cards = [
        f'<div class="card"><div class="label">本期总声量</div>'
        f'<div class="value"{_attr("totals.items", totals["items"])}>{_n(totals["items"])} '
        f'<small>条</small></div>'
        f'<div class="sub">内容 {_n(totals["content"])} · 评论 {_n(totals["comments"])} · {totals["platforms"]} 个平台</div></div>',
        f'<div class="card"><div class="label">正面占比</div>'
        f'<div class="value green"{_attr("sentiment.overall.positive.ratio", sent["positive"]["ratio"])}>'
        f'{_pct(sent["positive"]["ratio"])}</div>'
        f'<div class="sub">正面 {_n(sent["positive"]["count"])} 条</div></div>',
        f'<div class="card"><div class="label">负面占比</div>'
        f'<div class="value red"{_attr("sentiment.overall.negative.ratio", sent["negative"]["ratio"])}>'
        f'{_pct(sent["negative"]["ratio"])}</div>'
        f'<div class="sub">负面 {_n(sent["negative"]["count"])} 条</div></div>',
        f'<div class="card"><div class="label">风险等级</div>'
        f'<div class="value"{_attr("risk.level", risk["level"])} style="color:'
        + {"low": "#16a34a", "medium": "#d97706", "high": "#dc2626"}.get(risk["level_code"], "#1a5fb4")
        + f'">{risk["level"]}</div>'
        f'<div class="sub">重点负面事件 {_n(risk["negative_events"])} 起</div></div>',
    ]
    return "\n".join(cards)


def _render_hot_words(stats: Dict[str, Any]) -> str:
    keywords = stats["keywords"]["top"][:10]
    if not keywords:
        return '<span class="hotword w4">暂无</span>'
    max_doc = max(k["doc_count"] for k in keywords) or 1
    parts = []
    for idx, kw in enumerate(keywords):
        ratio = kw["doc_count"] / max_doc
        weight = "w1" if ratio >= 0.8 else "w2" if ratio >= 0.5 else "w3" if ratio >= 0.25 else "w4"
        parts.append(
            f'<span class="hotword {weight}"{_attr(f"keywords.top[{idx}].doc_count", kw["doc_count"])} '
            f'title="文档数 {kw["doc_count"]} / 词频 {kw["frequency"]}｜平台：{"、".join(kw["platforms"])}">'
            f'{_esc(kw["keyword"])}<em>{kw["doc_count"]}</em></span>'
        )
    return "".join(parts)


def _render_sentiment_table(stats: Dict[str, Any]) -> str:
    rows = []
    for idx, item in enumerate(stats["sentiment"]["by_platform"]):
        block = item["overall"]
        rows.append(
            "<tr>"
            f'<td class="cell-strong">{_esc(item["platform"])}</td>'
            f'<td>{_n(block["positive"]["count"])} <span class="cell-sub">{_pct(block["positive"]["ratio"])}</span></td>'
            f'<td>{_n(block["neutral"]["count"])} <span class="cell-sub">{_pct(block["neutral"]["ratio"])}</span></td>'
            f'<td>{_n(block["negative"]["count"])} <span class="cell-sub">{_pct(block["negative"]["ratio"])}</span></td>'
            f'<td{_attr(f"sentiment.by_platform[{idx}].overall.total", block["total"])}>{_n(block["total"])}</td>'
            f'<td{_attr(f"sentiment.by_platform[{idx}].overall.negative.ratio", block["negative"]["ratio"])}>'
            f'{_pct(block["negative"]["ratio"])}</td>'
            "</tr>"
        )
    total = stats["sentiment"]["overall"]
    rows.append(
        '<tr><td class="cell-strong">合计</td>'
        f'<td class="cell-strong">{_n(total["positive"]["count"])} <span class="cell-sub">{_pct(total["positive"]["ratio"])}</span></td>'
        f'<td class="cell-strong">{_n(total["neutral"]["count"])} <span class="cell-sub">{_pct(total["neutral"]["ratio"])}</span></td>'
        f'<td class="cell-strong">{_n(total["negative"]["count"])} <span class="cell-sub">{_pct(total["negative"]["ratio"])}</span></td>'
        f'<td class="cell-strong"{_attr("sentiment.overall.total", total["total"])}>{_n(total["total"])}</td>'
        f'<td class="cell-strong">{_pct(total["negative"]["ratio"])}</td></tr>'
    )
    return "\n".join(rows)


def _source_link(event: Dict[str, Any], source_links: str) -> str:
    url = event.get("url") or ""
    if not url:
        return ""
    if source_links == "text":
        return ""
    return (
        f' <a class="src-link" href="{_esc(url)}" target="_blank" rel="noopener noreferrer" '
        f'data-source-url="{_esc(url)}">原文链接</a>'
    )


def _render_negative_table(stats: Dict[str, Any], narrative: Dict[str, Any], source_links: str) -> str:
    measures = {m.get("event_id"): m for m in narrative.get("negative", {}).get("measures", [])}
    rows = []
    for idx, event in enumerate(stats["negative_events"]):
        measure = measures.get(event["event_id"], {})
        badge_class = _BADGE_CLASS.get(event["handling_code"], "badge watch")
        entity_note = (
            f'<span class="cell-sub" style="color:#b45309">⚠ {_esc(event["entity_risk"])}</span>'
            if event.get("entity_risk")
            else ""
        )
        rows.append(
            "<tr>"
            f'<td><span class="rank-num r{min(idx + 1, 6)}">{event["rank"]}</span></td>'
            f'<td class="left"><span class="cell-strong">{_esc(event["title"])}</span>{entity_note}'
            f'{_source_link(event, source_links)}</td>'
            f'<td>{_esc(event["platform"])}</td>'
            f'<td>{_esc(event["published_date"])}</td>'
            f'<td class="left">{_esc(_fmt_interactions(event["interactions"]))}'
            f'<span class="cell-sub">热度分 {_heat(event["heat_score"])} · 全量内容第 {event["heat_rank"]} 位</span></td>'
            f'<td class="left">{_esc(_truncate(event["summary"], 88))}</td>'
            f'<td class="left">{_esc(_truncate(measure.get("measure", "—"), 112))}'
            f'<span class="cell-sub">{_esc(event["handling_basis"])}</span></td>'
            f'<td><span class="{badge_class}">{_esc(event["handling_status"])}</span></td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="8">本期未识别到负面事件</td></tr>')
    return "\n".join(rows)


def _render_platform_table(stats: Dict[str, Any]) -> str:
    ranking = stats["platform_ranking"]
    if not ranking:
        return '<tr><td colspan="7">本期无平台声量数据</td></tr>'
    top_platform = ranking[0]["platform"]
    negative_top = max(ranking, key=lambda r: (r["negative_ratio"], r["mentions"]))
    rows = []
    for row in ranking:
        dominant = row["content_dominant_sentiment"]
        rank_index = row["rank"] - 1
        if row["platform"] == top_platform:
            note = f"声量第一；内容 {_n(row['content'])} 条、评论 {_n(row['comments'])} 条"
        elif row["platform"] == negative_top["platform"]:
            note = f"负面占比最高（{_pct(row['negative_ratio'])}），建议加强监测"
        else:
            note = "常规监测"
        rows.append(
            "<tr>"
            f'<td><span class="rank-num r{min(row["rank"], 6)}">{row["rank"]}</span></td>'
            f'<td class="cell-strong">{_esc(row["platform"])}</td>'
            f'<td{_attr(f"platform_ranking[{rank_index}].mentions", row["mentions"])}>{_n(row["mentions"])}</td>'
            f'<td{_attr(f"platform_ranking[{rank_index}].share", row["share"])}>{_pct(row["share"])}</td>'
            f'<td>{_n(row["content"])} / {_n(row["comments"])}</td>'
            f'<td><span class="{_DOMINANT_CLASS.get(dominant, "neutral-bar")}">{_esc(dominant)}</span></td>'
            f'<td class="left">{_esc(note)}</td>'
            "</tr>"
        )
    return "\n".join(rows)


def _render_positive_table(stats: Dict[str, Any], narrative: Dict[str, Any], source_links: str) -> str:
    follow_ups = {f.get("event_id"): f for f in narrative.get("positive", {}).get("follow_ups", [])}
    rows = []
    for idx, event in enumerate(stats["positive_events"]):
        follow = follow_ups.get(event["event_id"], {})
        worth = "是" if follow.get("worth_amplifying") else "否"
        badge = "badge solved" if follow.get("worth_amplifying") else "badge info"
        rows.append(
            "<tr>"
            f'<td>{event["rank"]}</td>'
            f'<td class="left"><span class="cell-strong">{_esc(_truncate(event["title"], 56))}</span>'
            f'<span class="cell-sub">{_esc(_truncate(event["summary"], 76))}</span></td>'
            f'<td>{_esc(event["platform"])}</td>'
            f'<td class="left">{_esc(_fmt_interactions(event["interactions"]))}'
            f'<span class="cell-sub">热度分 {_heat(event["heat_score"])}</span></td>'
            f'<td><span class="{badge}">{worth}</span></td>'
            f'<td class="left">{_esc(follow.get("follow_up", "—"))}'
            f'{_source_link(event, source_links)}</td>'
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="6">本期未识别到正面事件</td></tr>')
    return "\n".join(rows)


def _render_suggestions(narrative: Dict[str, Any]) -> str:
    suggestions = narrative.get("suggestions") or []
    if not suggestions:
        return '<div class="suggestion"><b>1.</b> 暂无</div>'
    return "\n".join(
        f'<div class="suggestion"><b>{idx + 1}.</b> {_clean_esc(text)}</div>'
        for idx, text in enumerate(suggestions)
    )


# ---------------------------------------------------------------------------
# 图表数据（reportData）
# ---------------------------------------------------------------------------
def build_report_data(stats: Dict[str, Any]) -> Dict[str, Any]:
    trend = stats["trend"]
    platforms = trend["platforms"]
    series = {p: [day["by_platform"].get(p, 0) for day in trend["days"]] for p in platforms}
    totals = [day["total"] for day in trend["days"]]
    labels = [day["label"] for day in trend["days"]]

    sent = stats["sentiment"]["overall"]
    sentiment_values = [
        round(sent["positive"]["ratio"] * 100, 1),
        round(sent["neutral"]["ratio"] * 100, 1),
        round(sent["negative"]["ratio"] * 100, 1),
    ]
    # 保证环形图三项之和为 100.0（取一位小数后把舍入差补到最大项）
    delta = round(100.0 - sum(sentiment_values), 1)
    if abs(delta) >= 0.1:
        max_idx = max(range(3), key=lambda i: sentiment_values[i])
        sentiment_values[max_idx] = round(sentiment_values[max_idx] + delta, 1)

    ranking = stats["platform_ranking"]
    return {
        "templateVersion": TEMPLATE_VERSION,
        "report": {
            "id": "",
            "date": stats["report"]["report_date"],
            "version": stats["report"]["version"],
            "scopeMode": stats["report"]["scope_mode"],
            "statsFingerprint": "",
        },
        "trend": {
            "window": trend["window"],
            "labels": labels,
            "platforms": platforms,
            "series": series,
            "total": totals,
        },
        "sentiment": {
            "labels": [
                f"正面 {_pct(sent['positive']['ratio'])}",
                f"中性 {_pct(sent['neutral']['ratio'])}",
                f"负面 {_pct(sent['negative']['ratio'])}",
            ],
            "values": sentiment_values,
            "counts": [sent["positive"]["count"], sent["neutral"]["count"], sent["negative"]["count"]],
            "centerValue": _n(sent["total"]),
            "centerLabel": "样本总量（条）",
        },
        "platform": {
            "names": [row["platform"] for row in ranking],
            "counts": [row["mentions"] for row in ranking],
        },
    }


# ---------------------------------------------------------------------------
# 主渲染
# ---------------------------------------------------------------------------
def _read_chart_js(chart_js_path: str) -> str:
    with open(chart_js_path, encoding="utf-8") as fh:
        code = fh.read()
    # 去掉 sourceMappingURL 注释（避免相对路径引用，虽然注释不会加载资源）
    code = re.sub(r"//#\s*sourceMappingURL=.*$", "", code, flags=re.M)
    if "</script" in code.lower():
        code = re.sub(r"</script", r"<\\/script", code, flags=re.I)
    return code


def render_report(
    stats: Dict[str, Any],
    narrative: Dict[str, Any],
    *,
    template_path: str,
    chart_js_path: str,
    out_path: str,
    pipeline_version: str = "1.0.0",
    source_links: str = "anchor",
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    """渲染单文件 HTML，返回构建清单（用于写 manifest / 质检）。"""
    with open(template_path, encoding="utf-8") as fh:
        template = fh.read()

    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fingerprint = stats.get("stats_fingerprint", "")
    report_date = stats["report"]["report_date"]
    report_version = stats["report"]["version"]
    report_id = f"PSW-{report_date.replace('-', '')}-{(fingerprint or '00000000')[:8].upper()}"
    totals = stats["totals"]
    quality = stats["data_quality"]

    scope_mode_label = "数据快照" if stats["report"]["scope_mode"] == "snapshot" else "自然周期"
    meta_line = (
        f"报告日期：{_esc(report_date)}"
        f"　｜　数据来源：多平台采集与情感分析"
    )
    # 口径只给业务时间范围，不展示时区/采集工具/AI 判定等实现细节
    if stats["report"]["scope_mode"] == "period":
        period = stats["report"].get("period") or {}
        scope_line = _esc(
            f"统计时间：{period.get('start', '')} 至 {period.get('end', '')}。"
        )
    else:
        scope_line = _esc(
            f"数据截至：{str(stats['report'].get('collected_at', '')).split(' ')[0]}。"
        )

    # 图表数据 & 指纹写回
    report_data = build_report_data(stats)
    report_data["report"]["id"] = report_id
    report_data["report"]["statsFingerprint"] = fingerprint
    report_data_json = json.dumps(report_data, ensure_ascii=False, indent=None, separators=(",", ":"))
    report_data_json = report_data_json.replace("</", "<\\/")

    footer_line_1 = f"生成时间：{_esc(generated_at)}"
    footer_line_2 = (
        f"数据质量：原始数据 "
        f"{_n(quality.get('content_rows', 0) + quality.get('comment_rows', 0))} 行，"
        f"去重后 {_n(totals['items'])} 条"
        f"（内容 {_n(totals['content'])} + 评论 {_n(totals['comments'])}）。"
    )
    footer_line_3 = ""

    # 样本量过少时在报告正文顶部给出提醒（0 条不会进入渲染，由 sidecar 直接终止）
    data_warning = ""
    try:
        item_count = int(totals.get("items", 0) or 0)
    except (TypeError, ValueError):
        item_count = 0
    if 0 < item_count < LOW_DATA_THRESHOLD:
        data_warning = (
            '<div class="data-warning" id="report-data-warning">'
            f"<b>数据量提醒：</b>本期有效数据仅 {_n(item_count)} 条，"
            "样本量较少，趋势、情感判断和结论仅供参考。"
            "</div>"
        )

    replacements = {
        "PIPELINE_VERSION": _esc(pipeline_version),
        "TEMPLATE_VERSION": _esc(TEMPLATE_VERSION),
        "REPORT_VERSION": str(report_version),
        "REPORT_ID": _esc(report_id),
        "REPORT_DATE": _esc(report_date),
        "SCOPE_MODE": _esc(stats["report"]["scope_mode"]),
        "STATS_FINGERPRINT": _esc(fingerprint),
        "TITLE": _esc(stats["report"]["title"]),
        "META_LINE": meta_line,
        "SCOPE_LINE": scope_line,
        "DATA_WARNING": data_warning,
        "OVERVIEW_CARDS": _render_overview_cards(stats),
        "OVERVIEW_SUMMARY": _clean_esc(narrative.get("overview", {}).get("one_line_summary", "")),
        "HOT_WORDS": _render_hot_words(stats),
        "TREND_CHART_TITLE": _esc(
            f"近 {stats['trend']['window']['days']} 日每日声量趋势"
            f"（{stats['trend']['window']['start']} 至 {stats['trend']['window']['end']}，按平台堆叠）"
        ),
        "TREND_INSIGHT": _clean_esc(narrative.get("trend", {}).get("insight", "")),
        "SENTIMENT_TOTAL": _n(totals["items"]),
        "SENTIMENT_TABLE": _render_sentiment_table(stats),
        "SENTIMENT_INSIGHT": _clean_esc(narrative.get("sentiment", {}).get("insight", "")),
        "NEGATIVE_INTRO": _clean_esc(narrative.get("negative", {}).get("intro", "")),
        "NEGATIVE_TABLE": _render_negative_table(stats, narrative, source_links),
        "NEGATIVE_NOTE": _clean_esc(narrative.get("negative", {}).get("note", "")),
        "PLATFORM_TABLE": _render_platform_table(stats),
        "PLATFORM_INSIGHT": _clean_esc(narrative.get("platform", {}).get("insight", "")),
        "POSITIVE_INTRO": _clean_esc(narrative.get("positive", {}).get("intro", "")),
        "POSITIVE_TABLE": _render_positive_table(stats, narrative, source_links),
        "SUGGESTIONS": _render_suggestions(narrative),
        "SUGGESTIONS_NOTE": _clean_esc(narrative.get("suggestions_note", "")),
        "FOOTER_LINE_1": footer_line_1,
        "FOOTER_LINE_2": footer_line_2,
        "FOOTER_LINE_3": footer_line_3,
        "REPORT_DATA_JSON": report_data_json,
        "CHART_JS": _read_chart_js(chart_js_path),
    }

    rendered = template
    for key, value in replacements.items():
        rendered = rendered.replace("{{" + key + "}}", value)

    leftover = sorted(set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", rendered)))
    if leftover:
        raise ValueError(f"模板存在未替换占位符：{leftover}")

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(rendered)

    html_bytes = rendered.encode("utf-8")
    manifest = {
        "html_path": os.path.abspath(out_path),
        "html_size_bytes": len(html_bytes),
        "html_sha256": hashlib.sha256(html_bytes).hexdigest(),
        "template_path": os.path.abspath(template_path),
        "template_version": TEMPLATE_VERSION,
        "chart_js_path": os.path.abspath(chart_js_path),
        "chart_js_size_bytes": os.path.getsize(chart_js_path),
        "report_id": report_id,
        "report_version": report_version,
        "report_date": report_date,
        "stats_fingerprint": fingerprint,
        "generated_at": generated_at,
        "source_links_mode": source_links,
        "section_ids": SECTION_IDS,
        "report_data": report_data,
        "narrative_generated_by": narrative.get("generated_by", "local-rules"),
    }
    return manifest


def write_manifest(manifest: Dict[str, Any], path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":  # 手工调试：stats + narrative -> html
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="用 stats + narrative 渲染 HTML 报告")
    parser.add_argument("--stats", required=True)
    parser.add_argument("--narrative", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--template", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                          "templates", "weekly_report.template.html"))
    parser.add_argument("--chart-js", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                           "assets", "chart.umd.min.js"))
    parser.add_argument("--source-links", default="anchor", choices=["anchor", "text"])
    args = parser.parse_args()
    with open(args.stats, encoding="utf-8") as fh:
        stats = json.load(fh)
    with open(args.narrative, encoding="utf-8") as fh:
        narrative = json.load(fh)
    manifest = render_report(
        stats, narrative, template_path=args.template, chart_js_path=args.chart_js,
        out_path=args.out, source_links=args.source_links,
    )
    print(json.dumps({k: manifest[k] for k in (
        "html_path", "html_size_bytes", "html_sha256", "report_id", "report_version",
        "stats_fingerprint")}, ensure_ascii=False, indent=2))


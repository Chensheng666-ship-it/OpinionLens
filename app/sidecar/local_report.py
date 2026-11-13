# -*- coding: utf-8 -*-
"""本地模板报告渲染：模型 API 未配置或调用失败时的兜底。

优先复用项目 ``poc/report_pipeline``（与 ``report_data`` 同一路径规则）：
``FS_REPORT_PIPELINE_DIR`` 或项目内相对路径 ``poc/report_pipeline`` 的
``narrator.build_local_narrative`` + ``render_html.render_report`` + 模板/内联 Chart.js。

POC 不可用（缺文件/依赖异常/stats 字段不满足模板）时，退回**内置极简自包含模板**，
保证任何情况下都能生成可打开的 HTML（含标题、窗口、条数、情感、平台 Top、说明文字）。
"""
from __future__ import annotations

import html as html_module
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

LOCAL_TEMPLATE_VERSION = "local-template@v1"
LOCAL_NARRATIVE_GENERATED_BY = "local-template"
DEFAULT_TITLE = "舆情监测周报"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def resolve_pipeline_dir() -> Optional[Path]:
    """定位报告流水线目录（与 report_data.py 规则一致）。"""
    candidates: List[Path] = []
    env_dir = (os.environ.get("FS_REPORT_PIPELINE_DIR") or "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parents[1] / "report_pipeline",           # app/sidecar -> app/report_pipeline
            here.parent / "report_pipeline",               # app/sidecar/report_pipeline
            here.parents[2] / "app" / "report_pipeline",   # project/app/report_pipeline
            here.parents[2] / "poc" / "report_pipeline",   # 兼容旧目录
            Path.cwd() / "app" / "report_pipeline",
            Path.cwd() / "poc" / "report_pipeline",
        ]
    )
    for candidate in candidates:
        if (candidate / "render_html.py").is_file() and (
            candidate / "narrator.py"
        ).is_file():
            return candidate
    return None


def _escape(value: Any) -> str:
    return html_module.escape(str(value if value is not None else ""))


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def _sentiment_summary(stats: Dict[str, Any]) -> List[Dict[str, Any]]:
    sentiment = stats.get("sentiment") if isinstance(stats.get("sentiment"), dict) else {}
    rows: List[Dict[str, Any]] = []
    for key, label in (("positive", "正面"), ("neutral", "中性"), ("negative", "负面")):
        item = sentiment.get(key)
        if isinstance(item, dict):
            count = item.get("count", item.get("items", 0))
            ratio = item.get("ratio", item.get("share", 0))
        else:
            count, ratio = item, 0
        rows.append(
            {
                "label": label,
                "count": _fmt_int(count),
                "ratio": _format_ratio(ratio),
            }
        )
    return rows


def _format_ratio(value: Any) -> str:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return "0.0%"
    if ratio <= 1.0:
        ratio *= 100
    return f"{ratio:.1f}%"


def _platform_top(stats: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    ranking = stats.get("platform_ranking")
    rows: List[Dict[str, Any]] = []
    if isinstance(ranking, list):
        for item in ranking[:limit]:
            if isinstance(item, dict):
                rows.append(
                    {
                        "platform": str(item.get("platform") or item.get("name") or ""),
                        "content": _fmt_int(item.get("content") or item.get("contents") or 0),
                        "comments": _fmt_int(item.get("comments") or 0),
                        "interactions": _fmt_int(
                            item.get("interactions") or item.get("total_interactions") or 0
                        ),
                    }
                )
    return rows


def _pct_text(value: Any) -> str:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return "0.0%"
    if ratio <= 1.0:
        ratio *= 100
    return f"{ratio:.1f}%"


def _narrative_comment_facts(stats: Dict[str, Any]) -> Dict[str, Any]:
    """风险等级与第四章「重点负面事件」口径对齐所需事实（评论负面情况）。"""
    risk = stats.get("risk") if isinstance(stats.get("risk"), dict) else {}
    sentiment = stats.get("sentiment") if isinstance(stats.get("sentiment"), dict) else {}
    comment_block = sentiment.get("comments") if isinstance(sentiment.get("comments"), dict) else {}
    comment_negative = comment_block.get("negative") if isinstance(comment_block.get("negative"), dict) else {}
    overall_block = sentiment.get("overall") if isinstance(sentiment.get("overall"), dict) else {}
    overall_negative = overall_block.get("negative") if isinstance(overall_block.get("negative"), dict) else {}
    return {
        "level": str(risk.get("level") or ""),
        "level_code": str(risk.get("level_code") or ""),
        "negative_comments": int(risk.get("negative_comments", comment_negative.get("count", 0)) or 0),
        "comment_negative_ratio": risk.get("comment_negative_ratio", comment_negative.get("ratio", 0.0)),
        "overall_negative_ratio": risk.get("negative_ratio", overall_negative.get("ratio", 0.0)),
    }


def reconcile_narrative(stats: Dict[str, Any], narrative: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """后处理叙述，修正「风险等级高但第四章未识别到负面事件」的矛盾（客户反馈过）。

    背景：风险等级按「全量（内容 + 评论）」负面占比判定；第四章表格仅列负面内容帖。
    当本期无负面内容帖、但评论区负面占比高（风险等级为中/高）时，服务端 AI（或本地规则早期版本）
    容易写出「本期未识别到负面内容，舆情面平稳」，与风险等级相互矛盾。这里统一改为
    「风险来自评论区」的表述，并补充风险口径说明（不改 HTML 模板/版式）。
    """
    if not isinstance(narrative, dict):
        return {}
    stats = stats if isinstance(stats, dict) else {}
    events = stats.get("negative_events") if isinstance(stats.get("negative_events"), list) else []
    facts = _narrative_comment_facts(stats)
    if events or facts["level_code"] not in ("medium", "high") or facts["negative_comments"] <= 0:
        return narrative
    negative = narrative.get("negative") if isinstance(narrative.get("negative"), dict) else {}
    negative = dict(negative)
    intro = (
        f"本期未识别到负面内容帖；但评论区存在 {facts['negative_comments']} 条负面评论"
        f"（占评论 {_pct_text(facts['comment_negative_ratio'])}、占全量样本 {_pct_text(facts['overall_negative_ratio'])}），"
        f"风险等级「{facts['level'] or '—'}」主要由评论区负面情绪驱动，建议重点排查评论区诉求"
        "（如用工、服务、售后等）并准备回应口径。"
    )
    fact_note = (
        f"本期负面互动集中在评论区：负面内容帖 {len(events)} 起、负面评论 {facts['negative_comments']} 条"
        f"（占评论 {_pct_text(facts['comment_negative_ratio'])}）；风险等级按全量（内容 + 评论）负面占比判定。"
    )
    note = str(negative.get("note") or "").strip()
    if "评论区" not in note:
        note = f"{note} {fact_note}".strip() if note else fact_note
    elif "风险等级" not in note:
        note = f"{note} {fact_note}".strip()
    negative["intro"] = intro
    negative["note"] = note
    if not isinstance(negative.get("measures"), list):
        negative["measures"] = []
    reconciled = dict(narrative)
    reconciled["negative"] = negative
    return reconciled


def _builtin_html(
    package: Dict[str, Any],
    stats: Dict[str, Any],
    *,
    title: str,
    reason: str,
    instruction: str,
    version: int,
) -> str:
    report = package.get("report") if isinstance(package.get("report"), dict) else {}
    window = report.get("window") if isinstance(report.get("window"), dict) else {}
    window_start = str(window.get("start") or "")
    window_end = str(window.get("end") or "")
    keywords = [str(item) for item in (report.get("keywords") or []) if str(item).strip()]
    platforms = [str(item) for item in (report.get("platforms") or []) if str(item).strip()]
    counts = package.get("counts") if isinstance(package.get("counts"), dict) else {}
    content = counts.get("content", 0)
    comments = counts.get("comments", 0)
    total = counts.get("total", content + comments)
    sentiment_rows = _sentiment_summary(stats)
    platform_rows = _platform_top(stats)
    sentiment_html = "".join(
        f"<tr><td>{_escape(row['label'])}</td><td>{_escape(row['count'])}</td>"
        f"<td>{_escape(row['ratio'])}</td></tr>"
        for row in sentiment_rows
    )
    platform_html = "".join(
        f"<tr><td>{_escape(row['platform'])}</td><td>{_escape(row['content'])}</td>"
        f"<td>{_escape(row['comments'])}</td><td>{_escape(row['interactions'])}</td></tr>"
        for row in platform_rows
    ) or "<tr><td colspan='4'>本期无平台互动数据</td></tr>"
    keywords_text = "、".join(keywords) if keywords else "全部关键词"
    platforms_text = "、".join(platforms) if platforms else "全部平台"
    instruction_block = (
        f"<section class='revision'><h2>修订说明</h2><p>{_escape(instruction)}</p></section>"
        if instruction
        else ""
    )
    reason_block = (
        f"<p class='reason'>{_escape(reason)}</p>" if reason else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}（本地模板 v{version}）</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
         margin: 0; padding: 32px; background: #f5f7fb; color: #1f2937; }}
  .wrap {{ max-width: 960px; margin: 0 auto; background: #fff; border-radius: 16px;
          padding: 32px 40px; box-shadow: 0 8px 28px rgba(15, 23, 42, .08); }}
  h1 {{ margin: 0 0 6px; font-size: 26px; }}
  h2 {{ margin: 26px 0 10px; font-size: 18px; border-left: 4px solid #2563eb; padding-left: 10px; }}
  .meta {{ color: #6b7280; font-size: 13px; margin-bottom: 18px; }}
  .badge {{ display: inline-block; background: #fef3c7; color: #92400e; border-radius: 999px;
           padding: 2px 10px; font-size: 12px; margin-left: 8px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; }}
  .card {{ background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 12px; padding: 14px 16px; }}
  .card .num {{ font-size: 26px; font-weight: 700; color: #1d4ed8; }}
  .card .label {{ color: #6b7280; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 14px; }}
  th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px 10px; text-align: left; }}
  th {{ background: #f1f5f9; color: #475569; }}
  .reason {{ background: #fff7ed; border: 1px solid #fed7aa; color: #9a3412;
             border-radius: 10px; padding: 10px 14px; font-size: 13px; }}
  .revision {{ margin-top: 24px; padding: 14px 18px; background: #e6f4f1; border-radius: 12px; }}
  footer {{ margin-top: 28px; color: #94a3b8; font-size: 12px; text-align: center; }}
</style></head><body><div class="wrap">
  <h1>{_escape(title)}<span class="badge">本地模板生成 · 无 AI 叙述</span></h1>
  <div class="meta">统计窗口：{_escape(window_start)} ~ {_escape(window_end)}　｜　关键词：{_escape(keywords_text)}　｜　平台：{_escape(platforms_text)}</div>
  {reason_block}
  <h2>一、数据概览</h2>
  <div class="cards">
    <div class="card"><div class="num">{_fmt_int(content)}</div><div class="label">内容数</div></div>
    <div class="card"><div class="num">{_fmt_int(comments)}</div><div class="label">评论数</div></div>
    <div class="card"><div class="num">{_fmt_int(total)}</div><div class="label">总条数</div></div>
  </div>
  <h2>二、情感分布</h2>
  <table><thead><tr><th>情感</th><th>条数</th><th>占比</th></tr></thead>
  <tbody>{sentiment_html}</tbody></table>
  <h2>三、平台分布 Top {len(platform_rows) or 0}</h2>
  <table><thead><tr><th>平台</th><th>内容</th><th>评论</th><th>互动</th></tr></thead>
  <tbody>{platform_html}</tbody></table>
  {instruction_block}
  <footer>本报告由 exe 本地模板生成（无 AI 叙述）；数据统计由 report_data 代码完成。<br>
  生成时间：{_escape(_now_text())} · 本地模板 {LOCAL_TEMPLATE_VERSION} · 版本 v{version}</footer>
</div></body></html>"""


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def render_local_report(
    package: Dict[str, Any],
    *,
    title: str = "",
    version: int = 1,
    instruction: str = "",
    reason: str = "",
    out_path: Optional[str] = None,
    narrative: Optional[Dict[str, Any]] = None,
    narrative_generated_by: str = LOCAL_NARRATIVE_GENERATED_BY,
) -> Dict[str, Any]:
    """渲染本地模板 HTML；返回 ``{html, engine, template_version, bytes}``。

    - 优先 POC ``render_html.render_report``（含内联 Chart.js）；
    - 传入 ``narrative`` 时使用服务端 AI 叙述；否则 ``narrator.build_local_narrative``；
    - POC 不可用/渲染异常 → 内置极简模板（永远成功）。
    """
    package = package if isinstance(package, dict) else {}
    stats = package.get("stats") if isinstance(package.get("stats"), dict) else {}
    report = package.get("report") if isinstance(package.get("report"), dict) else {}
    resolved_title = (
        str(title or "").strip()
        or str(report.get("title") or "").strip()
        or DEFAULT_TITLE
    )
    html = ""
    engine = "builtin"
    template_version = LOCAL_TEMPLATE_VERSION
    poc_error = ""
    pipeline_dir = resolve_pipeline_dir()
    if pipeline_dir is not None:
        try:
            import sys

            pipeline_str = str(pipeline_dir)
            if pipeline_str not in sys.path:
                sys.path.insert(0, pipeline_str)
            from narrator import build_local_narrative  # type: ignore
            from render_html import TEMPLATE_VERSION, render_report  # type: ignore

            narrative_data = dict(narrative) if isinstance(narrative, dict) else build_local_narrative(stats)
            if instruction:
                narrative_data = dict(narrative_data)
                narrative_data["revision_instruction"] = instruction
            # 统一口径：风险等级按全量负面占比、第四章只列负面内容帖——不一致时纠正叙述，避免自相矛盾
            narrative_data = reconcile_narrative(stats, narrative_data)
            template_path = pipeline_dir / "templates" / "weekly_report.template.html"
            chart_js_path = pipeline_dir / "assets" / "chart.umd.min.js"
            if not template_path.is_file() or not chart_js_path.is_file():
                raise FileNotFoundError("缺少 POC 模板/chart.js")
            fd, tmp_name = tempfile.mkstemp(prefix="local_report_", suffix=".html")
            os.close(fd)
            try:
                render_report(
                    stats,
                    narrative_data,
                    template_path=str(template_path),
                    chart_js_path=str(chart_js_path),
                    out_path=tmp_name,
                )
                html = Path(tmp_name).read_text(encoding="utf-8")
            finally:
                try:
                    Path(tmp_name).unlink()
                except OSError:
                    pass
            engine = "poc"
            template_version = TEMPLATE_VERSION
        except Exception as exc:  # noqa: BLE001 - POC 不可用是预期兜底场景
            poc_error = f"{type(exc).__name__}: {exc}"
            html = ""
    if not html.strip():
        html = _builtin_html(
            package,
            stats,
            title=resolved_title,
            reason=reason,
            instruction=instruction,
            version=int(version or 1),
        )
        engine = "builtin"
        template_version = LOCAL_TEMPLATE_VERSION
    # 本地修订说明注入（POC 路径模板不会渲染 narrative.revision_instruction）
    if instruction and engine == "poc":
        note = (
            "<section class=\"local-revision-note\" style=\"margin:24px auto;max-width:960px;"
            "padding:14px 18px;background:#e6f4f1;border-radius:12px;font-family:'Microsoft YaHei',sans-serif;\">"
            f"<h2 style=\"margin:0 0 6px;font-size:16px;\">修订说明</h2>"
            f"<p style=\"margin:0;color:#1e3a8a;\">{_escape(instruction)}</p></section>"
        )
        if "</body>" in html:
            html = html.replace("</body>", note + "</body>", 1)
        else:
            html += note
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
    return {
        "html": html,
        "engine": engine,
        "template_version": template_version,
        "bytes": len(html.encode("utf-8")),
        "poc_error": poc_error,
        "title": resolved_title,
        "narrative_generated_by": narrative_generated_by,
    }


__all__ = [
    "LOCAL_TEMPLATE_VERSION",
    "LOCAL_NARRATIVE_GENERATED_BY",
    "reconcile_narrative",
    "render_local_report",
    "resolve_pipeline_dir",
]

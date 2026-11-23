# -*- coding: utf-8 -*-
"""Weekly-report placeholder re-substitution.

Crawled titles and opening text are HTML-escaped into the event tables, then
``render_report`` replaces ``{{PLACEHOLDER}}`` tokens on the whole document.
A token that arrived inside a post is expanded on that later pass. These tests
drive the product path (MediaCrawler SQLite → ``build_report_package`` →
``render_local_report``) and require the chart library to stay inside
``<script id="chartjs-inline">``.

They fail while that second pass still scans already inserted text.

Sentiment labels come from ``report_package._stub_analyzer``, the offline
analyzer shipped with the package builder. It implements the same callback
``report_manager`` passes (``sentiment_analyzer``): the crawled title is not
rewritten, and a complaint/praise word on the post selects the event table.
The desktop app wires the model client to that same callback.

Platforms that share ``title_cols`` (抖音, 快手, B站, 贴吧, 知乎) take the same
``_row_title`` path as the 小红书 cases below and are not repeated. Comment
bodies are not rendered into the weekly template. Hot words cannot carry
``{{CHART_JS}}`` because keyword tokens are at most 8 characters. The Excel
CLI is not the crawl path the app uses to build a report.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Sequence

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_DIR = ROOT / "app" / "sidecar"
for _path in (SIDECAR_DIR,):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from local_report import render_local_report  # noqa: E402
from report_package import _stub_analyzer, build_report_package  # noqa: E402

WINDOW_START = "2026-11-03"
WINDOW_END = "2026-11-09"
PUBLISHED = "2026-11-08 12:00:00"
KEYWORD = "示例科技"
CHART_MARKER = "Do NOT use SRI with dynamically generated files!"
CHART_PLACEHOLDER = "{{CHART_JS}}"
SUGGESTION_PHRASE = "避免样本偏差"


def _chart_copies(html: str) -> tuple:
    """Return (copies inside the inline script, copies everywhere else)."""
    start = html.find('<script id="chartjs-inline">')
    if start < 0:
        return 0, html.count(CHART_MARKER)
    end = html.find("</script>", start)
    if end < 0:
        return 0, html.count(CHART_MARKER)
    inside = html[start:end].count(CHART_MARKER)
    outside = html[:start].count(CHART_MARKER) + html[end:].count(CHART_MARKER)
    return inside, outside


def _insert(conn: sqlite3.Connection, table: str, rows: Sequence[Dict[str, Any]]) -> None:
    columns: List[str] = sorted({key for row in rows for key in row})
    definitions = ["id INTEGER PRIMARY KEY AUTOINCREMENT"]
    for column in columns:
        kind = "INTEGER" if column in ("add_ts", "last_modify_ts") else "TEXT"
        definitions.append(f'"{column}" {kind}')
    conn.execute(f'CREATE TABLE "{table}" ({", ".join(definitions)})')
    sql = (
        f'INSERT INTO "{table}" ({",".join(chr(34) + c + chr(34) for c in columns)}) '
        f'VALUES ({",".join("?" for _ in columns)})'
    )
    conn.executemany(sql, [[row.get(column) for column in columns] for row in rows])


def _common() -> Dict[str, Any]:
    return {
        "creator_hash": "u1",
        "nickname": "作者",
        "add_ts": 1,
        "last_modify_ts": 1,
        "source_keyword": KEYWORD,
    }


def _xhs(note_id: str, title: str, desc: str, *, likes: str = "20") -> Dict[str, Any]:
    row = _common()
    row.update(
        {
            "note_id": note_id,
            "title": title,
            "desc": desc,
            "time": PUBLISHED,
            "note_url": f"https://example.test/{note_id}",
            "liked_count": likes,
            "comment_count": "0",
            "collected_count": "0",
            "share_count": "0",
        }
    )
    return row


def _render_notes(
    notes: Sequence[Dict[str, Any]],
    *,
    comments: Sequence[Dict[str, Any]] = (),
    extra_tables: Dict[str, Sequence[Dict[str, Any]]] | None = None,
    platforms: Sequence[str] = ("xhs",),
    narrative: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a crawled database and render the weekly report the app writes."""
    tmp = tempfile.TemporaryDirectory(prefix="ol-placeholder-")
    db_path = os.path.join(tmp.name, "media.db")
    app_db = os.path.join(tmp.name, "app.db")
    conn = sqlite3.connect(db_path)
    try:
        if notes:
            _insert(conn, "xhs_note", list(notes))
        if comments:
            _insert(conn, "xhs_note_comment", list(comments))
        for table, rows in (extra_tables or {}).items():
            _insert(conn, table, list(rows))
        conn.commit()
    finally:
        conn.close()
    package = build_report_package(
        db_path,
        WINDOW_START,
        WINDOW_END,
        [KEYWORD],
        list(platforms),
        _stub_analyzer,
        app_db_path=app_db,
        brand=KEYWORD,
        title="示例科技舆情监测周报",
    )
    rendered = render_local_report(
        package,
        title="示例科技舆情监测周报",
        narrative=narrative,
    )
    rendered["_package"] = package
    tmp.cleanup()
    return rendered


class WeeklyReportPlaceholderTests(unittest.TestCase):
    def assertChartStaysInScript(self, html: str) -> None:
        inside, outside = _chart_copies(html)
        self.assertEqual(inside, 1, "chart library was not inlined into its script element")
        self.assertEqual(
            outside,
            0,
            f"chart library was copied into the report body ({outside} extra copies)",
        )

    def assertWeeklyReport(self, rendered: Dict[str, Any], readable: str) -> None:
        self.assertEqual(rendered["engine"], "poc", rendered.get("poc_error"))
        html = rendered["html"]
        self.assertIn('id="section-suggestions"', html)
        self.assertIn(readable, html, "ranked post did not reach the rendered report")
        self.assertChartStaysInScript(html)

    def test_negative_title_is_not_replaced_with_the_chart_library(self) -> None:
        rendered = _render_notes(
            [
                _xhs("neg-title", CHART_PLACEHOLDER, f"{KEYWORD} 投诉 服务很差 MARKER_NEG_TITLE", likes="30"),
                _xhs("neg-later", "MARKER_LATER_NEGATIVE", f"{KEYWORD} 投诉 另一条负面", likes="1"),
            ]
        )
        titles = [event["title"] for event in rendered["_package"]["stats"]["negative_events"]]
        self.assertIn(CHART_PLACEHOLDER, titles)
        self.assertIn("MARKER_LATER_NEGATIVE", titles)
        self.assertWeeklyReport(rendered, "MARKER_LATER_NEGATIVE")

    def test_negative_opening_text_is_not_replaced_with_the_chart_library(self) -> None:
        rendered = _render_notes(
            [
                _xhs(
                    "neg-open",
                    "MARKER_OPENING_TITLE",
                    f"{CHART_PLACEHOLDER} {KEYWORD} 投诉 开篇就很差",
                )
            ]
        )
        event = rendered["_package"]["stats"]["negative_events"][0]
        self.assertEqual(event["title"], "MARKER_OPENING_TITLE")
        self.assertIn(CHART_PLACEHOLDER, event["summary"])
        self.assertWeeklyReport(rendered, "MARKER_OPENING_TITLE")

    def test_positive_title_is_not_replaced_with_the_chart_library(self) -> None:
        rendered = _render_notes(
            [_xhs("pos-title", CHART_PLACEHOLDER, f"{KEYWORD} 获奖 优秀 MARKER_POS_TITLE")]
        )
        titles = [event["title"] for event in rendered["_package"]["stats"]["positive_events"]]
        self.assertIn(CHART_PLACEHOLDER, titles)
        self.assertWeeklyReport(rendered, "MARKER_POS_TITLE")

    def test_positive_opening_text_is_not_replaced_with_the_chart_library(self) -> None:
        rendered = _render_notes(
            [
                _xhs(
                    "pos-open",
                    "MARKER_POS_OPENING",
                    f"{CHART_PLACEHOLDER} {KEYWORD} 获奖 开篇表扬",
                )
            ]
        )
        event = rendered["_package"]["stats"]["positive_events"][0]
        self.assertNotIn(CHART_PLACEHOLDER, event["title"])
        self.assertIn(CHART_PLACEHOLDER, event["summary"])
        self.assertWeeklyReport(rendered, "MARKER_POS_OPENING")

    def test_weibo_body_used_as_the_event_title_is_not_expanded(self) -> None:
        row = _common()
        row.update(
            {
                "note_id": "wb-body",
                "content": f"{CHART_PLACEHOLDER} {KEYWORD} 投诉 WBMARK",
                "create_time": PUBLISHED,
                "create_date_time": PUBLISHED,
                "liked_count": "8",
                "comments_count": "0",
                "shared_count": "0",
                "note_url": "https://example.test/wb-body",
            }
        )
        rendered = _render_notes([], extra_tables={"weibo_note": [row]}, platforms=("wb",))
        event = rendered["_package"]["stats"]["negative_events"][0]
        self.assertIn(CHART_PLACEHOLDER, event["title"])
        self.assertIn("WBMARK", event["summary"])
        self.assertWeeklyReport(rendered, "WBMARK")

    def test_suggestions_placeholder_in_a_title_is_not_expanded(self) -> None:
        rendered = _render_notes(
            [_xhs("sug-title", "{{SUGGESTIONS}}", f"{KEYWORD} 投诉 MARKER_SUGGESTIONS")]
        )
        titles = [event["title"] for event in rendered["_package"]["stats"]["negative_events"]]
        self.assertIn("{{SUGGESTIONS}}", titles)
        self.assertWeeklyReport(rendered, "MARKER_SUGGESTIONS")
        self.assertEqual(
            rendered["html"].count(SUGGESTION_PHRASE),
            1,
            "suggestion text was substituted into the crawled title",
        )

    def test_narrative_text_is_not_scanned_for_later_placeholders(self) -> None:
        rendered = _render_notes(
            [_xhs("narr-safe", "MARKER_NARRATIVE", f"{KEYWORD} 投诉 普通标题")],
            narrative={
                "overview": {"one_line_summary": f"综述 {CHART_PLACEHOLDER} 结束"},
                "trend": {"insight": "趋势正常"},
                "sentiment": {"insight": "情感正常"},
                "platform": {"insight": "平台正常"},
                "negative": {"intro": "负面说明", "note": "状态说明", "measures": []},
                "positive": {"intro": "正面说明", "follow_ups": []},
                "suggestions": ["保持监测"],
                "suggestions_note": "以上为本地说明",
            },
        )
        self.assertWeeklyReport(rendered, "MARKER_NARRATIVE")
        self.assertIn("综述", rendered["html"])

    def test_neutral_post_is_not_placed_in_an_event_table(self) -> None:
        rendered = _render_notes(
            [_xhs("neutral", CHART_PLACEHOLDER, f"{KEYWORD} 今日天气 MARKER_NEUTRAL")]
        )
        self.assertEqual(rendered["_package"]["stats"]["negative_events"], [])
        self.assertEqual(rendered["_package"]["stats"]["positive_events"], [])
        self.assertEqual(rendered["engine"], "poc", rendered.get("poc_error"))
        self.assertChartStaysInScript(rendered["html"])

    def test_comment_body_is_not_substituted_into_the_report(self) -> None:
        note = _xhs("parent", "MARKER_COMMENT_PARENT", f"{KEYWORD} 投诉 正文没有占位符")
        comment = _common()
        comment.update(
            {
                "comment_id": "c-ph",
                "note_id": "parent",
                "content": f"{CHART_PLACEHOLDER} 这条评论投诉",
                "create_time": PUBLISHED,
                "like_count": "1",
            }
        )
        rendered = _render_notes([note], comments=[comment])
        self.assertWeeklyReport(rendered, "MARKER_COMMENT_PARENT")
        self.assertNotIn(CHART_PLACEHOLDER, rendered["html"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

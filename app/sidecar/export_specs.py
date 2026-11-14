# -*- coding: utf-8 -*-
"""
export_specs.py — Excel 导出列规格表（P1-2：声明式列规格，单一事实来源）

`exporter.py` 不再逐张表手写「列名 + append」，而是**读规格表**决定：
  1. sheet 顺序 / sheet 名 / 表名 / 平台 code；
  2. 每个数据 sheet 的列顺序与输出列名（`creator_hash → user_id` 之类的改名写在规格里）；
  3. 导出前的「规格列 vs SQLite 实际列」一致性自检（缺列 / 未声明列）。

改造要点（与改造前逐字等价）：
  - 规格表里的列顺序 = MediaCrawler 当前 ORM 的列顺序，因此导出的表头与
    `output_columns(db_columns)` 派生结果完全一致（`test_exporter.py` 有等价断言）；
  - 每个数据 sheet 末尾固定追加三个计算列：
    `filtered_reason`（相关性标记）、`sentiment` / `sentiment_reason`（情感缓存值）；
  - 平台中文名复用 `report_data.PLATFORM_LABELS`，新增平台只需在 `PLATFORM_SHEETS`
    与对应列规格里各加一行（sheet 名 = 平台名 / 平台名+评论）。

列的三种形态：
  - `Col(label, field)`              —— 普通列，输出列名 = 数据库列名；
  - `Col(label, field, fallbacks=)`  —— 兼容旧库的列（如旧库只有 `user_id`，没有 `creator_hash`）；
  - `Col(label)`                     —— 计算列（`field=""`），值由 exporter 计算，见 `COMPUTED_KEYS`。

`required=True` 表示**结构列**：数据库里缺它就无法正确导出（内容 ID / 评论父 ID），
导出时抛 `EXPORT_SCHEMA_MISMATCH`；展示类列缺失时按旧库兼容处理（跳过该列并记录告警）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

# 平台中文名唯一来源（`app/sidecar/report_data.py` 重新导出的同一份表）
from report_data import PLATFORM_LABELS

# ---------------------------------------------------------------------------
# 计算列（不来自 MediaCrawler 表，由 exporter 在导出前算好写进 row 的临时键）
# ---------------------------------------------------------------------------
FILTERED_REASON_COLUMN = "filtered_reason"
SENTIMENT_COLUMN = "sentiment"
SENTIMENT_REASON_COLUMN = "sentiment_reason"

# 输出列名 → row 中的内部键（exporter 注解行数据时使用）
COMPUTED_KEYS: Dict[str, str] = {
    FILTERED_REASON_COLUMN: "__computed_filtered_reason__",
    SENTIMENT_COLUMN: "__sentiment_cn__",
    SENTIMENT_REASON_COLUMN: "__sentiment_reason__",
}

OVERVIEW_SHEET = "概览"


@dataclass(frozen=True)
class Col:
    """一个输出列：`label` 为 Excel 列名，`field` 为数据库列名（计算列为空）。"""

    label: str
    field: str = ""
    fallbacks: Tuple[str, ...] = ()
    required: bool = False

    @property
    def is_computed(self) -> bool:
        return not self.field

    def candidate_fields(self) -> Tuple[str, ...]:
        """数据库里可能的源列（主列 + 旧库兼容候选，按优先级）。"""
        if self.is_computed:
            return ()
        return (self.field,) + tuple(self.fallbacks)


@dataclass(frozen=True)
class SheetSpec:
    """一张 sheet 的完整声明（顺序即工作簿顺序）。"""

    name: str
    columns: Tuple[Col, ...]
    table: str = ""
    kind: str = "overview"          # overview | content | comment
    platform: str = ""
    id_col: str = ""                # kind=content：内容 ID 列
    parent_col: str = ""            # kind=comment：父内容 ID 列

    def as_legacy_dict(self) -> Dict[str, str]:
        """兼容旧 `SHEETS: List[Dict[str, str]]` 结构（外部与测试仍在用）。"""
        item: Dict[str, str] = {"name": self.name, "table": self.table, "kind": self.kind}
        if self.kind == "content":
            item["id_col"] = self.id_col
        if self.kind == "comment":
            item["parent_col"] = self.parent_col
        item["platform"] = self.platform
        return item


@dataclass(frozen=True)
class PlatformSheet:
    """一个平台的表映射（内容表 + 评论表）。"""

    code: str
    content_table: str
    content_id_col: str
    comment_table: str
    parent_col: str

    @property
    def label(self) -> str:
        return PLATFORM_LABELS.get(self.code, self.code)

    @property
    def content_sheet(self) -> str:
        return self.label

    @property
    def comment_sheet(self) -> str:
        return f"{self.label}评论"


# 所有数据 sheet 末尾的统一计算列（顺序即 Excel 列顺序，契约 §3）
DERIVED_COLUMNS: Tuple[Col, ...] = tuple(Col(label) for label in COMPUTED_KEYS)
# 概览 sheet 的两列语义（导出器实际布局另带标题行/统计区块，见 exporter._write_overview）
OVERVIEW_COLUMNS: Tuple[Col, ...] = (Col("指标", "metric"), Col("数值", "value"))


# ---------------------------------------------------------------------------
# 平台清单（顺序即 Excel sheet 顺序，契约 §3）—— 新增平台只改这里 + 列规格
# ---------------------------------------------------------------------------
PLATFORM_SHEETS: Tuple[PlatformSheet, ...] = (
    PlatformSheet("xhs", "xhs_note", "note_id", "xhs_note_comment", "note_id"),
    PlatformSheet("dy", "douyin_aweme", "aweme_id", "douyin_aweme_comment", "aweme_id"),
    PlatformSheet("ks", "kuaishou_video", "video_id", "kuaishou_video_comment", "video_id"),
    PlatformSheet("bili", "bilibili_video", "video_id", "bilibili_video_comment", "video_id"),
    PlatformSheet("wb", "weibo_note", "note_id", "weibo_note_comment", "note_id"),
    PlatformSheet("tieba", "tieba_note", "note_id", "tieba_comment", "note_id"),
    PlatformSheet("zhihu", "zhihu_content", "content_id", "zhihu_comment", "content_id"),
)


# ---------------------------------------------------------------------------
# 列规格：与 MediaCrawler/database/models.py 当前 ORM 的列顺序逐字一致
# ---------------------------------------------------------------------------
def _columns(*fields: str, required: Tuple[str, ...] = ()) -> Tuple[Col, ...]:
    """
    按 ORM 字段顺序生成列规格（末尾自动追加三列计算列）。

    - `creator_hash` 输出为 `user_id`（原始 ID，不脱敏）；旧库只有 `user_id` 时回退命中；
    - `required` 里的字段标记为结构列（缺列 → EXPORT_SCHEMA_MISMATCH）。
    """
    cols: List[Col] = []
    for field in fields:
        if field == "creator_hash":
            cols.append(Col("user_id", "creator_hash", fallbacks=("user_id",), required=field in required))
        else:
            cols.append(Col(field, field, required=field in required))
    return tuple(cols) + DERIVED_COLUMNS


CONTENT_COLUMNS: Dict[str, Tuple[Col, ...]] = {
    "xhs_note": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "type", "title", "desc",
        "video_url", "time", "last_update_time", "liked_count", "collected_count", "comment_count",
        "share_count", "image_list", "tag_list", "note_url", "source_keyword", "xsec_token",
        required=("note_id",),
    ),
    "douyin_aweme": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "aweme_id", "aweme_type", "title",
        "desc", "create_time", "liked_count", "comment_count", "share_count", "collected_count",
        "aweme_url", "cover_url", "video_download_url", "music_download_url", "note_download_url",
        "source_keyword",
        required=("aweme_id",),
    ),
    "kuaishou_video": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "video_id", "video_type", "title",
        "desc", "create_time", "liked_count", "viewd_count", "video_url", "video_cover_url",
        "video_play_url", "source_keyword",
        required=("video_id",),
    ),
    "bilibili_video": _columns(
        "id", "video_id", "video_url", "creator_hash", "nickname", "liked_count", "add_ts",
        "last_modify_ts", "video_type", "title", "desc", "create_time", "disliked_count",
        "video_play_count", "video_favorite_count", "video_share_count", "video_coin_count",
        "video_danmaku", "video_comment", "video_cover_url", "source_keyword",
        required=("video_id",),
    ),
    "weibo_note": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "note_id", "content", "create_time",
        "create_date_time", "liked_count", "comments_count", "shared_count", "note_url", "source_keyword",
        required=("note_id",),
    ),
    "tieba_note": _columns(
        "id", "note_id", "title", "desc", "note_url", "publish_time", "creator_hash", "user_nickname",
        "tieba_id", "tieba_name", "tieba_link", "total_replay_num", "total_replay_page", "add_ts",
        "last_modify_ts", "source_keyword",
        required=("note_id",),
    ),
    "zhihu_content": _columns(
        "id", "content_id", "content_type", "content_text", "content_url", "question_id", "title",
        "desc", "created_time", "updated_time", "voteup_count", "comment_count", "source_keyword",
        "creator_hash", "user_nickname", "add_ts", "last_modify_ts",
        required=("content_id",),
    ),
}

COMMENT_COLUMNS: Dict[str, Tuple[Col, ...]] = {
    "xhs_note_comment": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "create_time",
        "note_id", "content", "sub_comment_count", "pictures", "parent_comment_id", "like_count",
        required=("note_id",),
    ),
    "douyin_aweme_comment": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "aweme_id",
        "content", "create_time", "sub_comment_count", "parent_comment_id", "like_count", "pictures",
        required=("aweme_id",),
    ),
    "kuaishou_video_comment": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "video_id",
        "content", "create_time", "sub_comment_count",
        required=("video_id",),
    ),
    "bilibili_video_comment": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "video_id",
        "content", "create_time", "sub_comment_count", "parent_comment_id", "like_count",
        required=("video_id",),
    ),
    "weibo_note_comment": _columns(
        "id", "creator_hash", "nickname", "add_ts", "last_modify_ts", "comment_id", "note_id",
        "content", "create_time", "create_date_time", "comment_like_count", "sub_comment_count",
        "parent_comment_id",
        required=("note_id",),
    ),
    "tieba_comment": _columns(
        "id", "comment_id", "parent_comment_id", "content", "creator_hash", "user_nickname", "tieba_id",
        "tieba_name", "tieba_link", "publish_time", "sub_comment_count", "note_id", "note_url", "add_ts",
        "last_modify_ts",
        required=("note_id",),
    ),
    "zhihu_comment": _columns(
        "id", "comment_id", "parent_comment_id", "content", "publish_time", "sub_comment_count",
        "like_count", "dislike_count", "content_id", "content_type", "creator_hash", "user_nickname",
        "add_ts", "last_modify_ts",
        required=("content_id",),
    ),
}


def build_sheet_specs() -> Tuple[SheetSpec, ...]:
    """按 `PLATFORM_SHEETS` 顺序展开成完整 sheet 规格（概览在最前）。"""
    specs: List[SheetSpec] = [
        SheetSpec(OVERVIEW_SHEET, OVERVIEW_COLUMNS, kind="overview"),
    ]
    for platform in PLATFORM_SHEETS:
        specs.append(
            SheetSpec(
                platform.content_sheet,
                CONTENT_COLUMNS[platform.content_table],
                table=platform.content_table,
                kind="content",
                platform=platform.code,
                id_col=platform.content_id_col,
            )
        )
        specs.append(
            SheetSpec(
                platform.comment_sheet,
                COMMENT_COLUMNS[platform.comment_table],
                table=platform.comment_table,
                kind="comment",
                platform=platform.code,
                parent_col=platform.parent_col,
            )
        )
    return tuple(specs)


SHEET_SPECS: Tuple[SheetSpec, ...] = build_sheet_specs()
_DATA_SHEET_SPECS: Tuple[SheetSpec, ...] = tuple(spec for spec in SHEET_SPECS if spec.kind != "overview")

# 兼容旧结构（`exporter.SHEETS` / 测试仍在按 dict 读取）
SHEETS: List[Dict[str, str]] = [spec.as_legacy_dict() for spec in _DATA_SHEET_SPECS]


__all__ = [
    "COMPUTED_KEYS",
    "COMMENT_COLUMNS",
    "CONTENT_COLUMNS",
    "Col",
    "DERIVED_COLUMNS",
    "FILTERED_REASON_COLUMN",
    "OVERVIEW_COLUMNS",
    "OVERVIEW_SHEET",
    "PLATFORM_SHEETS",
    "SENTIMENT_COLUMN",
    "SENTIMENT_REASON_COLUMN",
    "SHEETS",
    "SHEET_SPECS",
    "SheetSpec",
    "PlatformSheet",
    "build_sheet_specs",
]

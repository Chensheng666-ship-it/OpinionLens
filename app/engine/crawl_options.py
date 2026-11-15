# -*- coding: utf-8 -*-
"""采集高级参数：单一来源的规格表 + 校验（engine 与 sidecar 共用，只依赖标准库）。

为什么单独成模块
----------------
同一组参数要在三处保持一致：sidecar 请求校验（``POST /api/runs``）、engine CLI
（``--options-json``）、``apply_config`` 注入 MediaCrawler ``config``。字段、默认值、
取值范围只在这里写一遍，避免「前端能填、后端截断、引擎又用默认值」的静默不一致。

约定
----
* ``normalize_options(raw)`` 只接受已知键；未知键 / 越界 / 类型错误 -> ``ValueError``（调用方转 400）。
* 返回值**只含用户显式给出的键**（缺省键由 ``effective_options`` 补默认值），这样旧任务的
  ``params`` 不会因为新增字段而变化，续采时也不会被新默认值悄悄改掉。
* 默认值 = 改造前写死的值（``MAX_CONCURRENCY_NUM=1``、``CRAWLER_MAX_SLEEP_SEC=2`` 等），
  不传 options 时行为与旧版逐字节一致。
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

# 平台并行（sidecar 层）：同时运行的平台数上限。各平台站点/账号互相独立，并行不增加单账号风险。
DEFAULT_PLATFORM_PARALLEL = 3
MAX_PLATFORM_PARALLEL = 7

# 规格：key -> (类型, 默认值, 下限, 上限, 中文说明)
# 类型：int / float / bool / choice（choice 的「下限」位放可选值元组）
OPTION_SPECS: Dict[str, Tuple[str, Any, Any, Any, str]] = {
    "request_interval": ("float", 2.0, 0.5, 30.0, "请求间隔（秒）：每页/每条之间的等待，越小越快、风控风险越高"),
    "concurrency": ("int", 1, 1, 5, "单平台并发数：同时抓取详情/评论的条数"),
    "sub_comments": ("bool", False, None, None, "是否抓取二级评论（楼中楼）"),
    "start_page": ("int", 1, 1, 50, "搜索起始页"),
    "platform_parallel": ("int", DEFAULT_PLATFORM_PARALLEL, 1, MAX_PLATFORM_PARALLEL, "同时采集的平台数"),
    "xhs_sort": ("choice", "popularity_descending", ("general", "popularity_descending", "time_descending"), None, "小红书排序：综合 / 最热 / 最新"),
    "dy_publish_time": ("choice", 0, (0, 1, 7, 180), None, "抖音发布时间：不限 / 1 天内 / 1 周内 / 半年内"),
    "wb_search_type": ("choice", "default", ("default", "real_time", "popular", "video"), None, "微博搜索类型：综合 / 实时 / 热门 / 视频"),
}

# 只作用于 sidecar 编排（不下发引擎）的键
SIDECAR_ONLY_KEYS = frozenset({"platform_parallel"})

_TRUE = ("1", "true", "yes", "y", "on", "t")
_FALSE = ("0", "false", "no", "n", "off", "f", "")


def _coerce_bool(key: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{key} 必须是布尔值：{value!r}")


def _coerce_number(key: str, kind: str, value: Any, low: Any, high: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError(f"{key} 必须是数字：{value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} 必须是数字：{value!r}") from exc
    if number != number:  # NaN
        raise ValueError(f"{key} 必须是数字：{value!r}")
    if kind == "int":
        if number != int(number):
            raise ValueError(f"{key} 必须是整数：{value!r}")
        number = int(number)
    if number < low or number > high:
        raise ValueError(f"{key} 超出范围（{low}–{high}）：{value!r}")
    return number


def _coerce_choice(key: str, value: Any, choices: Tuple[Any, ...]) -> Any:
    for choice in choices:
        if isinstance(choice, int) and not isinstance(value, bool):
            try:
                if int(str(value).strip()) == choice:
                    return choice
            except (TypeError, ValueError):
                continue
        elif str(value).strip() == str(choice):
            return choice
    shown = " / ".join(str(item) for item in choices)
    raise ValueError(f"{key} 只支持 {shown}：{value!r}")


def normalize_options(raw: Any) -> Dict[str, Any]:
    """校验并规范化用户给出的高级参数；只返回显式给出的键（``None`` 值视为未给出）。"""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("options 必须是对象（JSON object）")
    unknown = sorted(str(key) for key in raw if key not in OPTION_SPECS)
    if unknown:
        raise ValueError(f"options 含不支持的参数：{', '.join(unknown)}（可选：{', '.join(OPTION_SPECS)}）")
    result: Dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            continue
        kind, _default, low, high, _label = OPTION_SPECS[key]
        if kind == "bool":
            result[key] = _coerce_bool(key, value)
        elif kind == "choice":
            result[key] = _coerce_choice(key, value, low)
        else:
            result[key] = _coerce_number(key, kind, value, low, high)
    return result


def effective_options(options: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """补齐默认值后的完整参数（用于日志、apply_config）。"""
    merged = {key: spec[1] for key, spec in OPTION_SPECS.items()}
    merged.update(normalize_options(options or {}))
    return merged


def engine_options(options: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """下发给引擎的子集（去掉 sidecar 专用键）；只含显式给出的键。"""
    return {
        key: value
        for key, value in normalize_options(options or {}).items()
        if key not in SIDECAR_ONLY_KEYS
    }


def platform_parallel(options: Optional[Mapping[str, Any]] = None) -> int:
    return int(effective_options(options)["platform_parallel"])


def option_catalog() -> List[Dict[str, Any]]:
    """给前端/文档用的参数目录（键、类型、默认值、范围、说明）。"""
    items: List[Dict[str, Any]] = []
    for key, (kind, default, low, high, label) in OPTION_SPECS.items():
        item: Dict[str, Any] = {"key": key, "type": kind, "default": default, "label": label}
        if kind == "choice":
            item["choices"] = list(low)
        elif kind in ("int", "float"):
            item["min"] = low
            item["max"] = high
        items.append(item)
    return items


__all__ = [
    "DEFAULT_PLATFORM_PARALLEL",
    "MAX_PLATFORM_PARALLEL",
    "OPTION_SPECS",
    "SIDECAR_ONLY_KEYS",
    "effective_options",
    "engine_options",
    "normalize_options",
    "option_catalog",
    "platform_parallel",
]

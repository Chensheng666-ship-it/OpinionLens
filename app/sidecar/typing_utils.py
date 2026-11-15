# -*- coding: utf-8 -*-
"""typing_utils.py — 把「动态 JSON 值」收窄成具体容器的极小工具（只服务类型检查）。

背景：sidecar 里遍地是这种写法——

    x = obj.get("k") if isinstance(obj.get("k"), dict) else {}

`dict.get` 的静态类型是 `Optional[Any]`，而 mypy 不会对**方法调用表达式**做 isinstance
收窄，于是后面每次 `x.get(...)` / `for i in x` / `len(x)` 都会被报 union-attr / arg-type。
把收窄动作固定在这个文件里，调用点只剩一句 `as_dict(obj.get("k"))`：

* 参数类型是 `Any`（收 `Optional[Any]` 不报错），返回 `Dict[str, Any]` / `List[Any]`；
* 运行期与原来的三元表达式**逐字节等价**：是 dict/list 就原样返回同一个对象，
  否则给一个新建的空容器（不会修改入参，也不会因为 None 抛异常）。

字段形状本身由 `app/CONTRACT.md` 保证（事件/数据包字段名按契约），这里只负责
「把它当 dict / list 用」这件事在类型上是安全的，不做任何校验或转换。
"""

from __future__ import annotations

from typing import Any, Dict, List


def as_dict(value: Any) -> Dict[str, Any]:
    """value 是 dict → 原样返回（同一对象）；否则返回新的空 dict。"""
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> List[Any]:
    """value 是 list → 原样返回（同一对象）；否则返回新的空 list。"""
    return value if isinstance(value, list) else []


__all__ = ["as_dict", "as_list"]

# -*- coding: utf-8 -*-
"""按文件路径加载 ``engine/crawl_options.py``（采集高级参数的单一来源）。

sidecar 与 engine 在源码与安装包里都是同级目录（``app/sidecar`` / ``app/engine``，
``resources/sidecar`` / ``resources/engine``）。这里只按路径导入这一个纯标准库模块，
**不把 engine 目录加入 sys.path**，避免 ``patches`` 等名字与 sidecar 模块互相遮蔽。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_MODULE_NAME = "opinionlens_crawl_options"
_PATH = Path(__file__).resolve().parent.parent / "engine" / "crawl_options.py"


def load() -> ModuleType:
    cached = sys.modules.get(_MODULE_NAME)
    if cached is not None:
        return cached
    if not _PATH.is_file():
        raise ImportError(f"找不到采集参数规格文件：{_PATH}")
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载采集参数规格文件：{_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


crawl_options = load()

__all__ = ["crawl_options", "load"]

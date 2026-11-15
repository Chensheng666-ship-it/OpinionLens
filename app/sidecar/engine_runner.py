# -*- coding: utf-8 -*-
"""Engine 门面（M2）：实现已拆到 ``app/sidecar/runner/*``，这里只做 re-export。

对外契约不变：``main.py`` / ``account_manager.py`` / ``selftest.py`` / ``api_*`` /
``report_manager`` 以及 ``tests/python/test_*.py`` 仍从 ``engine_runner`` 导入同一批符号
（见 ``runner.__all__``，由 test_run_manager 逐个 getattr 守护）。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent
if str(_SELF_DIR) not in sys.path:
    sys.path.insert(0, str(_SELF_DIR))

from runner import *  # noqa: E402,F401,F403
from runner import __all__ as _RUNNER_ALL  # noqa: E402

__all__ = list(_RUNNER_ALL)

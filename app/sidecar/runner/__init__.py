# -*- coding: utf-8 -*-
"""runner 子包：engine_runner 的实现模块（M2 拆分）。

依赖方向（无环）：config -> process -> guard/login -> run -> service。
``__all__`` 即 ``engine_runner`` 门面向外转发的完整符号清单。
"""

from __future__ import annotations

from . import config, guard, login, process, run, service
from .config import SIDECAR_DIR
from .config import APP_DIR
from .config import PROJECT_ROOT
from .config import PLATFORMS
from .config import PLATFORM_LABELS
from .config import PLATFORM_IDS
from .config import DEFAULT_LOGIN_TIMEOUT
from .config import _truthy
from .config import _env_str
from .config import _to_int
from .config import ServiceError
from .config import EngineStartError
from .config import SidecarConfig
from .process import EVENT_PREFIX
from .process import LOG_TAIL_MAX
from .process import _CREATE_NO_WINDOW
from .process import _RE_CREATE_NO_WINDOW
from .process import _ERROR_HINTS
from .process import _WARN_HINTS
from .process import classify_log_level
from .process import _kill_process_tree
from .process import _SMS_HINTS
from .process import _SLIDER_HINTS
from .process import _CAPTCHA_HINTS
from .process import infer_need_manual_kind
from .process import default_need_manual_message
from .process import _csv_process_row
from .process import list_browser_processes
from .process import kill_browser_processes_by_marker
from .process import LogSink
from .process import read_log_file_tail
from .process import EngineProcess
from .process import EngineRunner
from .guard import BUSY_GLOBAL_MESSAGE
from .guard import BUSY_PLATFORM_TEST_MESSAGE
from .guard import BUSY_PLATFORM_LOGIN_MESSAGE
from .guard import BUSY_ACCOUNT_MESSAGE
from .guard import _BUSY_MESSAGE
from .guard import ActivityGuard
from .login import LOGIN_TERMINAL_STATUSES
from .login import _ENGINE_LOGIN_STATUS_MAP
from .process import NEED_MANUAL_KINDS
from .login import LOGIN_WATCHDOG_GRACE
from .login import LoginSession
from .login import LoginManager
from .run import RUN_STATUS_RUNNING
from .run import RUN_STATUS_DONE
from .run import RUN_STATUS_FAILED
from .run import RUN_STATUS_CANCELLED
from .run import _calc_progress
from .run import RunRuntime
from .run import _is_retryable_crawl_error
from .run import RunManager
from .run import _run_to_api
from .run import _run_to_summary
from .run import CrawlHandle
from .run import CrawlBackend
from .run import EngineProcessBackend
from .run import FakeProc
from .run import FakeBackend
from .service import SIDECAR_VERSION
from .service import load_exporter
from .service import load_run_feed
from .service import EngineService
from .config import log

__all__ = [
    "SIDECAR_DIR",
    "APP_DIR",
    "PROJECT_ROOT",
    "PLATFORMS",
    "PLATFORM_LABELS",
    "PLATFORM_IDS",
    "DEFAULT_LOGIN_TIMEOUT",
    "_truthy",
    "_env_str",
    "_to_int",
    "ServiceError",
    "EngineStartError",
    "SidecarConfig",
    "EVENT_PREFIX",
    "LOG_TAIL_MAX",
    "_CREATE_NO_WINDOW",
    "_RE_CREATE_NO_WINDOW",
    "_ERROR_HINTS",
    "_WARN_HINTS",
    "classify_log_level",
    "_kill_process_tree",
    "_SMS_HINTS",
    "_SLIDER_HINTS",
    "_CAPTCHA_HINTS",
    "infer_need_manual_kind",
    "default_need_manual_message",
    "_csv_process_row",
    "list_browser_processes",
    "kill_browser_processes_by_marker",
    "LogSink",
    "read_log_file_tail",
    "EngineProcess",
    "EngineRunner",
    "BUSY_GLOBAL_MESSAGE",
    "BUSY_PLATFORM_TEST_MESSAGE",
    "BUSY_PLATFORM_LOGIN_MESSAGE",
    "BUSY_ACCOUNT_MESSAGE",
    "_BUSY_MESSAGE",
    "ActivityGuard",
    "LOGIN_TERMINAL_STATUSES",
    "_ENGINE_LOGIN_STATUS_MAP",
    "NEED_MANUAL_KINDS",
    "LOGIN_WATCHDOG_GRACE",
    "LoginSession",
    "LoginManager",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_DONE",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_CANCELLED",
    "_calc_progress",
    "RunRuntime",
    "_is_retryable_crawl_error",
    "RunManager",
    "_run_to_api",
    "_run_to_summary",
    "CrawlHandle",
    "CrawlBackend",
    "EngineProcessBackend",
    "FakeProc",
    "FakeBackend",
    "SIDECAR_VERSION",
    "load_exporter",
    "load_run_feed",
    "EngineService",
    "log",
]

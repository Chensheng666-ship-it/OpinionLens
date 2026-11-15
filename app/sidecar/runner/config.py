# -*- coding: utf-8 -*-
"""sidecar 配置与基础错误对象（M2 从 engine_runner.py 纯搬迁，行为不变）。"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("sidecar.engine")  # 沿用旧 logger 名：日志输出与拆分前逐字节一致


SIDECAR_DIR = Path(__file__).resolve().parent.parent  # runner/config.py -> app/sidecar（与拆分前一致）
APP_DIR = SIDECAR_DIR.parent
PROJECT_ROOT = APP_DIR.parent

PLATFORMS: List[Tuple[str, str]] = [
    ("xhs", "小红书"),
    ("dy", "抖音"),
    ("ks", "快手"),
    ("bili", "B站"),
    ("wb", "微博"),
    ("tieba", "贴吧"),
    ("zhihu", "知乎"),
]
PLATFORM_LABELS: Dict[str, str] = dict(PLATFORMS)
PLATFORM_IDS: List[str] = [item[0] for item in PLATFORMS]

DEFAULT_LOGIN_TIMEOUT = 420

def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return str(value).strip() if value is not None else default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default

class ServiceError(Exception):
    """可映射为 HTTP 错误体 ``{"error":{"code","message"}}`` 的业务异常。"""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class EngineStartError(Exception):
    """引擎进程无法启动（入口缺失、python 不存在等）。"""

@dataclass
class SidecarConfig:
    """sidecar 运行配置（所有路径均解析为绝对路径）。"""

    data_dir: Path
    mc_root: Path
    python: Path
    engine_entry: Path
    fake_engine: bool = False
    login_timeout: int = DEFAULT_LOGIN_TIMEOUT
    login_headless: bool = False
    test_timeout: float = 90.0
    crawl_timeout: float = 1800.0

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def qr_dir(self) -> Path:
        return self.data_dir / "qr"

    @property
    def crawler_dir(self) -> Path:
        return self.data_dir / "crawler"

    @property
    def browser_data_dir(self) -> Path:
        return self.data_dir / "browser_data"

    @property
    def app_db(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def media_db(self) -> Path:
        return self.crawler_dir / "media_crawler.db"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.logs_dir,
            self.exports_dir,
            self.qr_dir,
            self.crawler_dir,
            self.browser_data_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def platform_profile_template(self, platform: str, account_id: str = "") -> str:
        """账号 profile 目录的「模板」或旧单账号实际路径（多账号号池契约 §6.2）。

        - ``account_id`` 为空 → 旧路径 ``<data-dir>/browser_data/<platform>_user_data_dir``
          （已解析为具体平台，无 ``%s``，用于 kill_orphan/list/delete 等旧调用）；
        - 指定账号 → ``<data-dir>/browser_data/%s/<account_id>/user_data_dir``，
          ``%s`` 由 MediaCrawler 替换为平台名（7 平台 ``core.py`` 统一格式）。
        """
        account_id = str(account_id or "").strip()
        if account_id:
            return str(self.browser_data_dir / "%s" / account_id / "user_data_dir")
        return str(self.browser_data_dir / f"{platform}_user_data_dir")

    def account_profile_template(
        self, platform: str, account_id: str, profile_layout: str = "v2"
    ) -> str:
        """账号 profile 模板（含恰好一个 ``%s``）：legacy 账号沿用旧模板。"""
        if (profile_layout or "v2") == "legacy":
            return str(self.browser_data_dir / "%s_user_data_dir")
        return self.platform_profile_template(platform, account_id)

    def platform_profile_dir(self, platform: str, account_id: str = "") -> Path:
        """账号 profile 实际目录（兼容旧签名；v2 账号自动解析模板里的 ``%s``）。"""
        raw = self.platform_profile_template(platform, account_id)
        return Path(raw % platform) if "%s" in raw else Path(raw)

    @classmethod
    def from_env(
        cls,
        data_dir: str | Path,
        mc_root: str | Path | None = None,
        python: str | Path | None = None,
        engine_entry: str | Path | None = None,
    ) -> "SidecarConfig":
        data_path = Path(data_dir).expanduser().resolve()

        mc_env = _env_str("MC_ROOT")
        if mc_root is not None:
            mc_path = Path(mc_root).expanduser().resolve()
        elif mc_env:
            mc_path = Path(mc_env).expanduser().resolve()
        else:
            mc_path = (PROJECT_ROOT / "MediaCrawler").resolve()

        fake_engine = _truthy(os.environ.get("FAKE_ENGINE"), False)

        if python is not None:
            py_path = Path(python).expanduser().resolve()
        else:
            py_env = _env_str("SIDECAR_PYTHON") or _env_str("MC_PYTHON")
            candidates: List[Path] = []
            if py_env:
                candidates.append(Path(py_env).expanduser().resolve())
            candidates.append(mc_path / ".venv" / "Scripts" / "python.exe")
            candidates.append(mc_path / ".venv" / "bin" / "python")
            py_path = next((p for p in candidates if p.is_file()), Path(sys.executable))

        entry_env = _env_str("SIDECAR_ENGINE_ENTRY")
        if engine_entry is not None:
            entry_path = Path(engine_entry).expanduser().resolve()
        elif entry_env:
            entry_path = Path(entry_env).expanduser().resolve()
        elif fake_engine:
            # 假 engine 替身优先 tests/fixtures（R1 布局）；不存在时回退旧位置（向后兼容旧检出）
            fixtures_entry = (PROJECT_ROOT / "tests" / "fixtures" / "_fake_engine.py").resolve()
            legacy_entry = (SIDECAR_DIR / "_fake_engine.py").resolve()
            entry_path = fixtures_entry if fixtures_entry.is_file() else legacy_entry
        else:
            entry_path = (APP_DIR / "engine" / "entry.py").resolve()

        login_timeout = _to_int(_env_str("SIDECAR_LOGIN_TIMEOUT"), DEFAULT_LOGIN_TIMEOUT) or DEFAULT_LOGIN_TIMEOUT
        # 登录默认「有头」窗口：用户在可见 Edge 窗口完成扫码后的短信/滑块/拼图验证。
        # SIDECAR_LOGIN_HEADLESS=1/true 可强制回无头（自动化/无桌面环境）。
        login_headless_env = _env_str("SIDECAR_LOGIN_HEADLESS")
        login_headless = _truthy(login_headless_env, False) if login_headless_env else False
        test_timeout = float(_env_str("SIDECAR_TEST_TIMEOUT") or 90.0)
        crawl_timeout = float(_env_str("SIDECAR_CRAWL_TIMEOUT") or 1800.0)

        return cls(
            data_dir=data_path,
            mc_root=mc_path,
            python=py_path,
            engine_entry=entry_path,
            fake_engine=fake_engine,
            login_timeout=max(10, login_timeout),
            login_headless=login_headless,
            test_timeout=max(5.0, test_timeout),
            crawl_timeout=max(30.0, crawl_timeout),
        )

    def login_headless_arg(self) -> str:
        """传给 engine 的 ``--headless`` 参数（登录模式）。"""
        return "true" if self.login_headless else "false"

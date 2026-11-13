# -*- coding: utf-8 -*-
"""sidecar 持久化层：sqlite3 标准库实现 ``<data-dir>/app.db``。

只维护契约允许的三张表：

- ``runs``           一次采集任务
- ``run_platforms``  任务内每个平台的执行明细（与 runs 1:N）
- ``account_state``  各平台登录态缓存（以 engine 的 login/test 事件为准）

设计要点
--------
* 单连接 + ``threading.RLock``：sidecar 内多线程（FastAPI 线程池 + 采集编排线程）
  共享同一个 ``StateStore`` 实例；sqlite 连接以 ``check_same_thread=False`` 打开，
  由锁串行化写操作，WAL 模式保证读不阻塞写。
* 重启恢复：``recover_interrupted_runs()`` 在启动时把残留的 running 记录置为
  failed/skipped（契约 §2.3），保证 ``GET /api/runs`` 重启后仍可列出历史。
* 时间戳统一为 int（epoch 秒），与契约事件里的 ``ts`` 口径一致。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    name         TEXT,
    params_json  TEXT NOT NULL,
    keywords_json TEXT NOT NULL,
    created_at   INTEGER NOT NULL,
    started_ts   INTEGER NOT NULL,
    finished_at  INTEGER,
    excel_path   TEXT,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS run_platforms (
    run_id       TEXT NOT NULL,
    platform     TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    status       TEXT NOT NULL,
    progress     INTEGER NOT NULL DEFAULT 0,
    stage        TEXT,
    saved_items  INTEGER NOT NULL DEFAULT 0,
    message      TEXT,
    started_at   INTEGER,
    finished_at  INTEGER,
    PRIMARY KEY (run_id, platform)
);

CREATE TABLE IF NOT EXISTS account_state (
    platform        TEXT PRIMARY KEY,
    logged_in       INTEGER,
    last_checked_at INTEGER,
    profile_exists  INTEGER,
    message         TEXT,
    updated_at      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_run_platforms_status ON run_platforms(run_id, status);

-- ===========================================================================
-- 开源版新增：多账号号池 / 通知推送 / 通用设置 / 模型 API
-- ===========================================================================
CREATE TABLE IF NOT EXISTS accounts (
    account_id       TEXT PRIMARY KEY,
    platform         TEXT NOT NULL,
    alias            TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'need_login',
    login_state      TEXT NOT NULL DEFAULT 'unknown',
    profile_dir      TEXT NOT NULL DEFAULT '',
    profile_layout   TEXT NOT NULL DEFAULT 'v2',
    health_score     INTEGER NOT NULL DEFAULT 100,
    success_count    INTEGER NOT NULL DEFAULT 0,
    fail_count       INTEGER NOT NULL DEFAULT 0,
    consecutive_fail INTEGER NOT NULL DEFAULT 0,
    last_login_at    INTEGER,
    last_checked_at  INTEGER,
    last_used_at     INTEGER,
    cooldown_until   INTEGER,
    risk_level       TEXT NOT NULL DEFAULT 'normal',
    risk_note        TEXT NOT NULL DEFAULT '',
    proxy_enc        TEXT,
    is_default       INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_pick
    ON accounts(platform, status, cooldown_until);

CREATE TABLE IF NOT EXISTS account_quota (
    account_id     TEXT NOT NULL,
    day            TEXT NOT NULL,
    runs           INTEGER NOT NULL DEFAULT 0,
    platform_runs  INTEGER NOT NULL DEFAULT 0,
    saved_items    INTEGER NOT NULL DEFAULT 0,
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (account_id, day)
);

CREATE TABLE IF NOT EXISTS run_accounts (
    run_id       TEXT NOT NULL,
    platform     TEXT NOT NULL,
    attempt      INTEGER NOT NULL DEFAULT 1,
    account_id   TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT 'auto',
    status       TEXT NOT NULL DEFAULT 'running',
    message      TEXT NOT NULL DEFAULT '',
    started_at   INTEGER,
    finished_at  INTEGER,
    PRIMARY KEY (run_id, platform, attempt)
);
CREATE INDEX IF NOT EXISTS idx_run_accounts_account
    ON run_accounts(account_id, started_at DESC);

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL DEFAULT '{}',
    is_secret   INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notify_channels (
    channel_id  TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    secret_enc  TEXT NOT NULL DEFAULT '',
    config_json TEXT NOT NULL DEFAULT '{}',
    quiet_hours_json TEXT NOT NULL DEFAULT '[]',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notify_rules (
    rule_id       TEXT PRIMARY KEY,
    event_type    TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    min_severity  TEXT NOT NULL DEFAULT 'info',
    channel_ids_json TEXT NOT NULL DEFAULT '[]',
    conditions_json  TEXT NOT NULL DEFAULT '{}',
    template      TEXT NOT NULL DEFAULT 'default',
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notify_outbox (
    outbox_id     TEXT PRIMARY KEY,
    dedupe_key    TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    severity      TEXT NOT NULL DEFAULT 'info',
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    channel_ids_json TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'pending',
    attempts      INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts INTEGER,
    last_error    TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    sent_at       INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_notify_outbox_dedupe
    ON notify_outbox(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_notify_outbox_pick
    ON notify_outbox(status, next_attempt_ts);

CREATE TABLE IF NOT EXISTS notify_log (
    log_id      TEXT PRIMARY KEY,
    outbox_id   TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    http_status INTEGER,
    latency_ms  INTEGER,
    error       TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notify_log_time ON notify_log(created_at DESC);

CREATE TABLE IF NOT EXISTS llm_providers (
    provider_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    kind         TEXT NOT NULL,
    base_url     TEXT NOT NULL DEFAULT '',
    model        TEXT NOT NULL DEFAULT '',
    secret_enc   TEXT NOT NULL DEFAULT '',
    extra_json   TEXT NOT NULL DEFAULT '{}',
    enabled      INTEGER NOT NULL DEFAULT 1,
    is_default   INTEGER NOT NULL DEFAULT 0,
    last_test_ok INTEGER,
    last_test_at INTEGER,
    last_test_msg TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
"""

# 平台执行状态（契约 §2.2 枚举）
PLATFORM_STATUSES = ("queued", "running", "done", "failed", "skipped")
# 任务状态（契约 §2.2 枚举）
RUN_STATUSES = ("running", "done", "failed", "cancelled")

_PLATFORM_UPDATE_FIELDS = {
    "status",
    "progress",
    "stage",
    "saved_items",
    "message",
    "started_at",
    "finished_at",
    "account_id",
    "account_attempts",
}


def now_ts() -> int:
    """当前 epoch 秒（与 engine 事件 ts 口径一致）。"""
    return int(time.time())


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class StateStore:
    """app.db 的唯一入口（线程安全）。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._migrate_schema()

    # ------------------------------------------------------------------ 基础
    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            if self._closed:
                raise RuntimeError("StateStore 已关闭")
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            if self._closed:
                raise RuntimeError("StateStore 已关闭")
            return list(self._conn.execute(sql, params).fetchall())

    # -------------------------------------------------- 新功能模块用的公共 API
    def query(self, sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """查询并返回 dict 列表（供账号池/通知/模型 API 模块使用）。"""
        return [dict(row) for row in self._query(sql, params)]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """执行写语句并提交，返回影响行数。"""
        return int(self._execute(sql, params).rowcount or 0)

    def executescript(self, script: str) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("StateStore 已关闭")
            self._conn.executescript(script)
            self._conn.commit()

    def get_setting(self, key: str, default: Any = None) -> Any:
        """读取 app_settings 键值（value_json 反序列化）。"""
        rows = self._query(
            "SELECT value_json, is_secret FROM app_settings WHERE key = ?", (str(key),)
        )
        if not rows:
            return default
        return _loads(rows[0]["value_json"], default)

    def set_setting(self, key: str, value: Any, is_secret: bool = False) -> None:
        """写入 app_settings 键值（is_secret 仅标记用途，加密由调用方完成）。"""
        self._execute(
            "INSERT INTO app_settings(key, value_json, is_secret, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, "
            "is_secret = excluded.is_secret, updated_at = excluded.updated_at",
            (str(key), _dumps(value), int(bool(is_secret)), now_ts()),
        )

    def delete_setting(self, key: str) -> None:
        self._execute("DELETE FROM app_settings WHERE key = ?", (str(key),))

    # ------------------------------------------------------------ 增量迁移
    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        """幂等加列：已存在则跳过，表不存在也跳过。"""
        rows = self._query("PRAGMA table_info(%s)" % table)
        if not rows:
            return
        if any(row["name"] == column for row in rows):
            return
        self._conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, ddl))
        self._conn.commit()

    def _migrate_schema(self) -> None:
        """旧库增量升级（只加列，不删数据）。失败只记日志，尽量保证 App 可启动。

        新库在 ``SCHEMA`` 里已包含新表；这里补 ``runs`` / ``run_platforms`` 的账号列，
        以及旧版本遗留的兼容列。所有操作幂等。
        """
        migrations = (
            ("runs", "account_policy_json", "TEXT NOT NULL DEFAULT ''"),
            ("run_platforms", "account_id", "TEXT NOT NULL DEFAULT ''"),
            ("run_platforms", "account_attempts", "INTEGER NOT NULL DEFAULT 0"),
        )
        for table, column, ddl in migrations:
            try:
                self._ensure_column(table, column, ddl)
            except sqlite3.OperationalError:
                # 例如旧库被占用/权限异常：不阻塞启动，账号池功能启动时会再暴露
                pass


    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.commit()
                self._conn.close()
            except Exception:  # noqa: BLE001 - 关闭阶段不再抛出
                pass

    # -------------------------------------------------------------- 重启恢复
    def recover_interrupted_runs(self) -> int:
        """把上次进程退出时残留的 running 记录置为失败/跳过，返回恢复的 run 数。

        ``runs.status``：running -> failed（error="interrupted"）
        ``run_platforms.status``：running -> failed；queued -> skipped
        """
        ts = now_ts()
        with self._lock:
            rows = self._query("SELECT run_id FROM runs WHERE status = 'running'")
            run_ids = [row["run_id"] for row in rows]
            if run_ids:
                self._execute(
                    "UPDATE run_platforms SET status = 'failed', finished_at = COALESCE(finished_at, ?), "
                    "message = COALESCE(NULLIF(message, ''), 'sidecar 重启中断') "
                    "WHERE status = 'running' AND run_id IN (%s)"
                    % ",".join("?" for _ in run_ids),
                    tuple([ts] + run_ids),
                )
                self._execute(
                    "UPDATE run_platforms SET status = 'skipped', finished_at = COALESCE(finished_at, ?), "
                    "message = COALESCE(NULLIF(message, ''), 'sidecar 重启中断，未执行') "
                    "WHERE status = 'queued' AND run_id IN (%s)"
                    % ",".join("?" for _ in run_ids),
                    tuple([ts] + run_ids),
                )
                self._execute(
                    "UPDATE runs SET status = 'failed', finished_at = COALESCE(finished_at, ?), "
                    "error = COALESCE(NULLIF(error, ''), 'interrupted') WHERE status = 'running'",
                    (ts,),
                )
            self._conn.commit()
            return len(run_ids)

    # -------------------------------------------------------------------- runs
    def create_run(
        self,
        run_id: str,
        params: Dict[str, Any],
        keywords: List[str],
        platforms: List[str],
        started_ts: int,
        name: str = "",
        account_policy: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(run_id, status, name, params_json, keywords_json, created_at, started_ts, finished_at, excel_path, error, account_policy_json) "
                "VALUES (?, 'running', ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)",
                (
                    run_id,
                    name or "",
                    _dumps(params),
                    _dumps(keywords),
                    now_ts(),
                    int(started_ts),
                    _dumps(account_policy or {}),
                ),
            )
            for seq, platform in enumerate(platforms):
                self._conn.execute(
                    "INSERT INTO run_platforms(run_id, platform, seq, status, progress, stage, saved_items, message) "
                    "VALUES (?, ?, ?, 'queued', 0, '', 0, '')",
                    (run_id, platform, seq),
                )
            self._conn.commit()

    def update_run(
        self,
        run_id: str,
        status: Optional[str] = None,
        finished_at: Optional[int] = None,
        excel_path: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        sets: List[str] = []
        params: List[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if finished_at is not None:
            sets.append("finished_at = ?")
            params.append(int(finished_at))
        if excel_path is not None:
            sets.append("excel_path = ?")
            params.append(str(excel_path))
        if error is not None:
            sets.append("error = ?")
            params.append(str(error))
        if not sets:
            return
        params.append(run_id)
        self._execute("UPDATE runs SET %s WHERE run_id = ?" % ", ".join(sets), tuple(params))

    def reset_run_for_resume(self, run_id: str) -> None:
        """断点续采：把 run 置回 running，清 finished_at / error（保留 started_ts、params、excel_path）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'running', finished_at = NULL, error = NULL WHERE run_id = ?",
                (run_id,),
            )
            self._conn.commit()

    def update_platform(self, run_id: str, platform: str, **fields: Any) -> None:
        clean = {k: v for k, v in fields.items() if k in _PLATFORM_UPDATE_FIELDS}
        if not clean:
            return
        sets = []
        params: List[Any] = []
        for key, value in clean.items():
            if key in ("progress", "saved_items", "account_attempts"):
                try:
                    value = int(value or 0)
                except (TypeError, ValueError):
                    value = 0
            elif key in ("started_at", "finished_at") and value is not None:
                value = int(value)
            elif key in ("stage", "message", "status", "account_id") and value is None:
                value = ""
            sets.append("%s = ?" % key)
            params.append(value)
        params.extend([run_id, platform])
        self._execute(
            "UPDATE run_platforms SET %s WHERE run_id = ? AND platform = ?" % ", ".join(sets),
            tuple(params),
        )

    def set_excel_path(self, run_id: str, path: str) -> None:
        self._execute("UPDATE runs SET excel_path = ? WHERE run_id = ?", (str(path), run_id))

    def _run_platforms(self, run_id: str) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT platform, seq, status, progress, stage, saved_items, message, started_at, finished_at, "
            "account_id, account_attempts "
            "FROM run_platforms WHERE run_id = ? ORDER BY seq ASC",
            (run_id,),
        )
        return [
            {
                "platform": row["platform"],
                "seq": row["seq"],
                "status": row["status"],
                "progress": int(row["progress"] or 0),
                "stage": row["stage"] or "",
                "saved_items": int(row["saved_items"] or 0),
                "message": row["message"] or "",
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "account_id": row["account_id"] or "",
                "account_attempts": int(row["account_attempts"] or 0),
            }
            for row in rows
        ]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        rows = self._query("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        if not rows:
            return None
        row = rows[0]
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "name": row["name"] or "",
            "params": _loads(row["params_json"], {}),
            "keywords": _loads(row["keywords_json"], []),
            "created_at": row["created_at"],
            "started_ts": row["started_ts"],
            "finished_at": row["finished_at"],
            "excel_path": row["excel_path"],
            "error": row["error"] or "",
            "account_policy": _loads(row["account_policy_json"], {}),
            "platforms": self._run_platforms(run_id),
        }

    def list_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 100), 500))
        rows = self._query(
            "SELECT run_id FROM runs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        )
        runs: List[Dict[str, Any]] = []
        for row in rows:
            run = self.get_run(row["run_id"])
            if run is not None:
                runs.append(run)
        return runs

    def run_exists(self, run_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)))

    def mark_run_status_light(self, run_id: str, status: str, finished_at: Optional[int] = None) -> None:
        self.update_run(run_id, status=status, finished_at=finished_at or now_ts())

    # ---------------------------------------------------------------- accounts
    def get_account_states(self) -> Dict[str, Dict[str, Any]]:
        rows = self._query("SELECT * FROM account_state")
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            logged_in: Optional[bool]
            if row["logged_in"] is None:
                logged_in = None
            else:
                logged_in = bool(row["logged_in"])
            result[row["platform"]] = {
                "logged_in": logged_in,
                "last_checked_at": row["last_checked_at"],
                "profile_exists": (
                    None if row["profile_exists"] is None else bool(row["profile_exists"])
                ),
                "message": row["message"] or "",
                "updated_at": row["updated_at"],
            }
        return result

    def set_account_state(
        self,
        platform: str,
        logged_in: Optional[bool],
        last_checked_at: Optional[int] = None,
        profile_exists: Optional[bool] = None,
        message: Optional[str] = None,
    ) -> None:
        if last_checked_at is None:
            last_checked_at = now_ts()
        logged_val: Optional[int] = None if logged_in is None else int(bool(logged_in))
        profile_val: Optional[int] = None if profile_exists is None else int(bool(profile_exists))
        self._execute(
            "INSERT INTO account_state(platform, logged_in, last_checked_at, profile_exists, message, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(platform) DO UPDATE SET "
            "logged_in = excluded.logged_in, "
            "last_checked_at = COALESCE(excluded.last_checked_at, account_state.last_checked_at), "
            "profile_exists = COALESCE(excluded.profile_exists, account_state.profile_exists), "
            "message = COALESCE(excluded.message, account_state.message), "
            "updated_at = excluded.updated_at",
            (
                platform,
                logged_val,
                int(last_checked_at) if last_checked_at else None,
                profile_val,
                message if message is not None else "",
                now_ts(),
            ),
        )

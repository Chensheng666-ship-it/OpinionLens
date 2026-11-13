# -*- coding: utf-8 -*-
"""离线自测专用「假 engine」（不联网、不启动浏览器）。

**不是生产引擎**。生产入口是 ``app/engine/entry.py``，
这里只是为了让 sidecar 的 selftest / UI 联调在无网络、无浏览器、无真实账号时
也能完整走通「事件 -> 状态机 -> API -> 落库 -> 导出」链路。

启用方式（sidecar 识别）::

    set FAKE_ENGINE=1
    python app/sidecar/main.py --port 0 --token t --data-dir <abs>

fake engine 与真实 engine 的命令行/事件协议完全一致（见 CONTRACT §1）：

    python _fake_engine.py --mode login|test|crawl --platform xhs --data-dir <abs> ...

环境变量（仅 fake engine 使用）：
* ``FAKE_LOGIN_SCENARIO``：``platform=confirmed|expired|failed|slow|manual``，逗号分隔，
  默认 confirmed；manual 会依次发 qrcode → waiting_qr → waiting_scan → need_manual(kind)
  → waiting_scan → confirmed，用于验证 sidecar 的人工接管状态机。
* ``FAKE_NEED_MANUAL_KIND``：manual 场景的 kind，默认 ``sms``（可 slider/captcha）。
* ``FAKE_TEST_RESULT``：``true|false``，默认 true。
* ``FAKE_CRAWL_FAIL``：逗号分隔的平台列表，这些平台 crawl 直接失败。
* crawl 关键词包含「停止」或 ``FAKE_CRAWL_SLOW=1`` 时进入慢速模式（等待 sidecar 停止）。

fake engine 会把收到的命令行参数写到 ``<data-dir>/logs/fake_engine_args_<mode>_<platform>.json``，
供 selftest 断言 sidecar 传参（例如登录 ``--headless false``、test ``--headless true``）。

fake engine crawl 会写一个最小但字段齐全的 ``media_crawler.db``（xhs_note /
xhs_note_comment / douyin_aweme / douyin_aweme_comment），供真实 exporter 联调。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

EVENT_PREFIX = "##EVENT##"

# 账号化（多账号号池）：非空时所有事件附加 account_id（与真实 engine 一致）
_FAKE_ACCOUNT_ID = ""

# 1x1 透明 PNG，足够验证 dataURL / 文件落盘链路
_FAKE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def now_ts() -> int:
    return int(time.time())


def emit(event: Dict[str, Any]) -> None:
    if _FAKE_ACCOUNT_ID and "account_id" not in event:
        event["account_id"] = _FAKE_ACCOUNT_ID
    event.setdefault("ts", now_ts())
    print(EVENT_PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def raw_log(text: str) -> None:
    print(f"[FakeEngine] {text}", flush=True)


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线自测假 engine（不联网）")
    parser.add_argument("--mode", required=True, choices=["login", "test", "crawl"])
    parser.add_argument("--platform", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--keywords", default="")
    parser.add_argument("--max-notes", type=int, default=15)
    parser.add_argument("--comments", default="true")
    parser.add_argument("--max-comments", type=int, default=20)
    parser.add_argument("--headless", default="true")
    parser.add_argument("--timeout", type=int, default=300)
    # 账号化参数（与真实 engine CLI 同名；未知参数会让 argparse 直接退出）
    parser.add_argument("--account-id", default="")
    parser.add_argument("--profile-template", default="")
    return parser.parse_args(argv)


def _account_id_of(args: argparse.Namespace) -> str:
    return str(getattr(args, "account_id", "") or "").strip()


def _profile_template_of(args: argparse.Namespace) -> str:
    return str(getattr(args, "profile_template", "") or "").strip()


def _parse_scenarios() -> Dict[str, str]:
    raw = os.environ.get("FAKE_LOGIN_SCENARIO", "").strip()
    result: Dict[str, str] = {}
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def _scenario_for(platform: str, account_id: str = "") -> str:
    """登录场景：支持 ``platform:account_id=...`` 精确匹配，回退平台级（旧行为）。"""
    scenarios = _parse_scenarios()
    account_id = str(account_id or "").strip()
    if account_id and f"{platform}:{account_id}" in scenarios:
        return scenarios[f"{platform}:{account_id}"]
    return scenarios.get(platform, "confirmed")


def _truthy_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _dump_args(args: argparse.Namespace) -> None:
    """落盘本次收到的命令行参数（供 sidecar selftest 断言 headless/timeout 等映射）。

    账号化：文件名/内容带 account_id（避免多账号互相覆盖）；空账号保持旧文件名，
    旧 selftest 的 ``read_fake_engine_args`` 不受影响。
    """
    try:
        log_dir = Path(args.data_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        account_id = _account_id_of(args)
        payload = {
            "argv": list(sys.argv[1:]),
            "mode": args.mode,
            "platform": args.platform,
            "headless": str(args.headless),
            "timeout": int(args.timeout),
            "max_notes": int(args.max_notes),
            "comments": str(args.comments),
            "max_comments": int(args.max_comments),
            "keywords": str(args.keywords),
            "account_id": account_id,
            "profile_template": _profile_template_of(args),
            "env_engine_login_headless": os.environ.get("ENGINE_LOGIN_HEADLESS", ""),
        }
        suffix = f"_{account_id}" if account_id else ""
        path = log_dir / f"fake_engine_args_{args.mode}_{args.platform}{suffix}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 调试辅助，失败不影响事件流
        pass


def _ensure_fake_profile(args: argparse.Namespace) -> None:
    """按 engine 参数创建假 profile 目录（模拟 Playwright 首次打开 profile 的行为）。

    * 有 ``--profile-template`` → 模板 % 平台（sidecar 传绝对模板；legacy 旧模板也兼容）；
    * 仅有 ``--account-id`` → 默认 v2 路径 ``browser_data/<platform>/<account_id>/user_data_dir``；
    * 都没有（旧单账号）→ 保持 fake engine 历史行为：不创建任何 profile 目录
      （selftest 既有 ``profile_exists=false`` 断言不变）。
    """
    account_id = _account_id_of(args)
    template = _profile_template_of(args)
    data_dir = Path(args.data_dir)
    target: Path | None = None
    if template:
        try:
            target = Path(template % args.platform)
        except Exception:  # noqa: BLE001 - 模板非法时忽略
            target = None
    elif account_id:
        target = data_dir / "browser_data" / str(args.platform) / account_id / "user_data_dir"
    if target is None:
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        marker = target / "fake_profile.txt"
        if not marker.exists():
            marker.write_text(
                json.dumps(
                    {"account_id": account_id, "platform": args.platform, "layout": "v2"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
def run_login(args: argparse.Namespace) -> int:
    platform = args.platform
    data_dir = Path(args.data_dir)
    scenario = _scenario_for(platform, _account_id_of(args))
    qr_dir = data_dir / "qr"
    qr_dir.mkdir(parents=True, exist_ok=True)
    qr_file = qr_dir / f"fake_{platform}.png"
    qr_file.write_bytes(_FAKE_PNG)

    emit(
        {
            "type": "engine.started",
            "mode": "login",
            "platform": platform,
            "pid": os.getpid(),
        }
    )
    raw_log(f"login 模式启动 platform={platform} scenario={scenario}（离线模拟）")
    time.sleep(0.15)
    emit(
        {
            "type": "login.qrcode",
            "platform": platform,
            "seq": 1,
            "image": "data:image/png;base64," + base64.b64encode(_FAKE_PNG).decode("ascii"),
            "file": str(qr_file),
            "bytes": len(_FAKE_PNG),
        }
    )
    raw_log("已推送二维码事件（1x1 PNG 占位图）")
    time.sleep(0.15)
    emit({"type": "login.status", "platform": platform, "status": "waiting_qr", "message": "等待扫码"})

    if scenario == "slow":
        time.sleep(0.2)
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "waiting_scan",
                "message": "已扫码，等待手机端确认（慢速模式，等待取消）",
            }
        )
        raw_log("慢速登录模式：等待 sidecar 取消（约 60s 后自动过期）")
        time.sleep(60)
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "expired",
                "message": "二维码已过期（fake slow 模式）",
            }
        )
        emit(
            {
                "type": "engine.finished",
                "ok": False,
                "mode": "login",
                "platform": platform,
                "elapsed_ms": 60000,
                "message": "登录超时/二维码已过期（fake slow 模式）",
            }
        )
        return 0

    time.sleep(0.2)
    if scenario == "expired":
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "expired",
                "message": "二维码已过期，请刷新（fake expired 模式）",
            }
        )
        emit(
            {
                "type": "engine.finished",
                "ok": False,
                "mode": "login",
                "platform": platform,
                "elapsed_ms": 500,
                "message": "登录超时：二维码已过期（fake expired 模式）",
            }
        )
        return 0
    if scenario == "failed":
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "failed",
                "message": "模拟登录失败（fake failed 模式）",
            }
        )
        emit(
            {
                "type": "engine.finished",
                "ok": False,
                "mode": "login",
                "platform": platform,
                "elapsed_ms": 500,
                "message": "模拟登录失败（fake failed 模式）",
            }
        )
        return 0

    if scenario in ("manual", "manual_sms", "manual_slider", "manual_captcha", "manual_nokind"):
        kind = os.environ.get("FAKE_NEED_MANUAL_KIND", "").strip().lower()
        if not kind:
            kind = {"manual_slider": "slider", "manual_captcha": "captcha"}.get(scenario, "sms")
        if scenario == "manual_nokind":
            # 模拟老版本 engine：need_manual 不带 kind，由 sidecar 从 message 推断
            kind = ""
        kind_label = {"sms": "短信验证码", "slider": "滑块/拼图", "captcha": "安全验证"}.get(kind, "短信验证码")
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "waiting_scan",
                "message": "已扫码，请在手机端确认",
            }
        )
        time.sleep(0.15)
        manual_event: Dict[str, Any] = {
            "type": "login.status",
            "platform": platform,
            "status": "need_manual",
            "message": f"平台需要{kind_label}：请在弹出的浏览器窗口完成验证，完成后自动继续（fake manual 模式）",
        }
        if kind:
            manual_event["kind"] = kind
        emit(manual_event)
        raw_log(f"need_manual 事件已发送 kind={kind or '(无，需兜底推断)'}（模拟人工接管等待）")
        time.sleep(0.3)
        # 人工接管期间 engine 可能周期发 waiting_scan 提示：sidecar 应保持 need_manual 粘性
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "waiting_scan",
                "message": "已扫码，请在手机端确认（人工验证等待中）",
            }
        )
        time.sleep(0.3)
        # 模拟 engine ManualGate：用户完成验证后发一次「验证已完成，继续登录…」
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "waiting_scan",
                "message": "验证已完成，继续登录…",
            }
        )
        time.sleep(0.25)
        raw_log("模拟用户已在浏览器窗口完成人工验证")
        emit(
            {
                "type": "login.status",
                "platform": platform,
                "status": "confirmed",
                "message": "登录成功（fake manual 完成）",
            }
        )
        emit(
            {
                "type": "engine.finished",
                "ok": True,
                "mode": "login",
                "platform": platform,
                "elapsed_ms": 1200,
                "message": "登录成功（fake manual 完成）",
            }
        )
        return 0

    emit(
        {
            "type": "login.status",
            "platform": platform,
            "status": "waiting_scan",
            "message": "已扫码，请在手机端确认",
        }
    )
    time.sleep(0.2)
    raw_log("模拟扫码确认成功")
    emit({"type": "login.status", "platform": platform, "status": "confirmed", "message": "登录成功（fake）"})
    emit(
        {
            "type": "engine.finished",
            "ok": True,
            "mode": "login",
            "platform": platform,
            "elapsed_ms": 700,
            "message": "登录成功（fake）",
        }
    )
    return 0


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------
def run_test(args: argparse.Namespace) -> int:
    platform = args.platform
    emit(
        {
            "type": "engine.started",
            "mode": "test",
            "platform": platform,
            "pid": os.getpid(),
        }
    )
    raw_log(f"test 模式：模拟检测 {platform} 登录态（离线模拟）")
    # FAKE_TEST_SLEEP 用于 selftest 验证「不同平台 test 并行」：模拟一个耗时的真实检测
    try:
        sleep_s = max(0.0, float(os.environ.get("FAKE_TEST_SLEEP", "0.1")))
    except ValueError:
        sleep_s = 0.1
    time.sleep(sleep_s)
    ok = _truthy_env("FAKE_TEST_RESULT", True)
    message = "已登录（fake）" if ok else "未登录（fake）"
    emit(
        {
            "type": "test.result",
            "platform": platform,
            "ok": ok,
            "message": message,
            "duration_ms": int(sleep_s * 1000) + 120,
        }
    )
    emit(
        {
            "type": "engine.finished",
            "ok": ok,
            "mode": "test",
            "platform": platform,
            "elapsed_ms": int(sleep_s * 1000) + 150,
            "message": message,
        }
    )
    return 0


# ---------------------------------------------------------------------------
# crawl：写最小 sqlite + 进度事件
# ---------------------------------------------------------------------------
_XHS_NOTE_DDL = """
CREATE TABLE IF NOT EXISTS xhs_note (
    id INTEGER PRIMARY KEY,
    creator_hash TEXT, nickname TEXT, add_ts INTEGER, last_modify_ts INTEGER,
    note_id TEXT, type TEXT, title TEXT, desc TEXT, video_url TEXT,
    time INTEGER, last_update_time INTEGER, liked_count TEXT, collected_count TEXT,
    comment_count TEXT, share_count TEXT, image_list TEXT, tag_list TEXT,
    note_url TEXT, source_keyword TEXT, xsec_token TEXT
)
"""
_XHS_COMMENT_DDL = """
CREATE TABLE IF NOT EXISTS xhs_note_comment (
    id INTEGER PRIMARY KEY,
    creator_hash TEXT, nickname TEXT, add_ts INTEGER, last_modify_ts INTEGER,
    comment_id TEXT, create_time INTEGER, note_id TEXT, content TEXT,
    sub_comment_count INTEGER, pictures TEXT, parent_comment_id TEXT, like_count TEXT
)
"""
_DY_AWEME_DDL = """
CREATE TABLE IF NOT EXISTS douyin_aweme (
    id INTEGER PRIMARY KEY,
    creator_hash TEXT, nickname TEXT, add_ts INTEGER, last_modify_ts INTEGER,
    aweme_id TEXT, aweme_type TEXT, title TEXT, desc TEXT, create_time INTEGER,
    liked_count TEXT, comment_count TEXT, share_count TEXT, collected_count TEXT,
    aweme_url TEXT, cover_url TEXT, video_download_url TEXT,
    music_download_url TEXT, note_download_url TEXT, source_keyword TEXT
)
"""
_DY_COMMENT_DDL = """
CREATE TABLE IF NOT EXISTS douyin_aweme_comment (
    id INTEGER PRIMARY KEY,
    creator_hash TEXT, nickname TEXT, add_ts INTEGER, last_modify_ts INTEGER,
    comment_id TEXT, aweme_id TEXT, content TEXT, create_time INTEGER,
    sub_comment_count TEXT, parent_comment_id TEXT, like_count TEXT, pictures TEXT
)
"""


def _connect(media_db: Path) -> sqlite3.Connection:
    media_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(media_db), timeout=15)
    for ddl in (_XHS_NOTE_DDL, _XHS_COMMENT_DDL, _DY_AWEME_DDL, _DY_COMMENT_DDL):
        conn.execute(ddl)
    conn.commit()
    return conn


def _write_xhs(
    conn: sqlite3.Connection, keywords: List[str], max_notes: int, comments: bool
) -> Dict[str, Any]:
    ts = now_ts()
    notes = 0
    comment_count = 0
    for kw_index, keyword in enumerate(keywords):
        for i in range(min(max(1, int(max_notes)), 3)):
            note_id = f"xhs_{kw_index}_{i}_{ts}"
            conn.execute(
                "INSERT INTO xhs_note(creator_hash, nickname, add_ts, last_modify_ts, note_id, type, "
                "title, desc, video_url, time, last_update_time, liked_count, collected_count, "
                "comment_count, share_count, image_list, tag_list, note_url, source_keyword, xsec_token) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"user_{10000 + i}",
                    f"测试昵称{i}（未脱敏）",
                    ts,
                    ts,
                    note_id,
                    "normal",
                    f"{keyword} 相关笔记 {i + 1}",
                    f"这是离线自测数据：关键词「{keyword}」，raw user_id=user_{10000 + i}（不脱敏）",
                    "",
                    ts - 3600,
                    ts - 1800,
                    str(10 + i),
                    str(2 + i),
                    "3",
                    "1",
                    json.dumps([], ensure_ascii=False),
                    json.dumps([keyword], ensure_ascii=False),
                    f"https://example.com/xhs/{note_id}",
                    keyword,
                    "fake-xsec-token",
                ),
            )
            notes += 1
            if comments:
                for c in range(2):
                    comment_id = f"xhs_c_{kw_index}_{i}_{c}_{ts}"
                    conn.execute(
                        "INSERT INTO xhs_note_comment(creator_hash, nickname, add_ts, last_modify_ts, "
                        "comment_id, create_time, note_id, content, sub_comment_count, pictures, "
                        "parent_comment_id, like_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"cuser_{c}",
                            f"评论用户{c}（未脱敏）",
                            ts,
                            ts,
                            comment_id,
                            ts - 1200,
                            note_id,
                            f"评论：{keyword} 的第 {c + 1} 条离线自测评论",
                            0,
                            json.dumps([], ensure_ascii=False),
                            "",
                            str(c),
                        ),
                    )
                    comment_count += 1
    conn.commit()
    return {"notes": notes, "comments": comment_count}


def _write_douyin(
    conn: sqlite3.Connection, keywords: List[str], max_notes: int, comments: bool
) -> Dict[str, Any]:
    ts = now_ts()
    items = 0
    comment_count = 0
    for kw_index, keyword in enumerate(keywords):
        for i in range(min(max(1, int(max_notes)), 3)):
            aweme_id = f"dy_{kw_index}_{i}_{ts}"
            conn.execute(
                "INSERT INTO douyin_aweme(creator_hash, nickname, add_ts, last_modify_ts, aweme_id, "
                "aweme_type, title, desc, create_time, liked_count, comment_count, share_count, "
                "collected_count, aweme_url, cover_url, video_download_url, music_download_url, "
                "note_download_url, source_keyword) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"user_{20000 + i}",
                    f"抖音测试昵称{i}（未脱敏）",
                    ts,
                    ts,
                    aweme_id,
                    "video",
                    f"{keyword} 相关视频 {i + 1}",
                    f"这是抖音离线自测数据：关键词「{keyword}」（不脱敏）",
                    ts - 3600,
                    str(20 + i),
                    "2",
                    "1",
                    "0",
                    f"https://example.com/dy/{aweme_id}",
                    "",
                    "",
                    "",
                    "",
                    keyword,
                ),
            )
            items += 1
            if comments:
                for c in range(2):
                    comment_id = f"dy_c_{kw_index}_{i}_{c}_{ts}"
                    conn.execute(
                        "INSERT INTO douyin_aweme_comment(creator_hash, nickname, add_ts, last_modify_ts, "
                        "comment_id, aweme_id, content, create_time, sub_comment_count, parent_comment_id, "
                        "like_count, pictures) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            f"cuser_{c}",
                            f"抖音评论用户{c}（未脱敏）",
                            ts,
                            ts,
                            comment_id,
                            aweme_id,
                            f"抖音评论：{keyword} 的第 {c + 1} 条离线自测评论",
                            ts - 1200,
                            "0",
                            "",
                            str(c),
                            "",
                        ),
                    )
                    comment_count += 1
    conn.commit()
    return {"notes": items, "comments": comment_count}


def _attempts_path(data_dir: Path) -> Path:
    return data_dir / "logs" / "fake_crawl_attempts.json"


def _bump_crawl_attempt(data_dir: Path, platform: str) -> int:
    """记录每个平台被 crawl 的次数（用于 FAIL_ONCE：前 N 次失败，之后成功，模拟断点续采）。"""
    path = _attempts_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, Any] = {}
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        data = {}
    attempts = int(data.get(platform, 0) or 0) + 1
    data[platform] = attempts
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return attempts


def _parse_fail_once() -> Dict[str, int]:
    """``FAKE_CRAWL_FAIL_ONCE="dy:2,wb:1"`` → {platform: 前 N 次失败}。"""
    spec: Dict[str, int] = {}
    for part in os.environ.get("FAKE_CRAWL_FAIL_ONCE", "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            key, value = part.split(":", 1)
            try:
                count = max(1, int(value.strip()))
            except ValueError:
                count = 1
        else:
            key, count = part, 1
        key = key.strip()
        if key:
            spec[key] = count
    return spec


def run_crawl(args: argparse.Namespace) -> int:
    platform = args.platform
    data_dir = Path(args.data_dir)
    keywords = [item.strip() for item in str(args.keywords or "").replace("，", ",").split(",") if item.strip()]
    if not keywords:
        keywords = ["离线自测"]
    comments = str(args.comments or "true").lower() in ("1", "true", "yes", "on")
    fail_platforms = {
        item.strip() for item in os.environ.get("FAKE_CRAWL_FAIL", "").split(",") if item.strip()
    }
    account_id = _account_id_of(args)
    # 账号级失败注入：FAKE_CRAWL_FAIL 支持 "平台"（该平台所有账号）与 "平台:account_id"（指定账号）
    should_fail = platform in fail_platforms or (
        bool(account_id) and f"{platform}:{account_id}" in fail_platforms
    )
    crawl_attempt = _bump_crawl_attempt(data_dir, platform)
    fail_once_spec = _parse_fail_once()
    fail_once = platform in fail_once_spec and crawl_attempt <= fail_once_spec[platform]

    emit(
        {
            "type": "engine.started",
            "mode": "crawl",
            "platform": platform,
            "pid": os.getpid(),
        }
    )
    raw_log(
        f"crawl 模式启动 platform={platform} keywords={','.join(keywords)} "
        f"max_notes={args.max_notes} comments={comments} attempt={crawl_attempt}（离线模拟，不联网）"
    )
    emit({"type": "progress", "platform": platform, "stage": "boot", "page": 0, "saved": 0, "message": "初始化采集器"})

    if should_fail or fail_once:
        time.sleep(0.2)
        if should_fail:
            reason = (
                f"FAKE_CRAWL_FAIL 包含 {platform}"
                if platform in fail_platforms
                else f"FAKE_CRAWL_FAIL 包含 {platform}:{account_id}"
            )
        else:
            reason = f"FAKE_CRAWL_FAIL_ONCE（第 {crawl_attempt}/{fail_once_spec[platform]} 次）"
        raw_log(f"按 {reason} 模拟 {platform} 采集失败")
        emit(
            {
                "type": "engine.finished",
                "ok": False,
                "mode": "crawl",
                "platform": platform,
                "elapsed_ms": 300,
                "message": os.environ.get("FAKE_CRAWL_ERROR_MESSAGE")
                or f"模拟采集失败（{reason}）",
            }
        )
        return 0

    slow = _truthy_env("FAKE_CRAWL_SLOW", False) or any("停止" in kw for kw in keywords)
    if slow:
        emit(
            {
                "type": "progress",
                "platform": platform,
                "stage": "search",
                "page": 1,
                "saved": 0,
                "message": "（模拟）慢速采集中，等待 sidecar 取消…",
            }
        )
        raw_log("慢速采集模式：等待 sidecar 停止（约 120s）")
        time.sleep(120)
        emit(
            {
                "type": "engine.finished",
                "ok": False,
                "mode": "crawl",
                "platform": platform,
                "elapsed_ms": 120000,
                "message": "cancelled（fake slow 模式自然结束）",
            }
        )
        return 0

    media_db = data_dir / "crawler" / "media_crawler.db"
    saved = 0
    try:
        conn = _connect(media_db)
        try:
            if platform == "xhs":
                write_result = _write_xhs(conn, keywords, args.max_notes, comments)
            elif platform == "dy":
                write_result = _write_douyin(conn, keywords, args.max_notes, comments)
            else:
                # selftest 只用 xhs/dy；其它平台只发事件不落库（真实 engine 会落库）
                write_result = {"notes": min(len(keywords) * min(max(1, args.max_notes), 3), 6), "comments": 0}
                conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - fake engine 不应因此崩溃
        raw_log(f"写入模拟 sqlite 失败：{exc}")
        write_result = {"notes": 0, "comments": 0}

    total_notes = int(write_result.get("notes") or 0)
    for kw_index, keyword in enumerate(keywords):
        emit(
            {
                "type": "progress",
                "platform": platform,
                "stage": "search",
                "page": 1,
                "saved": saved,
                "message": f"搜索关键词：{keyword}",
            }
        )
        for _ in range(min(max(1, args.max_notes), 3)):
            saved = min(total_notes, saved + 1)
            time.sleep(0.03)
            emit(
                {
                    "type": "progress",
                    "platform": platform,
                    "stage": "detail",
                    "page": 1,
                    "saved": saved,
                    "message": f"保存内容 {saved}/{total_notes or saved}",
                }
            )
        emit(
            {
                "type": "progress",
                "platform": platform,
                "stage": "comments",
                "page": 1,
                "saved": saved,
                "message": f"关键词「{keyword}」采集完成，saved={saved}",
            }
        )
    emit(
        {
            "type": "progress",
            "platform": platform,
            "stage": "done",
            "page": 1,
            "saved": saved,
            "message": "采集完成",
        }
    )
    emit(
        {
            "type": "engine.finished",
            "ok": True,
            "mode": "crawl",
            "platform": platform,
            "elapsed_ms": 1200,
            "db_path": str(media_db),
            "message": f"采集完成：内容 {saved} 条，评论 {write_result.get('comments', 0)} 条（fake）",
        }
    )
    return 0


def main(argv: List[str] | None = None) -> int:
    global _FAKE_ACCOUNT_ID
    args = parse_args(argv)
    _FAKE_ACCOUNT_ID = _account_id_of(args)
    _ensure_fake_profile(args)
    _dump_args(args)
    if args.mode == "login":
        return run_login(args)
    if args.mode == "test":
        return run_test(args)
    return run_crawl(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

# -*- coding: utf-8 -*-
"""逐个平台真实扫码登录可用性探针（开发/验收用）。

对每个平台用 engine --mode login 起一次真实浏览器，收集：
  - 是否出现 login.qrcode 事件（img bytes / seq）
  - login.status 序列、engine.finished 结果、错误摘要
  - 原始日志中的关键错误行
输出 JSON + 控制台汇总表，放在 app/data/logs/qr_probe/。

用法：
  python tests/tools/probe_platform_qr.py [--timeout 35] [--only xhs,wb]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
PYTHON = PROJECT / "MediaCrawler" / ".venv" / "Scripts" / "python.exe"
ENGINE = PROJECT / "app" / "engine" / "entry.py"
OUT_DIR = PROJECT / "app" / "data" / "logs" / "qr_probe"
PLATFORMS = ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]
LABELS = {"xhs": "小红书", "dy": "抖音", "ks": "快手", "bili": "B站", "wb": "微博", "tieba": "贴吧", "zhihu": "知乎"}

QR_RE = re.compile(r"^##EVENT##(\{.*\})\s*$")
IMPORTANT_LOG = re.compile(
    r"(ERR_|error|Error|failed|Failed|异常|超时|风控|验证码|滑块|qrcode|二维码|login|Login)"
)


def classify(result: dict) -> str:
    """可用性分类：QR_OK=已截获二维码；NEED_MANUAL=需用户在可见窗口完成验证；NO_QR=失败。"""
    if result.get("qr"):
        return "QR_OK"
    if "need_manual" in (result.get("status_trail") or []):
        return "NEED_MANUAL"
    return "NO_QR"


def run_one(platform: str, timeout: int) -> dict:
    data_dir = Path(tempfile.mkdtemp(prefix=f"qrprobe_{platform}_"))
    started = time.time()
    result = {
        "platform": platform,
        "label": LABELS.get(platform, platform),
        "data_dir": str(data_dir),
        "events": [],
        "qr": None,
        "final_status": None,
        "finished": None,
        "error": None,
        "ok": False,
        "elapsed_s": None,
        "log_tail": [],
        "key_lines": [],
    }
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [
        str(PYTHON),
        str(ENGINE),
        "--mode", "login",
        "--platform", platform,
        "--data-dir", str(data_dir),
        "--headless", "true",
        "--timeout", str(timeout),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout + 90, cwd=str(PROJECT))
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + "\n" + (exc.stderr or "")
        result["error"] = f"探针超时（{timeout + 90}s）"
    result["elapsed_s"] = round(time.time() - started, 1)

    lines = out.splitlines()
    for line in lines:
        m = QR_RE.match(line.strip())
        if not m:
            continue
        try:
            event = json.loads(m.group(1))
        except Exception:
            continue
        result["events"].append(
            {k: event.get(k) for k in ("type", "status", "seq", "message", "bytes", "kind", "image") if k in event}
        )
        if event.get("type") == "login.qrcode":
            image = event.get("image") or ""
            result["qr"] = {
                "seq": event.get("seq"),
                "bytes": event.get("bytes") or len(image),
                "file": event.get("file"),
                "data_url_len": len(image),
            }
        elif event.get("type") == "login.status":
            status = event.get("status")
            if status:
                result["status_trail"] = result.get("status_trail", []) + [status]
            if status == "need_manual":
                result["need_manual"] = {
                    "kind": event.get("kind"),
                    "message": event.get("message"),
                }
            if status in ("failed", "expired"):
                result["final_status"] = status
                if event.get("message"):
                    result["error"] = result["error"] or event.get("message")
        elif event.get("type") == "engine.finished":
            result["finished"] = {"ok": event.get("ok"), "elapsed_ms": event.get("elapsed_ms"),
                                  "message": event.get("message")}

    if result["finished"] and result["finished"].get("ok") and result["qr"]:
        result["ok"] = True
    if not result["error"]:
        for line in reversed(lines):
            if "ERR_" in line or "Call log" in line or "Error:" in line:
                result["error"] = line.strip()[:400]
                break
    result["class"] = classify(result)
    result["key_lines"] = [ln.strip()[:300] for ln in lines if IMPORTANT_LOG.search(ln)][-12:]
    result["log_tail"] = [ln.strip()[:300] for ln in lines[-15:]]

    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        raw = OUT_DIR / f"{platform}_raw.log"
        raw.write_text(out, encoding="utf-8")
        (OUT_DIR / f"{platform}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        qr_dir = OUT_DIR / f"{platform}_qr"
        src = Path(result["qr"]["file"]) if result["qr"] and result["qr"].get("file") else None
        if src and src.exists():
            qr_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, qr_dir / src.name)
    except Exception as exc:  # noqa: BLE001
        result["save_error"] = str(exc)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=35, help="每个平台等待扫码秒数")
    ap.add_argument("--only", default="", help="只测指定平台，逗号分隔")
    args = ap.parse_args()
    platforms = [p.strip() for p in args.only.split(",") if p.strip()] or PLATFORMS

    results = []
    for p in platforms:
        print(f"\n===== 探测 {p}（{LABELS.get(p, p)}） =====", flush=True)
        r = run_one(p, args.timeout)
        results.append(r)
        print(
            f"  -> {r['class']} qr={bool(r['qr'])} need_manual={r.get('need_manual')} "
            f"elapsed={r['elapsed_s']}s error={(r['error'] or '')[:160]}",
            flush=True,
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "timeout_s": args.timeout, "results": results}
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 110)
    print(f"{'平台':<8}{'分类':<14}{'二维码':<8}{'耗时':<8}{'状态轨迹':<46}{'失败原因/备注'}")
    print("=" * 110)
    for r in results:
        qr = f"{r['qr']['bytes']}B" if r["qr"] and isinstance(r["qr"], dict) else "无"
        trail = "→".join(r.get("status_trail") or [])[:44]
        print(
            f"{r['label']:<8}{r['class']:<14}{qr:<8}{str(r['elapsed_s']) + 's':<8}{trail:<46}"
            f"{(r['error'] or '')[:60]}"
        )
    print("=" * 110)
    print(f"明细: {OUT_DIR / 'summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

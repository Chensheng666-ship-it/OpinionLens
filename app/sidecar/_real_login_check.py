# -*- coding: utf-8 -*-
"""sidecar 真实登录链路检查（**需要真实网络 + 本机可见 Edge 窗口**，不是离线自测）。

用途（验收 supplement；离线自测见 ``selftest.py``）::

    # 7 平台矩阵（每个平台启动一次真实有头登录，拿到 QR/need_manual 后取消）
    python app/sidecar/_real_login_check.py

    # 只测小红书/微博，微博最多重试 3 次
    python app/sidecar/_real_login_check.py --platforms xhs,wb --wait 60

它做的是纯黑盒检查：
  1. 用临时 data-dir 启动**真实 sidecar**（真实 engine，登录默认 `--headless false` 打开可见 Edge 窗口）；
  2. 对每个平台 `POST /api/accounts/{p}/login`，轮询 `GET .../login/{id}`，
     记录 login.qrcode / login.status 轨迹 / `need_manual(kind,message)` / 终态；
  3. 校验登录子进程确实是**有头**（浏览器进程命令行无 `--headless`）；
  4. 取消登录后检查该平台 profile 名下 msedge/chrome 进程是否被杀干净（必要时兜底清理）；
  5. 输出控制台矩阵 + JSON（默认 `app/data/logs/task12_sidecar_login_matrix.json`）。

说明：真人扫码/短信码无法自动化，因此每个平台观察到「二维码出现」或「need_manual 人工接管」
即视为该平台链路 OK，随后取消登录；dy/tieba 期望进入 need_manual（等待人工验证）。
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

SIDECAR_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SIDECAR_DIR.parent.parent
if str(SIDECAR_DIR) not in sys.path:
    sys.path.insert(0, str(SIDECAR_DIR))

import engine_runner as er  # noqa: E402  （复用进程枚举/浏览器清理）

try:
    import httpx
except Exception as exc:  # pragma: no cover
    print(f"[realcheck] 缺少 httpx：{exc}（请用 MediaCrawler/.venv 的 python 运行）")
    sys.exit(2)

PYTHON = PROJECT_ROOT / "MediaCrawler" / ".venv" / "Scripts" / "python.exe"
if not PYTHON.is_file():
    PYTHON = Path(sys.executable)

PLATFORMS = ["xhs", "dy", "ks", "bili", "wb", "tieba", "zhihu"]
LABELS = er.PLATFORM_LABELS
READY_PREFIX = "##SIDECAR_READY##"
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 有头登录窗口在真实网络下的合理等待（浏览器启动 + 页面导航/重试 + 二维码渲染）
DEFAULT_WAIT = 55.0


# ---------------------------------------------------------------------------
# sidecar 进程
# ---------------------------------------------------------------------------
class Sidecar:
    def __init__(self, data_dir: Path, extra_env: Optional[Dict[str, str]] = None) -> None:
        self.data_dir = Path(data_dir)
        self.token = "realcheck-" + uuid.uuid4().hex[:12]
        self.extra_env = dict(extra_env or {})
        self.proc: Optional[subprocess.Popen[str]] = None
        self.port = 0
        self._stdout: List[str] = []
        self._stderr: List[str] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        env = os.environ.copy()
        # 真实有头测试：清掉可能从父环境残留的假引擎/无头/入口覆盖
        for key in (
            "FAKE_ENGINE",
            "SIDECAR_EXPORTER_MODULE",
            "SIDECAR_ENGINE_ENTRY",
            "SIDECAR_LOGIN_HEADLESS",
            "ENGINE_LOGIN_HEADLESS",
        ):
            env.pop(key, None)
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        env.update(self.extra_env)
        cmd = [
            str(PYTHON),
            str(SIDECAR_DIR / "main.py"),
            "--port", "0",
            "--token", self.token,
            "--data-dir", str(self.data_dir),
        ]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._read, args=(self.proc.stdout, self._stdout), daemon=True).start()
        threading.Thread(target=self._read, args=(self.proc.stderr, self._stderr), daemon=True).start()

    def _read(self, stream, sink: List[str]) -> None:
        try:
            for line in iter(stream.readline, ""):
                with self._lock:
                    sink.append(line.rstrip("\r\n"))
        except Exception:  # noqa: BLE001
            pass

    def wait_ready(self, timeout: float = 45.0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                lines = list(self._stdout)
            for line in lines:
                if line.startswith(READY_PREFIX):
                    payload = json.loads(line[len(READY_PREFIX):].strip())
                    self.port = int(payload["port"])
                    return self.port
            if self.proc is not None and self.proc.poll() is not None:
                raise RuntimeError(f"sidecar 提前退出 rc={self.proc.returncode}\n{self.stderr_tail()}")
            time.sleep(0.05)
        raise RuntimeError(f"等待 SIDECAR_READY 超时\n{self.stderr_tail()}")

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        timeout=15,
                        creationflags=_CREATE_NO_WINDOW,
                    )
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        self.proc = None

    def stderr_tail(self, count: int = 30) -> str:
        with self._lock:
            return "\n".join(self._stderr[-count:])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
        )


# ---------------------------------------------------------------------------
# HTTP / 进程辅助
# ---------------------------------------------------------------------------
def _call(client: httpx.Client, method: str, path: str, *, body: Optional[dict] = None, expect: int = 200) -> Any:
    resp = client.request(method, path, json=body)
    if resp.status_code != expect:
        raise RuntimeError(f"{method} {path} 期望 {expect} 实际 {resp.status_code}: {resp.text[:800]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"{method} {path} 返回非 JSON: {resp.text[:400]}") from exc


def _call_no_busy(client: httpx.Client, method: str, path: str, *, timeout: float = 20.0) -> Any:
    """短暂 409 BUSY 重试（取消登录/进程退出时互斥释放有毫秒级延迟）。"""
    deadline = time.monotonic() + timeout
    while True:
        resp = client.request(method, path, json=None)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 409 and time.monotonic() < deadline:
            time.sleep(0.2)
            continue
        raise RuntimeError(f"{method} {path} 失败 HTTP {resp.status_code}: {resp.text[:800]}")


def _browser_info(data_dir: Path, platform: str) -> Dict[str, Any]:
    """该平台 profile 下的浏览器进程信息（含是否真正有头）。"""
    marker = str(data_dir / "browser_data" / f"{platform}_user_data_dir").lower()
    procs = [
        item
        for item in er.list_browser_processes()
        if marker in str(item.get("commandline") or "").lower()
    ]
    mains = [
        item
        for item in procs
        if "--type=" not in str(item.get("commandline") or "").lower()
    ]
    headed: Optional[bool] = None
    if mains:
        headed = any("--headless" not in str(item.get("commandline") or "").lower() for item in mains)
    sample = ""
    if mains:
        sample = str(mains[0].get("commandline") or "")[:220]
    elif procs:
        sample = str(procs[0].get("commandline") or "")[:220]
    return {
        "count": len(procs),
        "main_count": len(mains),
        "headed": headed,
        "sample_cmdline": sample,
    }


def _cleanup_platform_browsers(data_dir: Path, platform: str) -> Dict[str, Any]:
    marker = str(data_dir / "browser_data" / f"{platform}_user_data_dir")
    before = _browser_info(data_dir, platform)
    killed = er.kill_browser_processes_by_marker(marker) if before["count"] else []
    if killed:
        time.sleep(1.0)
    after = _browser_info(data_dir, platform)
    return {"before": before["count"], "killed": killed, "after": after["count"]}


def _classify(result: Dict[str, Any]) -> str:
    if result.get("qr"):
        return "QR_OK"
    if result.get("need_manual"):
        return "NEED_MANUAL"
    if result.get("final_status") in ("failed", "expired"):
        return "FAILED"
    return "NO_QR"


def _qr_file_meta(path: str) -> Dict[str, Any]:
    """读取二维码 PNG 的大小/宽高/宽高比，并尝试 cv2 解码（验证修复：wb 空白、tieba 横幅）。"""
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    meta: Dict[str, Any] = {"file_bytes": file_path.stat().st_size}
    try:
        head = file_path.read_bytes()[:24]
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            width, height = struct.unpack(">II", head[16:24])
            meta["width"] = int(width)
            meta["height"] = int(height)
            if height:
                meta["aspect"] = round(float(width) / float(height), 3)
    except Exception:  # noqa: BLE001 - 仅调试信息
        pass
    # cv2 解码：与 engine 的“可解码”校验口径一致（补白边后 decode；未安装 cv2 时跳过）
    try:
        import cv2  # type: ignore[import-not-found]  # 原因：cv2 在检查环境无 stub；探针运行期从 venv 导入

        image = cv2.imread(str(file_path))
        if image is not None:
            detector = cv2.QRCodeDetector()
            decoded = ""
            for pad in (0, 40, 80):
                target = image
                if pad:
                    target = cv2.copyMakeBorder(
                        image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=(255, 255, 255)
                    )
                data, _points, _straight = detector.detectAndDecode(target)
                if data:
                    decoded = str(data)
                    break
            meta["cv2_decoded"] = bool(decoded)
            meta["cv2_data_len"] = len(decoded)
    except Exception:  # noqa: BLE001 - cv2 可选
        meta["cv2_available"] = False
    return meta


# ---------------------------------------------------------------------------
# 单平台检查
# ---------------------------------------------------------------------------
def check_platform(
    client: httpx.Client,
    data_dir: Path,
    platform: str,
    wait_s: float,
    attempts: int,
) -> Dict[str, Any]:
    started = time.time()
    result: Dict[str, Any] = {
        "platform": platform,
        "label": LABELS.get(platform, platform),
        "attempts": [],
        "statuses": [],
        "qr": None,
        "need_manual": None,
        "final_status": None,
        "error": None,
        "elapsed_s": None,
        "retried": False,
    }

    for attempt in range(1, attempts + 1):
        att: Dict[str, Any] = {
            "attempt": attempt,
            "statuses": [],
            "qr": None,
            "need_manual": None,
            "final_status": None,
            "browser": None,
            "cancel": None,
        }
        t0 = time.time()
        login = _call_no_busy(client, "POST", f"/api/accounts/{platform}/login")
        login_id = login["login_id"]
        print(f"    [{platform}] attempt#{attempt} login_id={login_id}", flush=True)
        deadline = t0 + wait_s
        while time.time() < deadline:
            try:
                state = _call(client, "GET", f"/api/accounts/{platform}/login/{login_id}")
            except Exception as exc:  # noqa: BLE001 - 轮询期间偶发异常继续
                att.setdefault("poll_errors", []).append(str(exc)[:200])
                time.sleep(0.5)
                continue
            status = str(state.get("status") or "")
            if not att["statuses"] or att["statuses"][-1] != status:
                att["statuses"].append(status)
                result["statuses"].append(status)
            now = time.time() - t0
            if state.get("qr") and not att["qr"]:
                att["qr"] = {
                    "seq": state["qr"].get("seq"),
                    "file": state["qr"].get("file"),
                    "data_url_len": len(state["qr"].get("image") or ""),
                    "at_s": round(now, 1),
                }
                att["qr"].update(_qr_file_meta(str(att["qr"].get("file") or "")))
                result["qr"] = att["qr"]
                print(
                    f"    [{platform}] QR seq={att['qr']['seq']} dataURL={att['qr']['data_url_len']}B "
                    f"png={att['qr'].get('width')}x{att['qr'].get('height')} "
                    f"aspect={att['qr'].get('aspect')} cv2_decoded={att['qr'].get('cv2_decoded')} "
                    f"file={att['qr'].get('file_bytes')}B at {att['qr']['at_s']}s",
                    flush=True,
                )
            if status == "need_manual" and not att["need_manual"]:
                att["need_manual"] = {
                    "kind": state.get("kind"),
                    "message": state.get("message"),
                    "manual_required": state.get("manual_required"),
                    "need_manual": state.get("need_manual"),
                    "at_s": round(now, 1),
                }
                result["need_manual"] = att["need_manual"]
                print(
                    f"    [{platform}] need_manual kind={att['need_manual']['kind']} "
                    f"at {att['need_manual']['at_s']}s: {str(att['need_manual']['message'])[:120]}",
                    flush=True,
                )
            if status in ("confirmed", "failed", "expired", "cancelled"):
                att["final_status"] = status
                result["final_status"] = status
                if status in ("failed", "expired"):
                    result["error"] = str(state.get("message") or "")[:400]
                break
            # 拿到 QR/need_manual 后再多观察 2~4s，看 engine 是否继续推进（如人工验证完成后出码）
            if att["need_manual"] and now - att["need_manual"]["at_s"] >= 2.0:
                break
            if att["qr"] and now - att["qr"]["at_s"] >= 4.0:
                break
            time.sleep(0.3)

        att["browser"] = _browser_info(data_dir, platform)
        print(
            f"    [{platform}] browser: count={att['browser']['count']} "
            f"main={att['browser']['main_count']} headed={att['browser']['headed']}",
            flush=True,
        )
        # 取消登录 + 进程树/浏览器清理校验
        try:
            cancel = _call(client, "POST", f"/api/accounts/{platform}/login/{login_id}/cancel")
        except Exception as exc:  # noqa: BLE001
            cancel = {"ok": False, "error": str(exc)[:300]}
        time.sleep(1.5)
        cleanup = _cleanup_platform_browsers(data_dir, platform)
        att["cancel"] = {
            "ok": bool(cancel.get("ok")),
            "leftover_before_cleanup": cleanup["before"],
            "killed": cleanup["killed"],
            "leftover_after_cleanup": cleanup["after"],
        }
        print(
            f"    [{platform}] cancel ok={att['cancel']['ok']} "
            f"browser_leftover={cleanup['before']} killed={len(cleanup['killed'])} "
            f"after={cleanup['after']}",
            flush=True,
        )
        att["elapsed_s"] = round(time.time() - t0, 1)
        result["attempts"].append(att)

        success = bool(att["qr"]) or bool(att["need_manual"])
        if success or attempt >= attempts:
            break
        result["retried"] = True
        time.sleep(1.0)

    result["elapsed_s"] = round(time.time() - started, 1)
    result["class"] = _classify(result)
    # 汇总取消/浏览器清理（最后一次尝试）
    last = result["attempts"][-1] if result["attempts"] else {}
    result["browser"] = last.get("browser")
    result["cancel"] = last.get("cancel")
    result["headed_ok"] = bool((result.get("browser") or {}).get("headed"))
    result["browser_clean_ok"] = bool(
        (result.get("cancel") or {}).get("leftover_after_cleanup", 0) == 0
    )
    return result


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="sidecar 真实登录链路检查（有头 Edge，需真实网络）")
    parser.add_argument("--platforms", default="", help="逗号分隔平台；默认 7 平台矩阵")
    parser.add_argument("--wait", type=float, default=DEFAULT_WAIT, help="单次登录等待秒数，默认 55")
    parser.add_argument("--attempts", type=int, default=2, help="普通平台重试次数，默认 2；wb 自动 3")
    parser.add_argument("--data-dir", default="", help="复用指定 data-dir（默认临时目录）")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "app" / "data" / "logs" / "task12_sidecar_login_matrix.json"))
    parser.add_argument("--keep", action="store_true", help="保留临时 data-dir")
    args = parser.parse_args(argv)

    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()] or list(PLATFORMS)
    unknown = [p for p in platforms if p not in LABELS]
    if unknown:
        print(f"未知平台：{unknown}（可选 {list(LABELS)}）")
        return 2

    if args.data_dir:
        data_dir = Path(args.data_dir).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        keep_data_dir = True
    else:
        data_dir = Path(tempfile.mkdtemp(prefix="sidecar_real_login_")).resolve()
        keep_data_dir = bool(args.keep)

    sidecar = Sidecar(data_dir)
    client: Optional[httpx.Client] = None
    results: List[Dict[str, Any]] = []
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        print("=" * 100)
        print("sidecar 真实登录链路检查（真实 engine + 有头 Edge 窗口 + 真实网络）")
        print("=" * 100)
        print(f"[环境] python     = {PYTHON}")
        print(f"[环境] data-dir   = {data_dir}")
        print(f"[环境] 平台       = {platforms}")
        print(f"[环境] 单次等待   = {args.wait}s")
        sidecar.start()
        port = sidecar.wait_ready()
        print(f"[sidecar] ready port={port}")
        client = sidecar.client()
        print("[sidecar] /health:", _call(client, "GET", "/health"))
        config = _call(client, "GET", "/api/config")
        print(
            "[sidecar] config:",
            {k: config.get(k) for k in ("version", "login_headless", "login_timeout", "mc_root")},
        )

        for platform in platforms:
            attempts = 3 if platform == "wb" else max(1, args.attempts)
            print(f"\n===== 检查 {platform}（{LABELS[platform]}，最多 {attempts} 次） =====", flush=True)
            results.append(check_platform(client, data_dir, platform, args.wait, attempts))

        print("\n" + "=" * 118)
        print(f"{'平台':<8}{'分类':<14}{'二维码(bytes/px)':<18}{'need_manual':<22}{'状态轨迹':<48}{'取消清理':<12}")
        print("=" * 118)
        for item in results:
            qr_info = item.get("qr") or {}
            if qr_info:
                dims = (
                    f"{qr_info.get('width')}x{qr_info.get('height')}"
                    if qr_info.get("width")
                    else "?"
                )
                qr = f"{qr_info.get('data_url_len')}B/{dims}"
            else:
                qr = "无"
            manual = item.get("need_manual") or {}
            manual_text = f"kind={manual.get('kind')}" if manual else "-"
            trail = "→".join(item.get("statuses") or [])[:46] or "-"
            clean = f"{item.get('cancel', {}).get('leftover_after_cleanup', '?')}"
            print(
                f"{item['label']:<8}{item['class']:<14}{qr:<18}{manual_text:<22}{trail:<48}{clean:<12}"
            )
        print("=" * 118)

        summary = {
            "generated_at": started_at,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": str(PYTHON),
            "data_dir": str(data_dir),
            "wait_s": args.wait,
            "sidecar_config": {
                k: config.get(k) for k in ("version", "login_headless", "login_timeout", "mc_root")
            },
            "results": results,
            "summary": {
                "qr_platforms": [item["platform"] for item in results if item.get("qr")],
                "need_manual_platforms": [item["platform"] for item in results if item.get("need_manual")],
                "failed_platforms": [item["platform"] for item in results if item["class"] in ("FAILED", "NO_QR")],
                "headed_ok": all(item.get("headed_ok") for item in results),
                "browser_clean_ok": all(item.get("browser_clean_ok") for item in results),
            },
        }
        print("\n[汇总]")
        print("  有二维码平台 :", summary["summary"]["qr_platforms"])
        print("  need_manual  :", summary["summary"]["need_manual_platforms"])
        print("  失败/无事件  :", summary["summary"]["failed_platforms"])
        print("  有头窗口验证 :", summary["summary"]["headed_ok"])
        print("  取消后清理   :", summary["summary"]["browser_clean_ok"])

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[输出] {out}")

        ok = not summary["summary"]["failed_platforms"] and summary["summary"]["headed_ok"] and summary["summary"]["browser_clean_ok"]
        return 0 if ok else 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        # 兜底清理：所有测试平台 profile 下的浏览器进程
        for platform in platforms:
            try:
                _cleanup_platform_browsers(data_dir, platform)
            except Exception:  # noqa: BLE001
                pass
        sidecar.stop()
        if keep_data_dir:
            print(f"[保留] data-dir：{data_dir}")
        else:
            import shutil

            shutil.rmtree(data_dir, ignore_errors=True)
            print(f"[清理] 已删除临时 data-dir：{data_dir}")


if __name__ == "__main__":
    sys.exit(main())

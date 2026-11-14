# -*- coding: utf-8 -*-
"""舆见 OpinionLens · Engine 入口（MediaCrawler 包装，生产版）。

契约：app/CONTRACT.md §1。用法：

    python app/engine/entry.py --mode login --platform xhs              --data-dir <abs> [--headless true] [--timeout 300]
    python app/engine/entry.py --mode test  --platform xhs              --data-dir <abs>
    python app/engine/entry.py --mode crawl --platform xhs --keywords "示例科技" --max-notes 15 --comments true --max-comments 20 --data-dir <abs>

结构化事件逐行输出：##EVENT##{"type":"...", ...}
最后一行必为 engine.finished（成功/失败都会发；失败时进程退出码仍为 0，由 sidecar 以事件为准）。
例外：上游契约探测失败（ENGINE_INCOMPATIBLE）时先发 engine.finished ok:false，再以退出码 2 结束。

上游契约探测：适配器依赖的 MediaCrawler 符号声明在 app/engine/adapters/，启动时（run_mode 之前）
统一探测；缺失即明确失败，不带着会打空枪的补丁继续跑。

环境变量：
    MC_ROOT         覆盖 MediaCrawler 根目录（默认 <project>/MediaCrawler）
    BROWSER_CHANNEL 浏览器通道，默认 msedge（系统 Edge，免装 Playwright Chromium）；chrome 可选
    ENGINE_LOGIN_HEADLESS 设为 1/true 时强制 login 模式无头（默认 login 有头，便于人工完成短信/滑块验证）
    ENGINE_EVENT_LOOP 设为 selector 时使用 WindowsSelectorEventLoopPolicy（默认不动，跟随系统）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
import traceback
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))


def _configure_javascript_runtime() -> None:
    """知乎签名脚本依赖 execjs；没有 Node 时会退回 Windows JScript。

    JScript 会报 ``SyntaxError: 语法错误``，表现为“扫码成功但测试/采集未登录”。
    安装包在 resources/runtime/node/node.exe 内置 Node；优先把它加入 PATH 并
    强制 execjs 使用 Node。开发机没有内置 Node 时继续使用系统 Node。
    """
    node_dir = ENGINE_DIR.parent / "runtime" / "node"
    bundled = node_dir / "node.exe"
    if bundled.is_file():
        os.environ["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")
        os.environ["FS_NODE_EXE"] = str(bundled)
    import shutil

    if bundled.is_file() or shutil.which("node"):
        os.environ["EXECJS_RUNTIME"] = "Node"


_configure_javascript_runtime()

import patches  # noqa: E402  （engine 本地模块，顶层只依赖标准库）


# --------------------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------------------
def _force_utf8() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def str2bool(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y", "t", "on"):
        return True
    if text in ("false", "0", "no", "n", "f", "off"):
        return False
    raise argparse.ArgumentTypeError(f"无法解析为布尔值: {value!r}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="舆见 Engine（MediaCrawler 包装）")
    parser.add_argument("--mode", required=True, choices=("login", "test", "crawl"))
    parser.add_argument("--platform", required=True, choices=patches.PLATFORMS)
    parser.add_argument("--keywords", default="", help="多个关键词用英文/中文逗号分隔")
    parser.add_argument("--max-notes", type=int, default=15, help="每关键词条数")
    parser.add_argument("--comments", default="true", help="是否抓评论（true/false）")
    parser.add_argument("--max-comments", type=int, default=20, help="每条内容评论上限")
    parser.add_argument(
        "--headless",
        default=None,
        help="无头模式（true/false）。login 默认 false（有头窗口，便于人工接管验证）；test/crawl 默认 true",
    )
    parser.add_argument("--timeout", type=int, default=300, help="login 模式最大等待秒数")
    parser.add_argument("--data-dir", required=True, help="运行期数据目录（绝对路径）")
    # 账号化（多账号号池）：空 account_id = 旧单账号行为，profile 路径/事件字段与历史完全一致
    parser.add_argument(
        "--account-id",
        default="",
        help="账号 ID（[A-Za-z0-9_-]{1,40}，空=旧单账号行为）",
    )
    parser.add_argument(
        "--profile-template",
        default=None,
        help="账号 profile 目录模板（含恰好一个 %%s 的绝对路径，%%s 由 MediaCrawler 替换为平台名）",
    )
    args = parser.parse_args(argv)
    args.comments = str2bool(args.comments)
    args.headless = None if args.headless is None else str2bool(args.headless)
    try:
        args.account_id = patches.validate_account_id(args.account_id)
        args.profile_template = patches.validate_profile_template(args.profile_template)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def resolve_headless(mode: str, cli_value, env=None) -> bool:
    """计算实际 headless 值。

    - login：默认 False（打开可见 Edge 窗口，供人工完成短信/滑块验证）；
      `--headless true` 显式无头（自动化探针）；环境变量 ENGINE_LOGIN_HEADLESS=1 强制无头
    - test/crawl：默认 True，可用 --headless false 覆盖
    """
    env_map = env if env is not None else os.environ
    if mode == "login":
        forced = str(env_map.get("ENGINE_LOGIN_HEADLESS", "")).strip().lower()
        if forced in ("1", "true", "yes", "on", "t", "y"):
            return True
        if cli_value is not None:
            return bool(cli_value)
        return False
    if cli_value is not None:
        return bool(cli_value)
    return True


def configure_event_loop() -> None:
    """契约 §1.3-8：默认跟随系统（本机 Proactor 已验证可用）；ENGINE_EVENT_LOOP=selector 时切换。"""
    if os.environ.get("ENGINE_EVENT_LOOP", "").strip().lower() != "selector":
        return
    if sys.platform != "win32":
        return
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
    except Exception:
        pass


def _install_signal_handlers() -> None:
    """尽力而为的取消：侧车在 Windows 上多用 TerminateProcess（不可捕获），此处主要覆盖 Ctrl+C/SIGTERM。"""

    def _raise_keyboard_interrupt(signum, _frame):  # type: ignore[no-untyped-def]
        raise KeyboardInterrupt(f"signal {signum}")

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _raise_keyboard_interrupt)
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# 三种模式
# --------------------------------------------------------------------------------------
async def _login_async(mc, args, data_dir: Path, emitter: patches.EventEmitter) -> None:
    """扫码登录，三种结果都准确：

    - profile 已有有效登录态（pong=true，未进入 login.begin）→ login.status confirmed(message=已登录无需扫码)
    - 需要扫码 → login.qrcode/waiting_qr → 成功 confirmed / 超时 expired
    - 登录验证页（短信/滑块/拼图）→ need_manual，等待用户在可见窗口完成后继续
    """
    patches.apply_config(
        mc,
        args.platform,
        data_dir,
        headless=args.headless,
        keywords=[],
        max_notes=1,
        comments=False,
        max_comments=1,
        account_id=args.account_id,
        profile_template=args.profile_template,
    )
    patch_info = patches.patch_browser_channel()
    patches.install_goto_retry(emitter)
    state = {"login_attempted": False, "confirmed": False, "error_status": False}
    manual = patches.ManualGate(emitter, args.platform)
    capture = patches.install_qr_capture(mc, emitter, data_dir, args.platform)
    patches.install_qr_fallback(
        mc, emitter, args.platform, manual, keepalive=not bool(args.headless)
    )
    patches.instrument_login_class(mc, emitter, args.platform, state, manual)
    if args.platform == "dy":
        patches.patch_douyin_login_dialog(mc, emitter)

    crawler = mc["main"].CrawlerFactory.create_crawler(args.platform)
    patches.patch_login_only(crawler, state)
    emitter.progress(
        "login",
        0,
        0,
        f"启动登录流程（browser channel={patch_info.get('channel')}, headless={bool(args.headless)}）",
    )

    watcher = asyncio.create_task(
        patches.watch_login_page(crawler, emitter, manual=manual, capture=capture, platform=args.platform)
    )
    try:
        # 有头登录时给人工验证留出时间：外层超时至少覆盖“浏览器启动 + 人工验证等待”
        wait_seconds = max(1, int(args.timeout))
        await asyncio.wait_for(crawler.start(), timeout=wait_seconds)
    except patches.AlreadyLoggedIn:
        # crawler.start() 里 pong=true 直接跳过 login.begin，未产生二维码：这是“已登录”而非失败
        state["confirmed"] = True
        emitter.progress("login", 0, 0, "检测到有效登录态，无需扫码")
        emitter.status("confirmed", "已登录，无需重新扫码；如需换号请先清除登录态")
    except patches.LoginOnlyDone:
        pass  # 扫码登录成功，已由 instrument_login_class 发 confirmed；search() 被替换为中止
    except asyncio.TimeoutError:
        state["error_status"] = True
        if manual.active_kind:
            emitter.status(
                "expired",
                f"等待人工验证/扫码超时（{int(args.timeout)}s，当前验证类型：{manual.active_kind}）",
            )
        else:
            emitter.status("expired", f"等待扫码超时（{int(args.timeout)}s）")
        raise
    except SystemExit as exc:  # MediaCrawler 登录失败路径大量使用 sys.exit()
        state["error_status"] = True
        emitter.status("failed", f"登录流程中止（sys.exit({exc.code})）")
        raise
    except patches.QrLoadFailed:
        # 3 轮重载重试仍拿不到有效二维码：wrapper 已发 login.status failed（明确网络/代理提示）
        state["error_status"] = True
        raise
    except Exception as exc:  # noqa: BLE001
        state["error_status"] = True
        emitter.status("failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        await patches.close_crawler(crawler)
        # 仅在“确实走了登录流程且没有二维码、也没有终态状态”时才提示二维码失败，避免误导
        if capture.count == 0 and not state["confirmed"] and not state["error_status"]:
            if manual.manual_events > 0:
                emitter.status("failed", "人工验证未完成或页面异常，未截获二维码；请在可见浏览器窗口完成验证后重试")
            else:
                emitter.status("failed", "未截获二维码（平台页面结构可能已变化）")


async def _crawl_async(mc, args, data_dir: Path, emitter: patches.EventEmitter) -> None:
    """关键词采集：使用已保存登录态；未登录直接失败（不拉起扫码）；数据落 sqlite。"""
    keywords = patches.parse_keywords(args.keywords)
    if not keywords:
        raise ValueError("crawl 模式需要 --keywords")

    info = patches.apply_config(
        mc,
        args.platform,
        data_dir,
        headless=args.headless,
        keywords=keywords,
        max_notes=args.max_notes,
        comments=args.comments,
        max_comments=args.max_comments,
        account_id=args.account_id,
        profile_template=args.profile_template,
    )
    patches.patch_browser_channel()
    patches.patch_login_blocked(mc, args.platform)
    patches.install_network_error_hook()
    if args.platform == "wb":
        # 微博无评论接口返回 ok=0 + 「还没有人评论」，不要当作错误反复重试
        patches.patch_weibo_comment_empty()
    if args.platform == "ks":
        # 快手：首屏搜索改用网页真实 payload（pcursor=""/webPageArea=""），避免推荐流混入
        patches.patch_kuaishou_search()
    progress = patches.install_progress_handler(mc, emitter)

    # main.main() 会在 start 前自动建表；engine 直接调 crawler.start()，需要自己建表
    import database.db as mc_db

    await mc_db.init_db("sqlite")

    crawler = mc["main"].CrawlerFactory.create_crawler(args.platform)
    emitter.progress("boot", 0, 0, f"开始采集：{args.platform}，关键词 {','.join(keywords)}")
    try:
        await crawler.start()
    finally:
        await patches.close_crawler(crawler)
    # 日志 Handler 通常已发过 stage=done；这里只在缺失时补一条（避免重复）
    if progress.stage != "done":
        emitter.progress("done", progress.page, progress.saved, "采集完成")
    # db_path 由 main() 统一放进 engine.finished（只需确保 sqlite 已落盘）
    _ = info


async def _test_async(mc, args, data_dir: Path, emitter: patches.EventEmitter) -> None:
    """测试连接：只做 pong 登录态探测，不触发登录/搜索。"""
    result = await patches.probe_login_state(
        mc,
        args.platform,
        data_dir,
        headless=args.headless,
        timeout=60.0,
        emitter=emitter,
        account_id=args.account_id,
        profile_template=args.profile_template,
    )
    emitter.test_result(result["ok"], result["message"], result["duration_ms"])


def run_mode(args, data_dir: Path, mc_root: Path, emitter: patches.EventEmitter) -> None:
    """按模式执行（自测会替换此函数以验证 engine.finished 兜底）。"""
    # 登录/测试只导入目标平台，减少启动等待；采集用完整 main（需要 database/store 全量初始化）
    light = args.mode in ("login", "test") and os.environ.get("ENGINE_LIGHT_IMPORT", "1") != "0"
    mc = patches.import_mediacrawler(mc_root, platform=args.platform, light=light)
    if args.mode == "login":
        asyncio.run(_login_async(mc, args, data_dir, emitter))
    elif args.mode == "test":
        asyncio.run(_test_async(mc, args, data_dir, emitter))
    elif args.mode == "crawl":
        asyncio.run(_crawl_async(mc, args, data_dir, emitter))
    else:  # pragma: no cover - argparse 已限制
        raise ValueError(f"未知模式: {args.mode}")


# --------------------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------------------
def main(argv=None) -> int:
    _force_utf8()
    sys.dont_write_bytecode = True  # 不往 MediaCrawler/ 写 __pycache__（只读约束）
    configure_event_loop()

    args = parse_args(argv)
    args.headless = resolve_headless(args.mode, args.headless)
    data_dir = Path(args.data_dir).expanduser().resolve()
    patches.ensure_data_dirs(data_dir)
    mc_root = patches.resolve_mc_root()
    db_path = str(data_dir / "crawler" / "media_crawler.db")

    emitter = patches.EventEmitter(
        platform=args.platform, mode=args.mode, account_id=args.account_id
    )
    started = time.perf_counter()
    _install_signal_handlers()
    emitter.started()

    # P0-1 上游契约探测：适配器依赖的 MediaCrawler 符号缺失时明确失败（退出码 2），
    # 绝不带着「打空枪也不报错」的补丁继续跑。
    try:
        patches.check_upstream_contract(mc_root, platform=args.platform, mode=args.mode)
    except patches.EngineIncompatible as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        emit_failed = False
        # 构造异常不再写日志（无副作用）：由抛出点在这里记录一次完整明细
        print(f"[engine] {exc.summary}", flush=True)
        print(f"[engine] 缺失明细：{exc.details}", flush=True)
        try:
            emitter.progress("boot", 0, 0, f"[error] {exc.summary}")
            emitter.finished(ok=False, elapsed_ms=elapsed_ms, db_path=None, message=str(exc))
        except Exception:  # noqa: BLE001 - 事件出口异常只影响退出码
            emit_failed = True
            traceback.print_exc()
        return 2 if not emit_failed else 1

    ok = False
    message = ""
    emit_failed = False
    try:
        run_mode(args, data_dir, mc_root, emitter)
        ok = True
    except KeyboardInterrupt:
        message = "cancelled"
        try:
            emitter.status("cancelled", "已取消")
        except Exception:
            pass
    except patches.NotLoggedIn:
        ok = False
        message = "未登录"
    except asyncio.TimeoutError:
        ok = False
        message = f"超时（{int(args.timeout)}s）"
    except SystemExit as exc:
        ok = False
        message = f"登录流程中止（sys.exit({exc.code})）"
    except patches.QrLoadFailed as exc:
        ok = False
        message = str(exc) or patches.QR_FAIL_MESSAGE
    except patches.EnginePatchError as exc:
        # 必需补丁（登录 / 二维码 / 数据解析）失败：出口层统一转成 engine.finished ok:false
        ok = False
        message = str(exc)
        traceback.print_exc()
    except Exception as exc:  # noqa: BLE001
        ok = False
        message = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    finally:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            emitter.finished(
                ok=ok,
                elapsed_ms=elapsed_ms,
                db_path=db_path if args.mode == "crawl" else None,
                message=message,
            )
        except Exception:
            emit_failed = True
            traceback.print_exc()
    return 1 if emit_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

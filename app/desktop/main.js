// =============================================================================
// 舆见 OpinionLens — Electron 主进程（M1 desktop shell）
// -----------------------------------------------------------------------------
// 职责（对齐 app/CONTRACT.md §4.1）：
//   1. 生成随机 token / sidecar 自选随机端口（--port 0）
//   2. spawn Python sidecar：python app/sidecar/main.py --port 0 --token <t> --data-dir <abs>
//   3. 解析 stdout 的 ##SIDECAR_READY##{"port":N}，/health 通过后创建 1400×900 窗口
//   4. IPC：app:info / app:openPath / app:showItemInFolder / app:openExternal
//   5. 退出时杀掉 sidecar 进程树；sidecar 启动失败时窗口内显示可读错误（绝不白屏）
//   6. 开发辅助：--screenshot <out.png>（加载后 2.5s 截图退出）、--no-sidecar（仅 UI）
//
// 安全基线：contextIsolation=true、nodeIntegration=false、sandbox=true；
//          禁止 window.open / 外部导航；token 不写日志。
// =============================================================================

'use strict';

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require('electron');
const { spawn, spawnSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');
const { isNewer, pickInstallerAsset } = require('./update');

// -----------------------------------------------------------------------------
// 路径与常量
// -----------------------------------------------------------------------------
const DESKTOP_DIR = __dirname;                                  // <project>/app/desktop（打包后为 app.asar 内）
const APP_DIR = path.resolve(DESKTOP_DIR, '..');                // <project>/app
const PROJECT_DIR = path.resolve(APP_DIR, '..');                // <project>
const IS_PACKAGED = app.isPackaged;
const RES_DIR = IS_PACKAGED ? process.resourcesPath : PROJECT_DIR;
// 打包版默认数据目录（用户可写、卸载保留）：%APPDATA%\OpinionLens
// 用户数据目录可在「设置 → 关于 → 数据存储」里改到其他盘；改动的**位置记录**必须放在固定路径，
// 不能跟着 DATA_DIR 走（否则换个盘就找不到自己的配置了）。
const BOOTSTRAP_DIR = path.join(process.env.APPDATA || app.getPath('appData'), 'OpinionLens');
const USER_CONFIG_PATH = path.join(BOOTSTRAP_DIR, 'user-config.json');
const DEFAULT_DATA_DIR = IS_PACKAGED ? BOOTSTRAP_DIR : path.join(APP_DIR, 'data');

function readUserConfig() {
  try {
    const parsed = JSON.parse(fs.readFileSync(USER_CONFIG_PATH, 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (_) {
    return {};
  }
}

const USER_CONFIG = readUserConfig();
const CUSTOM_DATA_DIR = typeof USER_CONFIG.dataDir === 'string' && USER_CONFIG.dataDir.trim()
  ? path.resolve(USER_CONFIG.dataDir)
  : '';
const DATA_DIR = CUSTOM_DATA_DIR || DEFAULT_DATA_DIR;
// 迁移数据目录时跳过的大目录/机器相关标记（Chromium 缓存、兼容回退标记）
const DATA_MIGRATE_SKIP = new Set(['electron', 'compat-no-sandbox.flag']);
const LOG_DIR = path.join(DATA_DIR, 'logs');
const SIDECAR_LOG = path.join(LOG_DIR, 'sidecar.log');
const MAIN_LOG = path.join(LOG_DIR, 'main.log');
const RENDERER_INDEX = path.join(DESKTOP_DIR, 'renderer', 'index.html');
const PRELOAD_PATH = path.join(DESKTOP_DIR, 'preload.js');
const APP_ICON_PATH = path.join(DESKTOP_DIR, 'renderer', 'assets', 'app-icon.ico');

const MC_PYTHON = process.env.MC_PYTHON
  ? path.resolve(process.env.MC_PYTHON)
  : (IS_PACKAGED
    ? path.join(RES_DIR, 'runtime', 'python', 'python.exe')
    : path.join(PROJECT_DIR, 'MediaCrawler', '.venv', 'Scripts', 'python.exe'));
const SIDECAR_ENTRY = process.env.SIDECAR_ENTRY
  ? path.resolve(process.env.SIDECAR_ENTRY)
  : (IS_PACKAGED
    ? path.join(RES_DIR, 'sidecar', 'main.py')
    : path.join(APP_DIR, 'sidecar', 'main.py'));
const MC_ROOT = process.env.MC_ROOT
  ? path.resolve(process.env.MC_ROOT)
  : (IS_PACKAGED ? path.join(RES_DIR, 'MediaCrawler') : path.join(PROJECT_DIR, 'MediaCrawler'));
const REPORT_PIPELINE_DIR = process.env.FS_REPORT_PIPELINE_DIR
  ? path.resolve(process.env.FS_REPORT_PIPELINE_DIR)
  : (IS_PACKAGED
    ? path.join(RES_DIR, 'report_pipeline')
    : path.join(APP_DIR, 'report_pipeline'));

const SIDECAR_READY_TIMEOUT_MS = 25_000;    // 契约要求：25s 内完成 ready + /health
const HEALTH_TIMEOUT_MS = 10_000;           // ready 之后额外等待 /health（已计入总 deadline）
const SIDECAR_READY_RE = /##SIDECAR_READY##(\{.*\})\s*$/;
const SCREENSHOT_DELAY_MS = Number(process.env.SCREENSHOT_DELAY_MS || 2500);
const APP_TITLE = '舆见 OpinionLens';
const APP_ID = 'com.opinionlens.desktop';

// -----------------------------------------------------------------------------
// 命令行参数（Electron 会把未知参数透传给应用）
// -----------------------------------------------------------------------------
function parseCli(argv) {
  const args = argv.slice(1);
  const opts = {
    screenshotPath: null,
    noSidecar: false,
    dev: false,
    rawNoSandbox: false,
    demoLogin: false,
    demoPage: null,
    demoTab: null,
    sidecarEntryOverride: process.env.SIDECAR_ENTRY || null,
  };
  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg === '--demo-login') {
      // 验收/演示辅助：UI 就绪后自动点「扫码登录」，配合 --screenshot 截取应用内二维码
      opts.demoLogin = true;
    } else if (arg === '--demo-page') {
      // 验收/演示辅助：切到指定页面（accounts|tasks|settings）后再截图
      opts.demoPage = args[i + 1] || null;
      i += 1;
    } else if (arg === '--demo-tab') {
      // 验收/演示辅助：设置页切到指定 tab（model|notify|pool|about）后再截图
      opts.demoTab = args[i + 1] || null;
      i += 1;
    } else if (arg === '--screenshot' || arg === '-s') {
      opts.screenshotPath = args[i + 1] ? path.resolve(args[i + 1]) : null;
      i += 1;
    } else if (arg.startsWith('--screenshot=')) {
      opts.screenshotPath = path.resolve(arg.slice('--screenshot='.length));
    } else if (arg === '--no-sidecar') {
      opts.noSidecar = true;
    } else if (arg === '--sidecar-start-fail') {
      // 仅用于验证「sidecar 起不来时窗口不白屏」：强制注入一个不存在的入口
      opts.forceSidecarEntry = path.join(APP_DIR, 'sidecar', '__not_exists__.py');
    } else if (arg === '--no-sandbox') {
      opts.rawNoSandbox = true;
    } else if (arg === '--dev') {
      opts.dev = true;
    }
  }
  return opts;
}

const CLI = parseCli(process.argv);

// 受限运行环境（CI / 沙箱 / 本机 DSH 会话）中，Chromium 渲染进程可能直接 launch-failed
// （render-process-gone: launch-failed, exitCode 18），窗口会白屏。
// 处理：调试模式（--screenshot / --dev / --no-sidecar）默认关闭 Chromium 沙箱以保证可验证；
//       正式运行（npm start / 打包后）保持沙箱开启；可用 FS_ELECTRON_SANDBOX=1 强制开启。
const DEBUG_MODE = Boolean(CLI.screenshotPath || CLI.dev || CLI.noSidecar);
const FORCE_SANDBOX_ON = process.env.FS_ELECTRON_SANDBOX === '1';
const DEBUG_SANDBOX_OFF = DEBUG_MODE && !FORCE_SANDBOX_ON;
const EFFECTIVE_NO_SANDBOX = DEBUG_SANDBOX_OFF || CLI.rawNoSandbox;
if (EFFECTIVE_NO_SANDBOX) {
  app.commandLine.appendSwitch('no-sandbox');
}

// -----------------------------------------------------------------------------
// Chromium 子进程沙箱兼容（受限网络 / 远程桌面 / EDR 拦截环境常见）
// 现象：GPU process launch failed error_code=18 → FATAL "GPU process isn't usable. Goodbye."
//       或 renderer launch-failed(exitCode 18) → 白屏。
// 策略：
//   1) Windows 默认加 --disable-gpu-sandbox，先避免 GPU 进程 FATAL（否则回退逻辑来不及执行）；
//   2) 曾成功回退过（compat 标记文件存在）或 FS_ELECTRON_COMPAT=1 → 启动即用 --no-sandbox + 关硬件加速；
//   3) 运行中仍遇到 renderer/GPU launch-failed → 写标记 + 自动以 --no-sandbox 重启一次。
// 恢复默认安全模式：删除 app/data/compat-no-sandbox.flag 并设置 FS_ELECTRON_SANDBOX=1。
// -----------------------------------------------------------------------------
const COMPAT_MARKER = path.join(DATA_DIR, 'compat-no-sandbox.flag');
let COMPAT_MARKER_EXISTED = false;
try {
  COMPAT_MARKER_EXISTED = fs.existsSync(COMPAT_MARKER);
} catch (_) {
  /* ignore */
}
const FORCE_COMPAT = process.env.FS_ELECTRON_COMPAT === '1';
const COMPAT_NO_SANDBOX_ACTIVE = EFFECTIVE_NO_SANDBOX || (!FORCE_SANDBOX_ON && (COMPAT_MARKER_EXISTED || FORCE_COMPAT));
if (process.platform === 'win32') {
  // GPU 沙箱在受限环境下最容易先崩（error_code=18），单独关掉它不降低 renderer 隔离
  app.commandLine.appendSwitch('disable-gpu-sandbox');
}
if (COMPAT_NO_SANDBOX_ACTIVE) {
  if (!EFFECTIVE_NO_SANDBOX) app.commandLine.appendSwitch('no-sandbox');
  try {
    app.disableHardwareAcceleration();
  } catch (_) {
    /* ignore */
  }
}

// Electron 自身缓存/崩溃目录落到 app/data/electron，避免污染 %APPDATA% 与文件锁冲突
try {
  app.setPath('userData', path.join(DATA_DIR, 'electron'));
} catch (_) {
  /* 忽略：路径不可写时使用默认 userData */
}

// -----------------------------------------------------------------------------
// 运行状态（app:info 暴露给 preload；不含 token 日志）
// -----------------------------------------------------------------------------
const STATE = {
  token: crypto.randomBytes(32).toString('hex'), // 每次启动随机，不落日志
  baseUrl: null,
  port: null,
  sidecar: {
    ok: false,
    skipped: false,
    starting: true,
    error: null,
    entry: CLI.forceSidecarEntry || SIDECAR_ENTRY,
    python: MC_PYTHON,
  },
};

let mainWindow = null;
let sidecarChild = null;
let sidecarKilled = false;
let isQuitting = false;
let sidecarLogStream = null;

// -----------------------------------------------------------------------------
// 日志（token 一律脱敏；sidecar stdout 落 app/data/logs/sidecar.log）
// -----------------------------------------------------------------------------
function ensureLogDir() {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
  } catch (_) {
    /* 日志目录不可写时不影响主流程 */
  }
}

function redact(text) {
  const str = String(text == null ? '' : text);
  return STATE.token ? str.split(STATE.token).join('***') : str;
}

function appendMainLog(text) {
  try {
    ensureLogDir();
    fs.appendFileSync(MAIN_LOG, `[${new Date().toISOString()}] ${redact(text)}\n`, 'utf8');
  } catch (_) {
    /* ignore */
  }
}

function appendSidecarLog(text) {
  try {
    if (!sidecarLogStream) {
      ensureLogDir();
      sidecarLogStream = fs.createWriteStream(SIDECAR_LOG, { flags: 'a', encoding: 'utf8' });
    }
    sidecarLogStream.write(`[${new Date().toISOString()}] ${redact(text)}\n`);
  } catch (_) {
    /* ignore */
  }
}

function writeSidecarLogHeader() {
  try {
    ensureLogDir();
    const header = [
      '',
      '='.repeat(78),
      `[${new Date().toISOString()}] sidecar launch`,
      `  python : ${MC_PYTHON}`,
      `  entry  : ${STATE.sidecar.entry}`,
      `  data   : ${DATA_DIR}`,
      `  token  : ***（不记录）`,
      '='.repeat(78),
    ].join('\n');
    fs.appendFileSync(SIDECAR_LOG, redact(header) + '\n', 'utf8');
  } catch (_) {
    /* ignore */
  }
}

// -----------------------------------------------------------------------------
// sidecar 启动：spawn → 读 ##SIDECAR_READY## → /health
// -----------------------------------------------------------------------------
class SidecarStartError extends Error {
  constructor(message, { kind = 'startup', tail = [] } = {}) {
    super(message);
    this.name = 'SidecarStartError';
    this.kind = kind;
    this.tail = tail;
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForHealth(baseUrl, token, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError = 'unknown';
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${baseUrl}/health`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(2000),
      });
      if (res.ok) return true;
      lastError = `HTTP ${res.status}`;
    } catch (error) {
      lastError = error && error.message ? error.message : String(error);
    }
    await delay(300);
  }
  throw new SidecarStartError(`/health 检查超时（${timeoutMs}ms）：${lastError}`, { kind: 'health' });
}

/**
 * 启动 sidecar 并等待就绪；失败时抛 SidecarStartError（窗口仍会创建并展示错误）。
 * @returns {Promise<{port:number, baseUrl:string, pid:number}>}
 */
async function startSidecar() {
  if (CLI.noSidecar) {
    STATE.sidecar = { ...STATE.sidecar, ok: false, skipped: true, starting: false, error: null };
    appendMainLog('--no-sidecar：跳过 sidecar 启动（仅调试 UI）');
    return null;
  }

  const entry = CLI.forceSidecarEntry || SIDECAR_ENTRY;
  STATE.sidecar.entry = entry;
  STATE.sidecar.starting = true;

  // sidecar 需要写入 <project>/app/data（日志/数据库/导出），启动前确保目录存在
  try {
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.mkdirSync(LOG_DIR, { recursive: true });
  } catch (error) {
    appendMainLog(`创建数据目录失败：${error.message}`);
  }

  const problems = [];
  if (!fs.existsSync(MC_PYTHON)) problems.push(`Python 解释器不存在：${MC_PYTHON}`);
  if (!fs.existsSync(entry)) problems.push(`sidecar 入口不存在：${entry}`);
  if (problems.length > 0) {
    const error = new SidecarStartError(`${problems.join('；')}`, { kind: 'precheck' });
    STATE.sidecar = { ...STATE.sidecar, ok: false, starting: false, error: error.message };
    appendMainLog(`sidecar 启动前置检查失败：${error.message}`);
    throw error;
  }

  const deadline = Date.now() + SIDECAR_READY_TIMEOUT_MS;
  const args = [entry, '--port', '0', '--token', STATE.token, '--data-dir', DATA_DIR];

  writeSidecarLogHeader();
  appendMainLog(`spawn sidecar: ${MC_PYTHON} ${path.basename(entry)} --port 0 --token *** --data-dir ${DATA_DIR}`);

  // 模型 API（OpenAI 兼容 / DeepSeek / Anthropic）由用户在「设置 → 模型 API」中配置，
  // 密钥 DPAPI 加密存 app.db；未配置时报告自动使用本地模板（degraded），无需注入任何环境变量。
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
    PYTHONNOUSERSITE: '1',
    MC_ROOT,
    MC_PYTHON,
    FS_REPORT_PIPELINE_DIR: REPORT_PIPELINE_DIR,
    FS_APP_VERSION: app.getVersion(),
  };
  // 本机 DSH 环境会注入 ELECTRON_RUN_AS_NODE，子进程不需要，清掉避免副作用
  delete env.ELECTRON_RUN_AS_NODE;
  delete env.ELECTRON_NO_ATTACH_CONSOLE;

  const child = spawn(MC_PYTHON, args, {
    cwd: IS_PACKAGED ? RES_DIR : PROJECT_DIR,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  sidecarChild = child;
  sidecarKilled = false;

  const tail = [];
  let stdoutBuffer = '';
  let resolvedPort = null;
  let settled = false;

  const ready = new Promise((resolve, reject) => {
    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      fn(value);
    };
    const timer = setTimeout(() => {
      finish(reject, new SidecarStartError(
        `sidecar 启动超时（${SIDECAR_READY_TIMEOUT_MS}ms）：未收到 ##SIDECAR_READY##。最近日志见 ${SIDECAR_LOG}`,
        { kind: 'timeout', tail: tail.slice(-30) }
      ));
    }, Math.max(1000, deadline - Date.now()));

    const remember = (line) => {
      if (!line) return;
      tail.push(line);
      if (tail.length > 200) tail.shift();
      appendSidecarLog(line);
    };

    const onStdout = (chunk) => {
      stdoutBuffer += chunk.toString('utf8');
      let idx = stdoutBuffer.indexOf('\n');
      while (idx >= 0) {
        const line = stdoutBuffer.slice(0, idx).replace(/\r$/, '');
        stdoutBuffer = stdoutBuffer.slice(idx + 1);
        remember(line);
        if (!resolvedPort) {
          const match = SIDECAR_READY_RE.exec(line);
          if (match) {
            try {
              const payload = JSON.parse(match[1]);
              const port = Number(payload.port);
              if (Number.isInteger(port) && port > 0 && port < 65536) {
                resolvedPort = port;
                finish(resolve, { port });
              } else {
                finish(reject, new SidecarStartError(`##SIDECAR_READY## 端口非法：${line}`, { kind: 'protocol' }));
              }
            } catch (error) {
              finish(reject, new SidecarStartError(`##SIDECAR_READY## JSON 解析失败：${error.message}`, { kind: 'protocol' }));
            }
          }
        }
        idx = stdoutBuffer.indexOf('\n');
      }
    };

    const onStderr = (chunk) => {
      for (const line of chunk.toString('utf8').split(/\r?\n/)) remember(line);
    };

    child.stdout.on('data', onStdout);
    child.stderr.on('data', onStderr);
    child.once('error', (error) => {
      finish(reject, new SidecarStartError(`sidecar 进程启动失败：${error.message}`, { kind: 'spawn' }));
    });
    child.once('exit', (code, signal) => {
      if (!resolvedPort) {
        finish(reject, new SidecarStartError(
          `sidecar 刚启动就退出（code=${code} signal=${signal || 'null'}）。最近日志见 ${SIDECAR_LOG}`,
          { kind: 'exit', tail: tail.slice(-30) }
        ));
      }
      appendMainLog(`sidecar 进程退出：code=${code} signal=${signal || 'null'}`);
    });
  });

  let readyInfo;
  try {
    readyInfo = await ready;
  } catch (error) {
    killSidecarProcessTree();
    STATE.sidecar = { ...STATE.sidecar, ok: false, starting: false, error: error.message };
    appendMainLog(`sidecar 启动失败：${error.message}`);
    throw error;
  }

  const baseUrl = `http://127.0.0.1:${readyInfo.port}`;
  try {
    await waitForHealth(baseUrl, STATE.token, Math.min(HEALTH_TIMEOUT_MS, Math.max(1000, deadline - Date.now())));
  } catch (error) {
    killSidecarProcessTree();
    STATE.sidecar = { ...STATE.sidecar, ok: false, starting: false, error: error.message };
    appendMainLog(`sidecar 健康检查失败：${error.message}`);
    throw error;
  }

  STATE.baseUrl = baseUrl;
  STATE.port = readyInfo.port;
  STATE.sidecar = {
    ...STATE.sidecar,
    ok: true,
    starting: false,
    error: null,
    pid: child.pid,
  };
  appendMainLog(`sidecar 就绪：${baseUrl}（pid=${child.pid}）`);
  appendSidecarLog(`sidecar 就绪：${baseUrl}（pid=${child.pid}）`);
  return { port: readyInfo.port, baseUrl, pid: child.pid };
}

/** 杀掉 sidecar 进程树（含 engine/浏览器等子进程）；可重复调用 */
function killSidecarProcessTree() {
  if (sidecarKilled || !sidecarChild || !sidecarChild.pid) {
    sidecarChild = null;
    return;
  }
  sidecarKilled = true;
  const pid = sidecarChild.pid;
  appendMainLog(`终止 sidecar 进程树 pid=${pid}`);
  try {
    const result = spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], {
      windowsHide: true,
      stdio: 'ignore',
    });
    if (result.error) throw result.error;
  } catch (error) {
    appendMainLog(`taskkill 失败，回退 child.kill：${error.message}`);
    try {
      sidecarChild.kill('SIGKILL');
    } catch (_) {
      /* ignore */
    }
  }
  sidecarChild = null;
}

/**
 * Chromium 沙箱兼容回退：写 compat 标记（后续启动直接走兼容模式）→ 以 --no-sandbox 重启一次。
 * 返回 true 表示已触发重启（调用方应尽快退出当前实例）。
 */
function relaunchWithCompat(reason) {
  if (process.env.FS_ELECTRON_COMPAT_FALLBACK === '1') {
    appendMainLog(`沙箱兼容回退已尝试过，跳过再次重启（${reason}）`);
    return false;
  }
  process.env.FS_ELECTRON_COMPAT_FALLBACK = '1';
  try {
    ensureLogDir();
    fs.mkdirSync(DATA_DIR, { recursive: true });
    fs.writeFileSync(
      COMPAT_MARKER,
      `[${new Date().toISOString()}] reason=${reason}\n`,
      'utf8'
    );
  } catch (error) {
    appendMainLog(`写兼容标记失败（忽略）：${error.message}`);
  }
  appendMainLog(`Chromium 沙箱兼容回退：${reason} → 自动以 --no-sandbox 重启（后续启动自动使用兼容模式）`);
  const relaunchArgs = process.argv.slice(1).filter((arg) => arg !== '--no-sandbox');
  relaunchArgs.push('--no-sandbox');
  try {
    app.relaunch({ args: relaunchArgs });
  } catch (error) {
    appendMainLog(`app.relaunch 失败：${error.message}`);
  }
  killSidecarProcessTree();
  app.exit(0);
  return true;
}

// GPU/子进程异常兜底：某些环境 GPU 进程会先于 renderer 崩溃（error_code=18 → FATAL）
app.on('child-process-gone', (_event, details) => {
  try {
    appendMainLog(`child-process-gone：${JSON.stringify(details)}`);
  } catch (_) {
    /* ignore */
  }
  const type = details && details.type;
  const fatal = details && (details.reason === 'launch-failed' || details.reason === 'crashed' || details.exitCode === 18);
  if ((type === 'GPU' || type === 'Utility') && fatal && !COMPAT_NO_SANDBOX_ACTIVE && !isQuitting) {
    relaunchWithCompat(`${type} 进程异常（${details.reason}/${details.exitCode}）`);
  }
});
// 旧版 Electron API 兼容
app.on('gpu-process-crashed', (_event, _killed) => {
  if (!COMPAT_NO_SANDBOX_ACTIVE && !isQuitting) {
    relaunchWithCompat('GPU 进程崩溃（gpu-process-crashed）');
  }
});

// -----------------------------------------------------------------------------
// 窗口：优先加载 renderer/index.html；缺失/加载失败时加载内置提示页（绝不白屏）
// -----------------------------------------------------------------------------
function escapeHtml(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function buildFallbackHtml() {
  const sidecarState = STATE.sidecar;
  const rendererReady = fs.existsSync(RENDERER_INDEX);
  const statusChip = CLI.noSidecar
    ? '<span class="chip chip-warn">sidecar 已跳过（--no-sidecar）</span>'
    : sidecarState.ok
      ? `<span class="chip chip-ok">sidecar 在线 · ${escapeHtml(STATE.baseUrl)}</span>`
      : `<span class="chip chip-err">sidecar 未就绪</span>`;
  const errorBlock = sidecarState.error
    ? `<div class="error"><div class="error-title">sidecar 启动失败</div><pre>${escapeHtml(sidecarState.error)}</pre>
       <div class="hint">排查：1) 确认 <code>MediaCrawler\\.venv\\Scripts\\python.exe</code> 存在；2) 确认 <code>app\\sidecar\\main.py</code> 已由 sidecar 负责人交付；3) 查看日志 ${escapeHtml(SIDECAR_LOG)}，或使用 <code>--no-sidecar</code> 先验证 UI。</div></div>`
    : '';

  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>${escapeHtml(APP_TITLE)}</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
    background: radial-gradient(1200px 600px at 20% -10%, #1e3a8a44, transparent), #0b1220;
    color: #e2e8f0; display: flex; align-items: center; justify-content: center; padding: 48px;
  }
  .card { width: min(880px, 100%); background: #111a2e; border: 1px solid #24314d; border-radius: 16px; padding: 36px 40px; box-shadow: 0 24px 80px #0008; }
  .brand { display: flex; align-items: center; gap: 14px; margin-bottom: 8px; }
  .logo { width: 44px; height: 44px; border-radius: 12px; background: linear-gradient(135deg, #2563eb, #0ea5e9); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 20px; color: #fff; }
  h1 { font-size: 22px; margin: 0; font-weight: 600; letter-spacing: .5px; }
  .sub { color: #94a3b8; font-size: 13px; margin: 6px 0 24px; }
  .chips { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 24px; }
  .chip { font-size: 12px; padding: 5px 12px; border-radius: 999px; border: 1px solid #334155; color: #cbd5e1; }
  .chip-ok { border-color: #14532d; background: #052e16; color: #86efac; }
  .chip-err { border-color: #7f1d1d; background: #2a0a0a; color: #fca5a5; }
  .chip-warn { border-color: #78350f; background: #2a1707; color: #fcd34d; }
  .grid { display: grid; grid-template-columns: 150px 1fr; gap: 10px 18px; font-size: 13px; padding: 20px; background: #0b1220; border: 1px solid #1e293b; border-radius: 12px; }
  .grid dt { color: #64748b; }
  .grid dd { margin: 0; color: #cbd5e1; word-break: break-all; font-family: Consolas, "Cascadia Mono", monospace; }
  .error { margin-top: 20px; padding: 18px 20px; border: 1px solid #7f1d1d; background: #1f0a0a; border-radius: 12px; }
  .error-title { color: #fca5a5; font-weight: 600; margin-bottom: 8px; }
  .error pre { margin: 0 0 10px; white-space: pre-wrap; word-break: break-all; color: #fecaca; font-family: Consolas, monospace; font-size: 12.5px; }
  .hint { color: #94a3b8; font-size: 12.5px; line-height: 1.7; }
  code { background: #0b1220; border: 1px solid #1e293b; border-radius: 5px; padding: 1px 5px; color: #93c5fd; }
  .foot { margin-top: 22px; color: #64748b; font-size: 12px; display: flex; justify-content: space-between; }
</style>
</head>
<body>
  <div class="card">
    <div class="brand"><div class="logo">飞</div><div><h1>${escapeHtml(APP_TITLE)}</h1>
      <div class="sub">Electron 桌面壳已启动${rendererReady ? '' : ' · 渲染进程尚未就绪，当前为内置占位页'}</div></div></div>
    <div class="chips">${statusChip}
      ${rendererReady ? '<span class="chip">renderer 已就绪</span>' : '<span class="chip chip-warn">等待 renderer/index.html</span>'}
    </div>
    <dl class="grid">
      <dt>应用版本</dt><dd>${escapeHtml(app.getVersion())}</dd>
      <dt>Electron</dt><dd>${escapeHtml(process.versions.electron || '')} / Node ${escapeHtml(process.versions.node || '')}</dd>
      <dt>sidecar 地址</dt><dd>${escapeHtml(STATE.baseUrl || '（未就绪）')}</dd>
      <dt>Python</dt><dd>${escapeHtml(MC_PYTHON)}</dd>
      <dt>sidecar 入口</dt><dd>${escapeHtml(STATE.sidecar.entry || '')}</dd>
      <dt>数据目录</dt><dd>${escapeHtml(DATA_DIR)}</dd>
      <dt>日志</dt><dd>${escapeHtml(LOG_DIR)}</dd>
    </dl>
    ${errorBlock}
    <div class="foot"><span>M1 · 采集闭环</span><span>${escapeHtml(new Date().toLocaleString('zh-CN'))}</span></div>
  </div>
</body>
</html>`;
}

function loadFallbackPage(win, reason) {
  appendMainLog(`加载内置占位页：${reason}`);
  const html = buildFallbackHtml();
  const url = `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
  return win.loadURL(url).catch((error) => appendMainLog(`占位页加载失败：${error.message}`));
}

/** 若 renderer 存在但未自行处理 sidecar 错误，注入一条醒目横幅（不改 renderer 文件） */
async function injectSidecarErrorBanner(win, message) {
  try {
    const payload = JSON.stringify(String(message));
    await win.webContents.executeJavaScript(`(() => {
      if (document.getElementById('__fs_sidecar_error__')) return;
      const box = document.createElement('div');
      box.id = '__fs_sidecar_error__';
      box.setAttribute('data-fs-injected', '1');
      box.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:2147483647;background:#7f1d1d;color:#fff;' +
        'font:13px/1.6 "Microsoft YaHei",sans-serif;padding:10px 16px;box-shadow:0 2px 12px rgba(0,0,0,.45);white-space:pre-wrap;';
      box.textContent = '采集服务（sidecar）启动失败：' + ${payload} + '\\n可查看日志目录，或联系管理员。';
      document.documentElement.appendChild(box);
    })();`);
    appendMainLog('已向 renderer 注入 sidecar 错误横幅');
  } catch (error) {
    appendMainLog(`注入 sidecar 错误横幅失败：${error.message}`);
  }
}

async function maybeCaptureScreenshot(win) {
  if (!CLI.screenshotPath) return;
  const outPath = CLI.screenshotPath;
  try {
    // 验收辅助：自动切页面（accounts|tasks）后再截图
    if (CLI.demoPage) {
      await delay(2500);
      try {
        const switched = await win.webContents.executeJavaScript(`(() => {
          const el = document.querySelector('[data-page="${String(CLI.demoPage).replace(/[^a-z]/gi, "")}"]');
          if (!el) return false;
          el.click();
          return true;
        })()`);
        appendMainLog(`--demo-page 切换 ${CLI.demoPage}：${switched}`);
        await delay(2500);
      } catch (error) {
        appendMainLog(`--demo-page 失败：${error.message}`);
      }
    }
    // 验收辅助：设置页可指定 tab（model|notify|pool|about）后截图
    if (CLI.demoTab) {
      await delay(900);
      try {
        const tabbed = await win.webContents.executeJavaScript(`(() => {
          const el = document.querySelector('[data-settings-tab="${String(CLI.demoTab).replace(/[^a-z]/gi, "")}"]');
          if (!el) return false;
          el.click();
          return true;
        })()`);
        appendMainLog(`--demo-tab 切换 ${CLI.demoTab}：${tabbed}`);
        await delay(1600);
      } catch (error) {
        appendMainLog(`--demo-tab 失败：${error.message}`);
      }
    }
    // 验收辅助：自动点「扫码登录」并等待真实二维码出现后再截图
    if (CLI.demoLogin) {
      await delay(4000);
      try {
        const clicked = await win.webContents.executeJavaScript(`(() => {
          const btns = Array.from(document.querySelectorAll('button'));
          const b = btns.find((x) => (x.textContent || '').includes('扫码登录'));
          if (!b) return false;
          b.click();
          return true;
        })()`);
        appendMainLog(`--demo-login 点击扫码登录：${clicked}`);
        await delay(22000);
      } catch (error) {
        appendMainLog(`--demo-login 失败：${error.message}`);
      }
    }
    await delay(SCREENSHOT_DELAY_MS);
    const image = await win.webContents.capturePage();
    if (!image || image.isEmpty()) throw new Error('capturePage 返回空图像');
    fs.mkdirSync(path.dirname(outPath), { recursive: true });
    fs.writeFileSync(outPath, image.toPNG());
    console.log(`##SCREENSHOT##${outPath}`);
    appendMainLog(`截图已保存：${outPath}`);
    killSidecarProcessTree();
    app.exit(0);
  } catch (error) {
    console.error(`##SCREENSHOT_ERROR##${error.message}`);
    appendMainLog(`截图失败：${error.message}`);
    killSidecarProcessTree();
    app.exit(2);
  }
}

/** 自检 preload/IPC 通路（不改 renderer 文件）：确认 window.fsapi 与 app:info 可用 */
async function preloadSelfCheck(win) {
  try {
    const result = await win.webContents.executeJavaScript(`(async () => {
      const api = window.fsapi || null;
      let info = null;
      if (api && typeof api.getInfo === 'function') {
        try { info = await api.getInfo(); } catch (e) { info = { error: String(e && e.message ? e.message : e) }; }
      }
      return {
        hasFsapi: !!api,
        keys: api ? Object.keys(api) : [],
        baseUrl: info ? (info.baseUrl || null) : null,
        hasToken: !!(info && info.token),
        dataDir: info ? (info.dataDir || null) : null,
        sidecarOk: info && info.sidecar ? !!info.sidecar.ok : null,
        sidecarSkipped: info && info.sidecar ? !!info.sidecar.skipped : null,
      };
    })()`);
    appendMainLog(`preload 自检：${JSON.stringify(result)}`);
    return result;
  } catch (error) {
    appendMainLog(`preload 自检失败：${error.message}`);
    return null;
  }
}

async function createMainWindow() {
  appendMainLog('createMainWindow：begin');
  const window = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    backgroundColor: '#0b1220',
    title: APP_TITLE,
    icon: APP_ICON_PATH,
    autoHideMenuBar: true,
    webPreferences: {
      preload: PRELOAD_PATH,
      contextIsolation: true,
      nodeIntegration: false,
      // 兼容模式下（--no-sandbox 已全局生效）显式关闭，避免个别 Electron 版本仍尝试沙箱渲染进程
      sandbox: !COMPAT_NO_SANDBOX_ACTIVE,
      webSecurity: true,
      allowRunningInsecureContent: false,
      spellcheck: false,
    },
  });
  mainWindow = window;
  appendMainLog('createMainWindow：BrowserWindow created');

  // 禁止新窗口/外部导航（硬性要求）
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  window.webContents.on('will-navigate', (event, url) => {
    const isLocal = url.startsWith('file://') || url.startsWith('data:text/html');
    if (!isLocal) {
      event.preventDefault();
      appendMainLog(`已阻止外部导航：${url}`);
    }
  });
  window.webContents.on('did-fail-load', (_e, code, desc, url) => {
    appendMainLog(`did-fail-load：${code} ${desc} ${String(url).slice(0, 120)}`);
  });
  window.webContents.on('render-process-gone', (_e, details) => {
    appendMainLog(`render-process-gone：${JSON.stringify(details)}`);
    // 受限运行环境（CI/沙箱/远程会话）中 Chromium 沙箱可能导致 renderer launch-failed
    // （exitCode 18）。写 compat 标记并以 --no-sandbox 重启一次，避免用户看到白屏/直接闪退。
    const launchFailed = details && (details.reason === 'launch-failed' || details.exitCode === 18);
    if (launchFailed && !COMPAT_NO_SANDBOX_ACTIVE && !isQuitting) {
      relaunchWithCompat(`renderer launch-failed（exitCode=${details && details.exitCode}）`);
    }
  });
  window.webContents.on('console-message', (event) => {
    try {
      const message = event && typeof event === 'object' && 'message' in event ? event.message : String(event);
      appendMainLog(`renderer console：${String(message).slice(0, 500)}`);
    } catch (_) {
      /* ignore */
    }
  });

  window.once('ready-to-show', () => {
    appendMainLog('createMainWindow：ready-to-show → show');
    window.show();
  });
  window.on('closed', () => {
    mainWindow = null;
  });

  // renderer 未就绪/加载失败 → 内置占位页（保证窗口可渲染、不白屏）
  if (fs.existsSync(RENDERER_INDEX)) {
    try {
      appendMainLog(`createMainWindow：loadFile ${RENDERER_INDEX}`);
      await window.loadFile(RENDERER_INDEX);
      appendMainLog('createMainWindow：loadFile done');
    } catch (error) {
      appendMainLog(`加载 renderer 失败：${error.message}`);
      await loadFallbackPage(window, `renderer 加载失败：${error.message}`);
    }
  } else {
    appendMainLog('createMainWindow：renderer/index.html 不存在，加载内置占位页');
    await loadFallbackPage(window, `renderer/index.html 不存在：${RENDERER_INDEX}`);
    appendMainLog('createMainWindow：占位页加载完成');
  }

  // sidecar 失败且 renderer 已加载：补一条可读错误横幅（renderer 自身也可通过 app:info 展示）
  if (!STATE.sidecar.ok && !STATE.sidecar.skipped && STATE.sidecar.error) {
    if (fs.existsSync(RENDERER_INDEX)) await injectSidecarErrorBanner(window, STATE.sidecar.error);
  }

  await preloadSelfCheck(window);

  appendMainLog(`窗口已创建：${window.getSize().join('x')}`);
  maybeCaptureScreenshot(window).catch((error) => {
    console.error(`##SCREENSHOT_ERROR##${error.message}`);
    app.exit(2);
  });
  return window;
}

// -----------------------------------------------------------------------------
// 版本更新（GitHub Release）—— 主进程代 renderer 联网，renderer 不直连外网
// 契约 §4.1：app:checkUpdate / app:downloadUpdate / app:runUpdateInstaller
// -----------------------------------------------------------------------------
const UPDATE_REPO = process.env.FS_UPDATE_REPO || 'SolitudeZY/OpinionLens';
const UPDATE_TIMEOUT_MS = 12_000;
const UPDATE_DOWNLOAD_TIMEOUT_MS = 60_000;
const ALLOWED_DOWNLOAD_HOSTS = ['github.com', 'objects.githubusercontent.com', 'githubusercontent.com', 'codeload.github.com'];

function userAgent() {
  return `OpinionLens/${app.getVersion()} (+https://github.com/${UPDATE_REPO})`;
}

/** GET 一个 JSON 接口（跟随一次 3xx）。 */
function httpGetJson(url, timeoutMs = UPDATE_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': userAgent(), Accept: 'application/vnd.github+json' } }, (res) => {
      const status = res.statusCode || 0;
      if (status >= 300 && status < 400 && res.headers.location) {
        res.resume();
        httpGetJson(new URL(res.headers.location, url).toString(), timeoutMs).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        res.resume();
        reject(new Error(`HTTP ${status}`));
        return;
      }
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        try { resolve(JSON.parse(body)); } catch (_) { reject(new Error('响应不是合法 JSON')); }
      });
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`请求超时（${timeoutMs}ms）`)));
    req.on('error', reject);
  });
}

/** 兜底：/releases/latest 的 302 Location 里解析 tag（匿名 API 限流时仍能判断有没有新版）。 */
function httpResolveLatestTag(timeoutMs = UPDATE_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const req = https.request(
      `https://github.com/${UPDATE_REPO}/releases/latest`,
      { method: 'HEAD', headers: { 'User-Agent': userAgent() } },
      (res) => {
        const location = res.headers.location || '';
        res.resume();
        const match = /\/releases\/tag\/([^/?#]+)/.exec(location);
        if (match) resolve(decodeURIComponent(match[1]));
        else reject(new Error(`跳转里没有 tag（HTTP ${res.statusCode || 0}）`));
      }
    );
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`请求超时（${timeoutMs}ms）`)));
    req.on('error', reject);
    req.end();
  });
}

/**
 * 查询最新 Release。API 失败（限流/断网）时退回解析跳转地址，至少拿到版本号与发布页。
 * @returns {Promise<{source:string, tag:string, name:string, notes:string, htmlUrl:string, publishedAt:string, download:object|null, warning?:string}>}
 */
async function fetchLatestRelease() {
  const apiUrl = `https://api.github.com/repos/${UPDATE_REPO}/releases/latest`;
  try {
    const release = await httpGetJson(apiUrl);
    const tag = String(release.tag_name || release.name || '').trim();
    return {
      source: 'api',
      tag,
      name: String(release.name || tag),
      notes: String(release.body || '').slice(0, 4000),
      htmlUrl: String(release.html_url || `https://github.com/${UPDATE_REPO}/releases/tag/${tag}`),
      publishedAt: String(release.published_at || ''),
      download: pickInstallerAsset(release.assets, tag),
    };
  } catch (error) {
    const htmlUrl = `https://github.com/${UPDATE_REPO}/releases/latest`;
    try {
      const tag = await httpResolveLatestTag();
      return {
        source: 'redirect',
        tag,
        name: tag,
        notes: '',
        htmlUrl,
        publishedAt: '',
        download: null,
        warning: `GitHub API 不可用（${error.message}），已按跳转地址解析版本；只能引导到发布页下载`,
      };
    } catch (fallbackError) {
      throw new Error(`检查更新失败：${error.message}；兜底也失败：${fallbackError.message}`);
    }
  }
}

/** 流式下载到本地文件，返回 {path,size}。进度通过 onProgress(received,total) 回报。 */
function downloadToFile(url, destPath, onProgress, timeoutMs = UPDATE_DOWNLOAD_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': userAgent() } }, (res) => {
      const status = res.statusCode || 0;
      if (status >= 300 && status < 400 && res.headers.location) {
        res.resume();
        downloadToFile(new URL(res.headers.location, url).toString(), destPath, onProgress, timeoutMs).then(resolve, reject);
        return;
      }
      if (status !== 200) {
        res.resume();
        reject(new Error(`HTTP ${status}`));
        return;
      }
      const total = Number(res.headers['content-length'] || 0);
      let received = 0;
      const out = fs.createWriteStream(destPath);
      res.on('data', (chunk) => {
        received += chunk.length;
        try { onProgress(received, total); } catch (_) { /* 进度回调失败不影响下载 */ }
      });
      res.on('error', reject);
      out.on('error', reject);
      out.on('finish', () => out.close(() => resolve({ path: destPath, size: received, total })));
      res.pipe(out);
    });
    req.setTimeout(timeoutMs, () => req.destroy(new Error(`下载超时（${timeoutMs}ms）`)));
    req.on('error', reject);
  });
}

function isAllowedDownloadUrl(raw) {
  try {
    const parsed = new URL(String(raw || ''));
    if (parsed.protocol !== 'https:') return false;
    const host = parsed.hostname.toLowerCase();
    return ALLOWED_DOWNLOAD_HOSTS.some((item) => host === item || host.endsWith('.' + item));
  } catch (_) {
    return false;
  }
}

// -----------------------------------------------------------------------------
// 数据目录迁移（用户把数据从 C 盘挪到其他盘）
// -----------------------------------------------------------------------------
async function copyDataTree(src, dst, skipNames, onEntry) {
  await fs.promises.mkdir(dst, { recursive: true });
  const entries = await fs.promises.readdir(src, { withFileTypes: true });
  let done = 0;
  for (const entry of entries) {
    const from = path.join(src, entry.name);
    const to = path.join(dst, entry.name);
    if (!skipNames.has(entry.name)) {
      if (entry.isDirectory()) {
        await fs.promises.cp(from, to, { recursive: true, force: true });
      } else if (entry.isFile()) {
        await fs.promises.copyFile(from, to);
      }
    }
    done += 1;
    try { onEntry(done, entries.length, entry.name); } catch (_) { /* 进度回调失败忽略 */ }
  }
}

function writeUserConfig(patch) {
  const next = { ...readUserConfig(), ...patch };
  fs.mkdirSync(path.dirname(USER_CONFIG_PATH), { recursive: true });
  fs.writeFileSync(USER_CONFIG_PATH, `${JSON.stringify(next, null, 2)}\n`, 'utf8');
  return next;
}

function relaunchApp() {
  const args = process.argv.slice(1).filter((arg) => arg !== '--no-sandbox');
  if (process.argv.includes('--no-sandbox')) args.push('--no-sandbox');
  try {
    app.relaunch({ args });
  } catch (error) {
    appendMainLog(`app.relaunch 失败：${error.message}`);
  }
  setTimeout(() => app.exit(0), 300);
}

// -----------------------------------------------------------------------------
// IPC（对齐 CONTRACT §4.1/4.2）
// -----------------------------------------------------------------------------
function registerIpc() {
  ipcMain.handle('app:info', () => ({
    baseUrl: STATE.baseUrl,
    token: STATE.token,
    python: MC_PYTHON,
    dataDir: DATA_DIR,
    dataDirDefault: DEFAULT_DATA_DIR,
    dataDirCustom: !!CUSTOM_DATA_DIR,
    userConfigPath: USER_CONFIG_PATH,
    appVersion: app.getVersion(),
    electronVersion: process.versions.electron,
    platform: process.platform,
    arch: process.arch,
    updateRepo: UPDATE_REPO,
    sidecar: {
      ok: STATE.sidecar.ok,
      skipped: STATE.sidecar.skipped,
      starting: STATE.sidecar.starting,
      error: STATE.sidecar.error,
      pid: STATE.sidecar.pid || null,
    },
    dev: CLI.dev,
  }));

  ipcMain.handle('app:openPath', async (_event, targetPath) => {
    if (typeof targetPath !== 'string' || targetPath.trim() === '') {
      return { ok: false, error: '路径无效' };
    }
    const abs = path.resolve(targetPath);
    const error = await shell.openPath(abs);
    return { ok: !error, error: error || null, path: abs };
  });

  ipcMain.handle('app:showItemInFolder', (_event, targetPath) => {
    if (typeof targetPath !== 'string' || targetPath.trim() === '') {
      return { ok: false, error: '路径无效' };
    }
    shell.showItemInFolder(path.resolve(targetPath));
    return { ok: true };
  });

  // 采集动态「原文链接」：交给系统浏览器打开（renderer 被禁止直接弹窗/导航外部 URL）。
  // 安全：仅放行 http/https 且长度 ≤2048；日志只记 host，不落完整 URL。
  ipcMain.handle('app:openExternal', async (_event, url) => {
    if (typeof url !== 'string' || url.trim() === '') {
      return { ok: false, error: '链接无效' };
    }
    const raw = url.trim();
    if (raw.length > 2048) {
      return { ok: false, error: '链接过长（超过 2048 字符）' };
    }
    let parsed;
    try {
      parsed = new URL(raw);
    } catch (_) {
      return { ok: false, error: '链接格式不正确' };
    }
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return { ok: false, error: '仅支持 http/https 链接' };
    }
    try {
      await shell.openExternal(parsed.toString());
      appendMainLog(`app:openExternal：已在系统浏览器打开 ${parsed.host}`);
      return { ok: true };
    } catch (error) {
      // 系统错误信息可能包含完整 URL：日志与提示统一脱敏为 <链接>，日志仅记录 host。
      const message = String((error && error.message) || error || '未知错误').slice(0, 200);
      const safeMessage = message.replace(/https?:\/\/\S+/gi, '<链接>');
      appendMainLog(`app:openExternal 失败（${parsed.host}）：${safeMessage}`);
      return { ok: false, error: safeMessage || '系统拒绝打开该链接' };
    }
  });

  // ---- 版本更新 ------------------------------------------------------------
  ipcMain.handle('app:checkUpdate', async () => {
    const current = app.getVersion();
    try {
      const release = await fetchLatestRelease();
      const hasUpdate = isNewer(current, release.tag);
      appendMainLog(`检查更新：当前 v${current} / 最新 ${release.tag}（hasUpdate=${hasUpdate}, source=${release.source}）`);
      return {
        ok: true,
        current,
        latest: release.tag.replace(/^[vV]+/, ''),
        hasUpdate,
        releaseUrl: release.htmlUrl,
        releaseName: release.name,
        notes: release.notes,
        publishedAt: release.publishedAt,
        download: release.download,
        source: release.source,
        warning: release.warning || '',
      };
    } catch (error) {
      appendMainLog(`检查更新失败：${error.message}`);
      return { ok: false, current, error: '检查更新失败：' + String(error.message || error) };
    }
  });

  ipcMain.handle('app:downloadUpdate', async (event, payload) => {
    const url = payload && typeof payload.url === 'string' ? payload.url.trim() : '';
    const rawName = payload && typeof payload.name === 'string' ? payload.name.trim() : '';
    if (!url || !isAllowedDownloadUrl(url)) {
      return { ok: false, error: '下载地址无效（仅支持 GitHub https 地址）' };
    }
    const safeName = (rawName || 'OpinionLens-Setup.exe').replace(/[\\/:*?"<>|]/g, '_').slice(-120);
    const destPath = path.join(os.tmpdir(), 'OpinionLens-update', safeName);
    try {
      fs.mkdirSync(path.dirname(destPath), { recursive: true });
    } catch (error) {
      return { ok: false, error: `创建临时目录失败：${error.message}` };
    }
    const sender = event.sender;
    let lastSent = 0;
    try {
      const result = await downloadToFile(url, destPath, (received, total) => {
        const now = Date.now();
        if (now - lastSent < 200 && received !== total) return;
        lastSent = now;
        try {
          sender.send('app:updateProgress', {
            received,
            total,
            percent: total > 0 ? Math.min(100, Math.round((received / total) * 100)) : null,
          });
        } catch (_) { /* 窗口已关闭 */ }
      });
      // 基础校验：Windows 安装包必须是 PE（MZ 头），防止把错误页保存成 exe
      const head = Buffer.alloc(2);
      const fd = fs.openSync(destPath, 'r');
      try { fs.readSync(fd, head, 0, 2, 0); } finally { fs.closeSync(fd); }
      if (head.toString('latin1') !== 'MZ') {
        fs.rmSync(destPath, { force: true });
        return { ok: false, error: '下载内容不是有效的 Windows 安装包（可能被网络代理拦截）' };
      }
      appendMainLog(`更新包下载完成：${destPath}（${(result.size / 1048576).toFixed(1)} MB）`);
      return { ok: true, path: destPath, size: result.size };
    } catch (error) {
      appendMainLog(`更新包下载失败：${error.message}`);
      try { fs.rmSync(destPath, { force: true }); } catch (_) { /* ignore */ }
      return { ok: false, error: `下载失败：${String(error.message || error)}` };
    }
  });

  ipcMain.handle('app:runUpdateInstaller', async (_event, filePath) => {
    if (typeof filePath !== 'string' || !filePath.trim()) {
      return { ok: false, error: '安装包路径无效' };
    }
    const abs = path.resolve(filePath);
    const tmpRoot = path.resolve(os.tmpdir(), 'OpinionLens-update');
    if (!abs.toLowerCase().startsWith(tmpRoot.toLowerCase() + path.sep)) {
      return { ok: false, error: '只允许运行本应用下载到临时目录的安装包' };
    }
    if (!fs.existsSync(abs)) return { ok: false, error: '安装包不存在，请重新下载' };
    const error = await shell.openPath(abs);
    if (error) return { ok: false, error };
    appendMainLog(`已启动更新安装包：${path.basename(abs)}`);
    return { ok: true };
  });

  // ---- 数据目录 ------------------------------------------------------------
  ipcMain.handle('app:chooseDataDir', async () => {
    const options = {
      title: '选择数据保存目录',
      defaultPath: DATA_DIR,
      buttonLabel: '使用此目录',
      properties: ['openDirectory', 'createDirectory'],
    };
    const result = mainWindow
      ? await dialog.showOpenDialog(mainWindow, options)
      : await dialog.showOpenDialog(options);
    if (result.canceled || !result.filePaths || result.filePaths.length === 0) {
      return { ok: false, canceled: true };
    }
    return { ok: true, path: result.filePaths[0] };
  });

  ipcMain.handle('app:migrateDataDir', async (event, targetRaw) => {
    const targetText = typeof targetRaw === 'string' ? targetRaw.trim() : '';
    if (!targetText) return { ok: false, error: '目标目录无效' };
    const target = path.resolve(targetText);
    const source = path.resolve(DATA_DIR);
    const lowerTarget = target.toLowerCase();
    const lowerSource = source.toLowerCase();
    if (!path.isAbsolute(targetText)) return { ok: false, error: '请选择绝对路径' };
    if (lowerTarget === lowerSource) return { ok: false, error: '目标目录与当前数据目录相同' };
    if (lowerTarget.startsWith(lowerSource + path.sep)) return { ok: false, error: '目标目录不能位于当前数据目录内部' };
    if (lowerSource.startsWith(lowerTarget + path.sep)) return { ok: false, error: '目标目录不能是当前数据目录的上级目录' };
    if (fs.existsSync(path.join(target, 'app.db'))) {
      return { ok: false, code: 'DATA_TARGET_OCCUPIED', error: '目标目录已存在 OpinionLens 数据（app.db），请另选空目录' };
    }
    try {
      fs.mkdirSync(target, { recursive: true });
      const probe = path.join(target, '.ol-write-probe');
      fs.writeFileSync(probe, 'ok', 'utf8');
      fs.rmSync(probe, { force: true });
    } catch (error) {
      return { ok: false, code: 'DATA_TARGET_NOT_WRITABLE', error: `目标目录不可写：${error.message}` };
    }

    const sender = event.sender;
    const progress = (step, label, extra) => {
      try { sender.send('app:dataDirProgress', { step, label, ...(extra || {}) }); } catch (_) { /* 窗口已关闭 */ }
    };
    const wasRunning = !!(sidecarChild && !sidecarKilled);
    progress('stopping', '停止本地服务…');
    killSidecarProcessTree();
    try {
      progress('copying', '复制数据…', { percent: 0 });
      await copyDataTree(source, target, DATA_MIGRATE_SKIP, (done, total, name) => {
        progress('copying', `复制 ${name}（${done}/${total}）`, { percent: Math.round((done / Math.max(1, total)) * 100) });
      });
      progress('config', '写入配置…');
      writeUserConfig({ dataDir: target, dataDirUpdatedAt: new Date().toISOString() });
      appendMainLog(`数据目录已迁移：${source} → ${target}（旧目录保留，可手动清理）`);
      progress('restarting', '重启应用…');
      relaunchApp();
      return { ok: true, source, target, oldDirKept: true, needRestart: true };
    } catch (error) {
      appendMainLog(`数据目录迁移失败：${error.message}`);
      if (wasRunning) {
        startSidecar().catch((restartError) => appendMainLog(`迁移失败后重启 sidecar 失败：${restartError.message}`));
      }
      return { ok: false, code: 'DATA_MIGRATE_FAILED', error: `迁移失败：${error.message}` };
    }
  });

  ipcMain.handle('app:resetDataDir', async () => {
    try {
      const current = readUserConfig();
      delete current.dataDir;
      delete current.dataDirUpdatedAt;
      fs.mkdirSync(path.dirname(USER_CONFIG_PATH), { recursive: true });
      fs.writeFileSync(USER_CONFIG_PATH, `${JSON.stringify(current, null, 2)}\n`, 'utf8');
      appendMainLog(`数据目录已恢复默认：${DEFAULT_DATA_DIR}`);
      relaunchApp();
      return { ok: true, target: DEFAULT_DATA_DIR, needRestart: true };
    } catch (error) {
      return { ok: false, error: `恢复默认目录失败：${error.message}` };
    }
  });
}

// -----------------------------------------------------------------------------
// 应用生命周期
// -----------------------------------------------------------------------------
function bootstrapSingleInstance() {
  // 截图模式（验收/CI）不抢单实例锁，避免与已打开窗口互相退出
  if (CLI.screenshotPath) return true;
  const gotLock = app.requestSingleInstanceLock();
  if (!gotLock) {
    app.quit();
    return false;
  }
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
  return true;
}

app.setAppUserModelId(APP_ID);
Menu.setApplicationMenu(null);

app.whenReady().then(async () => {
  ensureLogDir();
  appendMainLog(`应用启动：${APP_TITLE} ${app.getVersion()} electron=${process.versions.electron} node=${process.versions.node}`);
  appendMainLog(`截图模式：${CLI.screenshotPath || 'off'}；--no-sidecar：${CLI.noSidecar}；--dev：${CLI.dev}`);
  appendMainLog(`调试模式关闭 Chromium 沙箱：${EFFECTIVE_NO_SANDBOX}（正式运行保持开启；FS_ELECTRON_SANDBOX=1 可强制开启）`);

  if (!bootstrapSingleInstance()) return;
  registerIpc();

  try {
    await startSidecar();
  } catch (error) {
    // 失败不退出：照常创建窗口，由内置页/横幅展示可读错误
    appendMainLog(`sidecar 未就绪（继续创建窗口）：${error.message}`);
  }

  await createMainWindow();

  if (CLI.dev && !CLI.screenshotPath) {
    mainWindow?.webContents.openDevTools({ mode: 'detach' });
  }
});

app.on('window-all-closed', () => {
  killSidecarProcessTree();
  app.quit();
});

app.on('before-quit', () => {
  isQuitting = true;
  killSidecarProcessTree();
});

app.on('will-quit', () => {
  killSidecarProcessTree();
});

process.on('uncaughtException', (error) => {
  appendMainLog(`uncaughtException：${error && error.stack ? error.stack : error}`);
  try {
    killSidecarProcessTree();
  } catch (_) {
    /* ignore */
  }
  if (!mainWindow) app.exit(1);
});

process.on('unhandledRejection', (reason) => {
  appendMainLog(`unhandledRejection：${reason && reason.stack ? reason.stack : reason}`);
});

// 导出少量内部函数便于本地 node 语法/行为冒烟（不会在 Electron 运行时用到）
module.exports = {
  parseCli,
  buildFallbackHtml,
  STATE,
  MC_PYTHON,
  SIDECAR_ENTRY,
  DATA_DIR,
};

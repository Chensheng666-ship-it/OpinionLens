/* =============================================================================
   e2e · 最小 CDP 客户端（零依赖，依赖 Node 22 全局 WebSocket / fetch）
   - 目标发现：GET http://127.0.0.1:<port>/json/list → 找 renderer 的 index.html 页面
   - evaluate：Runtime.evaluate（awaitPromise + returnByValue），把异常转成可读 Error
   用法：
     const cdp = require('./cdp');
     const target = await cdp.waitForRendererTarget(port, { log });
     const client = await cdp.CdpClient.connect(target, { log });
     const mode = await client.evaluate('FS.mode');
   ========================================================================== */
'use strict';

const fs = require('fs');

const TARGET_POLL_INTERVAL_MS = 300;
const TARGET_TIMEOUT_MS = 30000;
const EVAL_TIMEOUT_MS = 15000;
const HTTP_TIMEOUT_MS = 3000;

/* 日志脱敏：getInfo 会返回 sidecar Bearer token / 各类密钥，写进 cdp.log（CI 失败时上传）前必须打码 */
const SECRET_FIELD_RE = /("(?:token|api_key|apiKey|secret|password|authorization|sendkey)"\s*:\s*")([^"]*)(")/gi;
const BEARER_RE = /(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}/g;

function redact(text) {
  return String(text)
    .replace(SECRET_FIELD_RE, '$1***$3')
    .replace(BEARER_RE, '$1***');
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** 追加写日志（cdp.log）；未给路径时静默（E2E_VERBOSE=1 时打到 stdout）。 */
function createLogger(filePath) {
  let stream = null;
  if (filePath) {
    try {
      fs.mkdirSync(require('path').dirname(filePath), { recursive: true });
      stream = fs.createWriteStream(filePath, { flags: 'a' });
    } catch (_) {
      stream = null;
    }
  }
  function log(line) {
    const text = `[${new Date().toISOString()}] ${redact(line)}`;
    if (stream) stream.write(`${text}\n`);
    if (process.env.E2E_VERBOSE === '1') console.log(text);
  }
  log.close = function close() {
    try {
      if (stream) stream.end();
    } catch (_) {
      /* ignore */
    }
    stream = null;
  };
  return log;
}

async function getJson(url, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || HTTP_TIMEOUT_MS);
  try {
    const res = await fetch(url, { signal: controller.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}：${url}`);
    return await res.json();
  } finally {
    clearTimeout(timer);
  }
}

async function listTargets(port, log) {
  const url = `http://127.0.0.1:${port}/json/list`;
  const data = await getJson(url, HTTP_TIMEOUT_MS);
  if (!Array.isArray(data)) throw new Error('CDP /json/list 返回的不是数组');
  if (log) log(`GET ${url} → ${data.length} 个目标：${data.map((t) => t.type).join(',')}`);
  return data;
}

/** renderer 页面目标：type=page 且 url 是 renderer/index.html（排除 DevTools 等窗口）。 */
function isRendererTarget(target) {
  return !!(
    target &&
    target.type === 'page' &&
    typeof target.url === 'string' &&
    /index\.html/i.test(target.url) &&
    target.webSocketDebuggerUrl
  );
}

async function waitForRendererTarget(port, options) {
  const opts = options || {};
  const log = opts.log || null;
  const timeoutMs = opts.timeoutMs || TARGET_TIMEOUT_MS;
  const deadline = Date.now() + timeoutMs;
  let lastError = null;
  while (Date.now() < deadline) {
    if (typeof opts.shouldAbort === 'function' && opts.shouldAbort()) {
      throw new Error('等待 CDP 目标期间应用已退出');
    }
    try {
      const targets = await listTargets(port, null);
      const target = targets.find(isRendererTarget);
      if (target) {
        if (log) log(`找到 renderer 目标：${target.url} · ${target.id}`);
        return target;
      }
    } catch (err) {
      lastError = err;
    }
    await sleep(opts.intervalMs || TARGET_POLL_INTERVAL_MS);
  }
  throw new Error(
    `等待 http://127.0.0.1:${port}/json/list 出现 renderer 页面超时（${timeoutMs}ms）` +
      (lastError ? `；最后一次错误：${lastError.message}` : '')
  );
}

class CdpClient {
  constructor(ws, log) {
    this._ws = ws;
    this._log = log || null;
    this._seq = 0;
    this._pending = new Map();
    this._closed = false;
    ws.addEventListener('message', (event) => this._onMessage(event));
    ws.addEventListener('close', () => this._onClose('WebSocket 已关闭'));
    ws.addEventListener('error', () => this._onClose('WebSocket 错误'));
  }

  static async connect(target, options) {
    const opts = options || {};
    const log = opts.log || null;
    const timeoutMs = opts.timeoutMs || EVAL_TIMEOUT_MS;
    if (!target || !target.webSocketDebuggerUrl) throw new Error('CDP 目标缺少 webSocketDebuggerUrl');
    if (typeof WebSocket !== 'function') {
      throw new Error('当前 Node 不支持全局 WebSocket（需要 Node 22+）');
    }
    if (log) log(`连接 ${target.webSocketDebuggerUrl}`);
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('连接 CDP WebSocket 超时')), timeoutMs);
      ws.addEventListener('open', () => {
        clearTimeout(timer);
        resolve();
      });
      ws.addEventListener('error', () => {
        clearTimeout(timer);
        reject(new Error('连接 CDP WebSocket 失败（目标可能已关闭）'));
      });
    });
    return new CdpClient(ws, log);
  }

  _onMessage(event) {
    let message;
    try {
      message = JSON.parse(typeof event.data === 'string' ? event.data : String(event.data));
    } catch (_) {
      return;
    }
    if (!message || typeof message.id !== 'number') return;
    const entry = this._pending.get(message.id);
    if (!entry) return;
    this._pending.delete(message.id);
    clearTimeout(entry.timer);
    if (message.error) {
      const err = new Error(`${message.error.message || 'CDP 错误'}${message.error.data ? `：${message.error.data}` : ''}`);
      if (this._log) this._log(`← #${message.id} 错误：${err.message}`);
      entry.reject(err);
      return;
    }
    entry.resolve(message.result);
  }

  _onClose(reason) {
    if (this._closed) return;
    this._closed = true;
    for (const [id, entry] of this._pending) {
      clearTimeout(entry.timer);
      entry.reject(new Error(`${reason}（未收到 #${id} 响应）`));
    }
    this._pending.clear();
  }

  send(method, params, options) {
    const opts = options || {};
    if (this._closed) return Promise.reject(new Error('CDP 连接已关闭'));
    const id = (this._seq += 1);
    const timeoutMs = opts.timeoutMs || EVAL_TIMEOUT_MS;
    const payload = { id, method, params: params || {} };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`CDP ${method} 超时（${timeoutMs}ms）`));
      }, timeoutMs);
      this._pending.set(id, { resolve, reject, timer });
      if (this._log) this._log(`→ #${id} ${method} ${summarize(params)}`);
      try {
        this._ws.send(JSON.stringify(payload));
      } catch (err) {
        clearTimeout(timer);
        this._pending.delete(id);
        reject(err);
      }
    });
  }

  /** 在页面里执行表达式；结果为值（returnByValue），异常抛 Error。 */
  async evaluate(expression, options) {
    const opts = options || {};
    const result = await this.send(
      'Runtime.evaluate',
      {
        expression,
        awaitPromise: true,
        returnByValue: true,
        userGesture: false,
        includeCommandLineAPI: false,
      },
      { timeoutMs: opts.timeoutMs || EVAL_TIMEOUT_MS }
    );
    if (result && result.exceptionDetails) {
      const details = result.exceptionDetails;
      const description =
        (details.exception && (details.exception.description || details.exception.value)) ||
        details.text ||
        '页面表达式抛出异常';
      const message = String(description).split('\n')[0].slice(0, 400);
      if (this._log) this._log(`← evaluate 异常：${message}`);
      throw new Error(message);
    }
    const value = result && result.result ? result.result.value : undefined;
    if (this._log && opts.logResult !== false) this._log(`← evaluate → ${summarize(value)}`);
    return value;
  }

  close() {
    try {
      if (!this._closed) this._ws.close();
    } catch (_) {
      /* ignore */
    }
    this._onClose('主动关闭');
  }
}

/** 尽力优雅关闭窗口（触发 window-all-closed → 主进程清理 sidecar 后退出）。 */
async function tryCloseWindow(client, options) {
  const opts = options || {};
  const log = opts.log || null;
  try {
    await client.send('Runtime.evaluate', { expression: 'window.close()' }, { timeoutMs: opts.timeoutMs || 2000 });
    if (log) log('已请求 window.close()');
    return true;
  } catch (err) {
    if (log) log(`window.close() 未拿到响应（多数情况下窗口已关闭）：${err.message}`);
    return false;
  }
}

function summarize(value) {
  if (typeof value === 'string') return value.length > 160 ? `${value.slice(0, 160)}…` : value;
  try {
    const text = JSON.stringify(value);
    if (text === undefined) return String(value);
    return text.length > 200 ? `${text.slice(0, 200)}…` : text;
  } catch (_) {
    return String(value);
  }
}

module.exports = {
  sleep,
  createLogger,
  getJson,
  listTargets,
  isRendererTarget,
  waitForRendererTarget,
  tryCloseWindow,
  redact,
  CdpClient,
};

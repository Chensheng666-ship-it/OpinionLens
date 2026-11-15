/* =============================================================================
   e2e · 渲染进程冒烟（通过 CDP 驱动真实 Electron 窗口）
   断言（默认 full 模式 / -NoSidecar 模式都要绿，sidecar 相关项在 no-sidecar 下记 SKIP）：
     0  窗口就绪：full → FS.mode=real；no-sidecar → FS.mode=offline + 未就绪页
     2  fsapi.getInfo() 返回 appVersion/dataDir/dataDirDefault/userConfigPath/sidecar
     3  设置→关于 渲染出「检查更新」「数据目录」两张卡
     4  migrateDataDir 校验：相对路径 / 与当前目录相同
     5  feed 增量参数非法：since_seq=-1 → HTTP 400 invalid_params（无 sidecar 记 SKIP）
     6  preview.js 未随主包加载（无静态 script，正式模式无 FS.PreviewApi）
     7  ui-kit 生效（FS.ui.numOr('abc', 7) === 7 等）
     8  连续切页后 FS.timers.size() 不持续增长
     1  全程无未捕获 JS 错误（window.onerror 收集器为空）
   用法：node e2e_smoke.js <cdp-port>
   环境变量：E2E_MODE=no-sidecar|full（默认 full）、E2E_CDP_LOG、E2E_RESULT_PATH
   ========================================================================== */
'use strict';

const fs = require('fs');
const path = require('path');
const cdp = require('./cdp');

const port = Number(process.argv[2]);
const MODE = process.env.E2E_MODE === 'no-sidecar' ? 'no-sidecar' : 'full';
const RESULT_PATH = process.env.E2E_RESULT_PATH || '';
const LOG_PATH = process.env.E2E_CDP_LOG || '';
const log = cdp.createLogger(LOG_PATH);

const results = [];
let client = null;

function record(status, name, detail) {
  results.push({ status, name, detail: detail || '' });
  console.log(`[${status}] ${name}${detail ? ` — ${detail}` : ''}`);
}

async function check(name, fn) {
  try {
    const out = await fn();
    if (out && out.skip) record('SKIP', name, out.detail || '');
    else record('PASS', name, (out && out.detail) || '');
  } catch (err) {
    record('FAIL', name, (err && err.message) || String(err));
  }
}

async function waitFor(expression, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await client.evaluate(`(function(){ try { return !!(${expression}); } catch (e) { throw e; } })()`);
      if (last === true) return true;
    } catch (err) {
      last = `evaluate 失败：${err.message}`;
    }
    await cdp.sleep(250);
  }
  throw new Error(`等待超时（${timeoutMs}ms）：${label}；最后状态=${String(last)}`);
}

function tail(text, max) {
  const str = String(text || '');
  return str.length > (max || 300) ? `${str.slice(0, max || 300)}…` : str;
}

async function main() {
  const target = await cdp.waitForRendererTarget(port, { log, timeoutMs: 30000 });
  client = await cdp.CdpClient.connect(target, { log });

  // 先装错误收集器：后续所有交互期间的未捕获错误都会被记录
  await client.evaluate(`(function () {
    if (!window.__e2eErrors) {
      window.__e2eErrors = [];
      window.addEventListener('error', function (event) {
        window.__e2eErrors.push({
          message: String((event && event.message) || (event && event.type) || 'unknown'),
          source: String((event && event.filename) || ''),
          line: (event && event.lineno) || 0,
          column: (event && event.colno) || 0
        });
      });
    }
    return true;
  })()`);

  /* ---------------------------------------------------------------- 0 就绪 */
  await check('窗口就绪且连接状态符合模式', async () => {
    const expected = MODE === 'no-sidecar' ? 'offline' : 'real';
    await waitFor(`FS.mode === '${expected}'`, MODE === 'no-sidecar' ? 25000 : 45000, `FS.mode === '${expected}'`);
    if (MODE === 'no-sidecar') {
      const readiness = await client.evaluate(`!!document.querySelector('.readiness')`);
      if (!readiness) throw new Error('no-sidecar 模式下未渲染「本地服务未就绪」引导页');
      return { detail: `mode=${expected} · 引导页已渲染` };
    }
    return { detail: `mode=${expected} · sidecar 已连接` };
  });

  /* -------------------------------------------------------------- 2 getInfo */
  await check('fsapi.getInfo() 字段齐全', async () => {
    const info = await client.evaluate('window.fsapi.getInfo()');
    if (!info || typeof info !== 'object') throw new Error('getInfo 未返回对象');
    const required = ['appVersion', 'dataDir', 'dataDirDefault', 'userConfigPath', 'sidecar'];
    const missing = required.filter((key) => info[key] === undefined || info[key] === null || info[key] === '');
    if (missing.length) throw new Error(`缺少字段：${missing.join(', ')}`);
    if (!info.sidecar || typeof info.sidecar !== 'object') throw new Error('sidecar 字段不是对象');
    if (typeof info.sidecar.ok !== 'boolean' || typeof info.sidecar.skipped !== 'boolean') {
      throw new Error('sidecar 字段缺少 ok/skipped 布尔值');
    }
    return {
      detail: `appVersion=${info.appVersion} · dataDir=${tail(info.dataDir, 80)} · sidecar.ok=${info.sidecar.ok} · sidecar.skipped=${info.sidecar.skipped}`,
    };
  });

  /* -------------------------------------------- 3 设置 → 关于（两张卡） */
  await check('设置 → 关于：检查更新 / 数据目录 两张卡已渲染', async () => {
    // full 模式走路由；no-sidecar 下 applyPage 固定渲染引导页，直接挂载设置视图（同一套 render 代码）
    const mount = await client.evaluate(`(function () {
      if (FS.mode === 'real' || FS.mode === 'preview') { FS.go('settings'); return 'route'; }
      FS.views.settings.render(document.getElementById('view'));
      return 'direct';
    })()`);
    await waitFor(`document.querySelector('[data-settings-tab="about"]')`, 10000, '设置页导航');
    await client.evaluate(`(function () {
      var tabBtn = document.querySelector('[data-settings-tab="about"]');
      if (!tabBtn) throw new Error('未找到「关于」tab');
      tabBtn.click();
      return true;
    })()`);
    await waitFor(`document.querySelector('[data-settings-act="check-update"]')`, 10000, '关于面板');
    const probe = await client.evaluate(`(function () {
      return {
        check: !!document.querySelector('[data-settings-act="check-update"]'),
        dataDir: !!document.querySelector('[data-settings-act="choose-data-dir"]'),
        tabs: document.querySelectorAll('[data-settings-tab]').length
      };
    })()`);
    if (!probe.check) throw new Error('缺少「检查更新」卡片按钮');
    if (!probe.dataDir) throw new Error('缺少「数据目录」卡片按钮');
    return { detail: `mount=${mount} · tabs=${probe.tabs} · 两张卡存在` };
  });

  /* -------------------------------------------- 4 migrateDataDir 参数校验 */
  await check('migrateDataDir 参数校验（相对路径 / 与当前目录相同）', async () => {
    const relative = await client.evaluate(`window.fsapi.migrateDataDir('relative')`);
    if (!relative || relative.ok !== false) throw new Error(`相对路径未被拒绝：${JSON.stringify(relative)}`);
    if (!/绝对路径/.test(String(relative.error || ''))) throw new Error(`相对路径错误文案不含「绝对路径」：${relative.error}`);

    const same = await client.evaluate(`(function () {
      return window.fsapi.getInfo().then(function (info) {
        return window.fsapi.migrateDataDir(info.dataDir);
      });
    })()`);
    if (!same || same.ok !== false) throw new Error(`当前数据目录未被拒绝：${JSON.stringify(same)}`);
    if (!/相同/.test(String(same.error || ''))) throw new Error(`同目录错误文案不含「相同」：${same.error}`);
    return { detail: `relative→${relative.error} · same→${same.error}` };
  });

  /* ------------------------------------------------- 5 feed 增量参数 400 */
  await check('feed 增量参数校验：since_seq=-1 → 400 invalid_params', async () => {
    const hasSidecar = await client.evaluate(`!!(FS.info && FS.info.baseUrl && FS.info.token)`);
    if (!hasSidecar) {
      if (MODE === 'full') throw new Error('full 模式已连接但 FS.info.baseUrl/token 为空');
      return { skip: true, detail: 'no-sidecar：无 baseUrl，跳过' };
    }
    const res = await client.evaluate(`(async function () {
      var r = await fetch(FS.info.baseUrl + '/api/runs/__e2e__/feed?since_seq=-1', {
        headers: { Authorization: 'Bearer ' + FS.info.token }
      });
      var body = null;
      try { body = await r.json(); } catch (e) { body = null; }
      return {
        status: r.status,
        code: body && body.error ? body.error.code : null,
        message: body && body.error ? body.error.message : ''
      };
    })()`);
    if (!res || res.status !== 400) throw new Error(`HTTP ${res && res.status}（期望 400）`);
    if (res.code !== 'invalid_params') throw new Error(`error.code=${res.code}（期望 invalid_params）`);
    return { detail: `HTTP 400 · ${res.code} · ${tail(res.message, 60)}` };
  });

  /* ------------------------------------------------- 6 preview.js 未加载 */
  await check('preview.js 未随主包加载（正式模式零成本）', async () => {
    const probe = await client.evaluate(`(function () {
      return {
        tags: document.querySelectorAll('script[src="preview.js"]').length,
        previewApi: typeof FS.PreviewApi,
        mode: FS.mode,
        scripts: Array.prototype.map.call(document.querySelectorAll('script[src]'), function (s) { return s.getAttribute('src'); })
      };
    })()`);
    if (probe.tags !== 0) throw new Error(`存在 ${probe.tags} 个静态 preview.js script 标签`);
    if (probe.previewApi !== 'undefined') throw new Error('FS.PreviewApi 已定义（mock 被加载）');
    if (probe.mode === 'preview') throw new Error('误入静态预览模式');
    return { detail: `scripts=${probe.scripts.join(',')} · FS.PreviewApi=undefined` };
  });

  /* ------------------------------------------------------- 7 ui-kit 生效 */
  await check('ui-kit 生效（FS.ui.*）', async () => {
    const probe = await client.evaluate(`(function () {
      try {
        if (!FS.ui || typeof FS.ui !== 'object') return { ok: false, reason: 'FS.ui 不存在' };
        var checks = {
          numOrFallback: FS.ui.numOr('abc', 7) === 7,
          numOrNumber: FS.ui.numOr('12', 7) === 12,
          strOr: typeof FS.ui.strOr === 'function',
          apiErrNote: typeof FS.ui.apiErrNote === 'function',
          bannerHtml: typeof FS.ui.bannerHtml === 'function',
          fieldHtml: typeof FS.ui.fieldHtml === 'function',
          selectFieldHtml: typeof FS.ui.selectFieldHtml === 'function',
          chip: typeof FS.ui.chip === 'function'
        };
        var bad = Object.keys(checks).filter(function (k) { return !checks[k]; });
        return { ok: bad.length === 0, bad: bad };
      } catch (e) {
        return { ok: false, reason: String(e && e.message) };
      }
    })()`);
    if (!probe || !probe.ok) throw new Error(`ui-kit 校验失败：${JSON.stringify(probe)}`);
    return { detail: `numOr('abc',7)=7 · 9 个工具函数可用` };
  });

  /* ------------------------------------------------ 8 切页后定时器不增长 */
  await check('连续切页后 FS.timers.size() 不持续增长', async () => {
    const probe = await client.evaluate(`(async function () {
      function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
      var sizes = [];
      FS.go('tasks'); await wait(800); sizes.push(FS.timers.size());
      FS.go('settings'); await wait(800); sizes.push(FS.timers.size());
      FS.go('tasks'); await wait(800); sizes.push(FS.timers.size());
      var keep = ['conn-health', 'conn-loss-retry', 'conn-countdown', 'conn-retry'].filter(function (label) {
        return FS.timers.isPersistent(label);
      });
      return { sizes: sizes, whitelist: keep.length };
    })()`);
    const sizes = (probe && probe.sizes) || [];
    if (sizes.length !== 3) throw new Error(`切页采样异常：${JSON.stringify(probe)}`);
    const max = Math.max.apply(null, sizes);
    const min = Math.min.apply(null, sizes);
    if (max - min > 1) throw new Error(`定时器数量随切页增长：${sizes.join(' → ')}`);
    if (max > 5) throw new Error(`定时器数量过高：${sizes.join(' → ')}`);
    return { detail: `sizes=${sizes.join(' → ')} · 白名单 label=${probe.whitelist}` };
  });

  /* --------------------------------------------- 1 全程无未捕获 JS 错误 */
  await check('全程无未捕获 JS 错误（window.onerror）', async () => {
    const errors = await client.evaluate('(window.__e2eErrors || []).slice(0, 5)');
    if (!Array.isArray(errors)) throw new Error('错误收集器未安装');
    if (errors.length) throw new Error(`${errors.length} 条未捕获错误：${tail(JSON.stringify(errors), 400)}`);
    return { detail: '0 条未捕获错误' };
  });

  return results;
}

(async () => {
  const startedAt = Date.now();
  let fatal = '';
  try {
    await main();
  } catch (err) {
    fatal = (err && err.message) || String(err);
    record('FAIL', 'e2e 执行阶段', fatal);
  } finally {
    if (client) {
      await cdp.tryCloseWindow(client, { log });
      client.close();
    }
    const counts = results.reduce(
      (acc, item) => {
        acc[item.status.toLowerCase()] += 1;
        return acc;
      },
      { pass: 0, fail: 0, skip: 0 }
    );
    const payload = {
      mode: MODE,
      port,
      startedAt: new Date(startedAt).toISOString(),
      durationMs: Date.now() - startedAt,
      counts,
      fatal,
      results,
    };
    if (RESULT_PATH) {
      try {
        fs.mkdirSync(path.dirname(RESULT_PATH), { recursive: true });
        fs.writeFileSync(RESULT_PATH, `${JSON.stringify(payload, null, 2)}\n`, 'utf8');
      } catch (err) {
        console.error(`[WARN] 写入结果文件失败：${err.message}`);
      }
    }
    console.log(
      `\n== e2e summary == mode=${MODE} pass=${counts.pass} fail=${counts.fail} skip=${counts.skip}` +
        (fatal ? ` fatal=${fatal}` : '')
    );
    log(`e2e 结束：${JSON.stringify(counts)}${fatal ? ` fatal=${fatal}` : ''}`);
    log.close();
    process.exitCode = counts.fail > 0 || fatal ? 1 : 0;
  }
})();

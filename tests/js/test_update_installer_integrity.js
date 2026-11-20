// =============================================================================
// tests/js/test_update_installer_integrity.js
//
// 桌面更新安装包必须在启动前通过已固定的发布者签名或摘要校验。
// 这些断言在漏洞还在时失败，修复后通过。
//
// 运行（不要访问真实 GitHub；脚本把更新域名解析到本机 HTTPS 替身）：
//   node tests/js/test_update_installer_integrity.js
// 产品请求的是 https 默认端口，脚本会 sudo -n 绑定 127.0.0.1:443。
// 修复前本文件以退出码 1 结束。
//
// 替身说明：
// - Electron 45.2.3 的 shell.openPath 在本环境无法加载 Windows 实现。
//   替身按已阅读的源码复现调用链，不另加判断：
//   electron/shell/common/api/electron_api_shell.cc OpenPath（约 75-86 行）
//   → shell/common/platform_util_win.cc OpenPath / OpenPathOnThread（358-364、315-325 行）
//   → chromium 156.0.8078.80 ui/base/win/shell.cc OpenFileViaShell（95-99 行）
//   → InvokeShellExecute（43-90 行）以空 verb 调用 ShellExecuteExW。
// - Node https.get（Electron 所带 Node v24.22.2 lib/https.js get，约 732 行）
//   本身不跟随 3xx；跟随重定向的是 app/desktop/main.js 的 downloadToFile / httpGetJson。
// =============================================================================
'use strict';

const assert = require('node:assert');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const https = require('node:https');
const os = require('node:os');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const SETUP_NAME = 'OpinionLens-Setup-9.9.9.exe';
const OTHER_NAME = 'Other-Setup-9.9.9.exe';
const EVIL_ASSET_NAME = 'Evil-Setup-9.9.9.exe';
const MARKER_OFFICIAL = 'OFFICIAL-UNSIGNED-INSTALLER';
const MARKER_OTHER = 'SUBSTITUTED-URL-INSTALLER';
const MARKER_EVIL = 'OFF-ALLOWLIST-REDIRECT-INSTALLER';
const MARKER_API = 'API-REDIRECT-INSTALLER';
const MARKER_SWAP = 'SWAPPED-AFTER-CHECK';

if (process.env.OL_UPDATE_TEST_CHILD !== '1') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ol-update-ca-'));
  const caKey = path.join(dir, 'ca.key');
  const caCrt = path.join(dir, 'ca.crt');
  const srvKey = path.join(dir, 'srv.key');
  const srvCsr = path.join(dir, 'srv.csr');
  const srvCrt = path.join(dir, 'srv.crt');
  const san = path.join(dir, 'san.cnf');
  fs.writeFileSync(san, [
    '[req]',
    'distinguished_name = dn',
    'prompt = no',
    '[dn]',
    'CN = github.com',
    '[san]',
    'subjectAltName = DNS:github.com,DNS:api.github.com,DNS:objects.githubusercontent.com,DNS:githubusercontent.com,DNS:codeload.github.com,DNS:evil.example,DNS:release-assets.githubusercontent.com',
    'basicConstraints = CA:FALSE',
    'keyUsage = digitalSignature,keyEncipherment',
    'extendedKeyUsage = serverAuth',
    '',
  ].join('\n'));
  const run = (args) => {
    const result = spawnSync('openssl', args, { encoding: 'utf8' });
    if (result.status !== 0) {
      console.error(result.stderr || result.stdout);
      process.exit(2);
    }
  };
  run(['req', '-x509', '-newkey', 'rsa:2048', '-keyout', caKey, '-out', caCrt, '-days', '2', '-nodes', '-subj', '/CN=OpinionLens Update Test CA']);
  run(['req', '-newkey', 'rsa:2048', '-keyout', srvKey, '-out', srvCsr, '-nodes', '-subj', '/CN=github.com']);
  run(['x509', '-req', '-in', srvCsr, '-CA', caCrt, '-CAkey', caKey, '-CAcreateserial', '-out', srvCrt, '-days', '2', '-extfile', san, '-extensions', 'san']);
  const child = spawnSync(process.execPath, [__filename], {
    env: {
      ...process.env,
      NODE_EXTRA_CA_CERTS: caCrt,
      OL_UPDATE_TEST_CHILD: '1',
      OL_UPDATE_TEST_CERT: srvCrt,
      OL_UPDATE_TEST_KEY: srvKey,
    },
    stdio: 'inherit',
  });
  process.exit(child.status == null ? 2 : child.status);
}

// 产品把更新地址写死为 https 默认端口。监听 443 才能让真实的 https.get 走到替身。
if (typeof process.getuid === 'function' && process.getuid() !== 0) {
  const elevated = spawnSync('sudo', ['-n', '-E', process.execPath, __filename], {
    env: process.env,
    stdio: 'inherit',
  });
  process.exit(elevated.status == null ? 2 : elevated.status);
}

const TEST_HOSTS = new Set([
  'github.com',
  'api.github.com',
  'objects.githubusercontent.com',
  'githubusercontent.com',
  'codeload.github.com',
  'evil.example',
  'release-assets.githubusercontent.com',
]);

function installLookupGuard() {
  const dns = require('node:dns');
  const original = dns.lookup;
  function lookup(hostname, options, callback) {
    if (typeof options === 'function') {
      callback = options;
      options = {};
    }
    const host = String(hostname || '').replace(/[\[\]]/g, '').toLowerCase();
    if (host === 'localhost' || host === '127.0.0.1' || host === '::1') {
      return original.call(dns, hostname, options, callback);
    }
    if (!TEST_HOSTS.has(host)) {
      const error = new Error('blocked outbound lookup: ' + host);
      error.code = 'ENOTFOUND';
      callback(error);
      return;
    }
    const all = options && typeof options === 'object' && options.all;
    if (all) callback(null, [{ address: '127.0.0.1', family: 4 }]);
    else callback(null, '127.0.0.1', 4);
  }
  dns.lookup = lookup;
}

function mz(marker) {
  return Buffer.concat([Buffer.from('MZ'), Buffer.from(marker, 'utf8')]);
}

function updateDir() {
  return path.join(os.tmpdir(), 'OpinionLens-update');
}

function resetUpdateDir() {
  fs.rmSync(updateDir(), { recursive: true, force: true });
}

function fileContains(filePath, marker) {
  try {
    return fs.readFileSync(filePath).includes(Buffer.from(marker, 'utf8'));
  } catch (_) {
    return false;
  }
}

function anyUpdateFileContains(marker) {
  const root = updateDir();
  if (!fs.existsSync(root)) return false;
  for (const name of fs.readdirSync(root)) {
    if (fileContains(path.join(root, name), marker)) return true;
  }
  return false;
}

function isTransportError(message) {
  const text = String(message || '');
  return /ECONN|ENOTFOUND|ETIMEDOUT|EPROTO|CERT_|certificate|unable to verify|socket hang up|超时|HTTP 5|HTTP 404/i.test(text);
}

/**
 * shell.openPath 的 Windows 实现（Electron 45.2.3 + Chromium 156.0.8078.80）。
 * 目录走 OpenFolderViaShell；文件以空 verb 交给 ShellExecuteExW。
 * 返回空字符串表示 ShellExecuteExW 成功（platform_util_win.cc OpenPathOnThread）。
 */
function openPathLikeWindows(fullPath) {
  const normalized = path.win32.normalize(String(fullPath).replace(/\//g, '\\'));
  let stat;
  try {
    stat = fs.statSync(fullPath);
  } catch (_) {
    return 'Failed to open path';
  }
  if (stat.isDirectory()) {
    return '';
  }
  openPathLikeWindows.executed.push({
    api: 'ShellExecuteExW',
    verb: null,
    mask: 'SEE_MASK_NOASYNC',
    path: normalized,
    sourcePath: path.resolve(fullPath),
  });
  return '';
}
openPathLikeWindows.executed = [];

function createElectronMock() {
  const handlers = new Map();
  const webContents = () => ({
    setWindowOpenHandler() {},
    on() {},
    once() {},
    loadFile() { return Promise.resolve(); },
    executeJavaScript() { return Promise.resolve({}); },
    capturePage() { return Promise.resolve({ isEmpty: () => true, toPNG: () => Buffer.alloc(0) }); },
    openDevTools() {},
    send() {},
  });
  const electron = {
    app: {
      isPackaged: false,
      getVersion: () => '0.7.0',
      getPath: () => process.env.APPDATA,
      commandLine: { appendSwitch() {} },
      setPath() {},
      setAppUserModelId() {},
      requestSingleInstanceLock: () => true,
      quit() {},
      exit() {},
      relaunch() {},
      disableHardwareAcceleration() {},
      whenReady() { return Promise.resolve(); },
      on() {},
    },
    BrowserWindow: function BrowserWindow() {
      return {
        webContents: webContents(),
        once() {},
        on() {},
        show() {},
        getSize: () => [1400, 900],
        isMinimized: () => false,
        restore() {},
        focus() {},
      };
    },
    Menu: { setApplicationMenu() {} },
    dialog: {
      showOpenDialog: async () => ({ canceled: true, filePaths: [] }),
    },
    ipcMain: {
      handle(channel, fn) { handlers.set(channel, fn); },
    },
    shell: {
      openPath(target) { return Promise.resolve(openPathLikeWindows(target)); },
      showItemInFolder() {},
      openExternal() { return Promise.resolve(); },
    },
  };
  return { electron, handlers };
}

function releaseJson(assetName) {
  const base = 'https://github.com';
  return JSON.stringify({
    tag_name: 'v9.9.9',
    name: 'v9.9.9',
    body: 'release notes',
    html_url: 'https://github.com/SolitudeZY/OpinionLens/releases/tag/v9.9.9',
    published_at: '2026-01-01T00:00:00Z',
    assets: [
      { name: 'latest.yml', size: 12, browser_download_url: `${base}/latest.yml` },
      {
        name: assetName,
        size: 64,
        browser_download_url: `${base}/SolitudeZY/OpinionLens/releases/download/v9.9.9/${assetName}`,
      },
      {
        name: 'OpinionLens-Setup-9.9.9.exe.blockmap',
        size: 8,
        browser_download_url: `${base}/blockmap`,
      },
      {
        name: OTHER_NAME,
        size: 32,
        browser_download_url: `${base}/SolitudeZY/OpinionLens/releases/download/v9.9.9/${OTHER_NAME}`,
      },
    ],
  });
}

function startServer() {
  const state = { mode: 'objects' };
  const hits = [];
  const cert = fs.readFileSync(process.env.OL_UPDATE_TEST_CERT);
  const key = fs.readFileSync(process.env.OL_UPDATE_TEST_KEY);
  const server = https.createServer({ cert, key }, (req, res) => {
    const host = String(req.headers.host || '').split(':')[0].toLowerCase();
    const pathname = decodeURIComponent(new URL(req.url, 'https://localhost').pathname);
    hits.push(host + pathname);
    const redirect = (location) => {
      res.writeHead(302, { Location: location, 'Content-Length': '0' });
      res.end();
    };
    const ok = (body, type) => {
      const buf = Buffer.isBuffer(body) ? body : Buffer.from(body);
      res.writeHead(200, {
        'Content-Type': type || 'application/octet-stream',
        'Content-Length': String(buf.length),
      });
      res.end(buf);
    };
    if (pathname === '/health') return ok('ok', 'text/plain');
    if (host === 'api.github.com' && pathname === '/repos/SolitudeZY/OpinionLens/releases/latest') {
      if (state.mode === 'api-redirect') {
        return redirect('https://evil.example/evil-release.json');
      }
      return ok(releaseJson(SETUP_NAME), 'application/json');
    }
    if (host === 'evil.example' && pathname === '/evil-release.json') {
      return ok(releaseJson(EVIL_ASSET_NAME), 'application/json');
    }
    if (pathname.endsWith('/' + SETUP_NAME) && host === 'github.com') {
      if (state.mode === 'redirect-evil') {
        return redirect('https://evil.example/evil-payload.exe');
      }
      if (state.mode === 'html') return ok('<html>not a pe</html>', 'text/html');
      return redirect(`https://objects.githubusercontent.com/objects/${SETUP_NAME}`);
    }
    if (host === 'objects.githubusercontent.com' && pathname === '/objects/' + SETUP_NAME) {
      return ok(mz(MARKER_OFFICIAL));
    }
    if (host === 'evil.example' && pathname === '/evil-payload.exe') {
      return ok(mz(MARKER_EVIL));
    }
    if (pathname.endsWith('/' + OTHER_NAME)) return ok(mz(MARKER_OTHER));
    if (pathname.endsWith('/' + EVIL_ASSET_NAME)) return ok(mz(MARKER_API));
    res.writeHead(404, { 'Content-Length': '0' });
    res.end();
  });
  return new Promise((resolve, reject) => {
    server.on('error', reject);
    server.listen(443, '127.0.0.1', () => {
      resolve({ server, state, hits });
    });
  });
}

function directGet(url) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, body: Buffer.concat(chunks), location: res.headers.location || '' }));
    });
    req.on('error', reject);
  });
}

async function loadMain() {
  const dataRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'ol-update-data-'));
  const appData = path.join(dataRoot, 'appdata');
  const dataDir = path.join(dataRoot, 'data');
  fs.mkdirSync(path.join(appData, 'OpinionLens'), { recursive: true });
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(
    path.join(appData, 'OpinionLens', 'user-config.json'),
    JSON.stringify({ dataDir }) + '\n'
  );
  process.env.APPDATA = appData;
  delete process.env.FS_UPDATE_REPO;
  process.argv = [process.execPath, path.join(REPO_ROOT, 'app', 'desktop', 'main.js'), '--no-sidecar'];
  const { electron, handlers } = createElectronMock();
  const Module = require('node:module');
  const original = Module.prototype.require;
  Module.prototype.require = function patched(id) {
    if (id === 'electron') return electron;
    return original.apply(this, arguments);
  };
  require(path.join(REPO_ROOT, 'app', 'desktop', 'main.js'));
  Module.prototype.require = original;
  const deadline = Date.now() + 5000;
  while (!handlers.has('app:runUpdateInstaller')) {
    if (Date.now() > deadline) throw new Error('主进程没有注册 app:runUpdateInstaller');
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  const downloadSource = String(handlers.get('app:downloadUpdate'));
  const runSource = String(handlers.get('app:runUpdateInstaller'));
  assert.ok(downloadSource.includes('MZ'), '加载到的不是 main.js 的 app:downloadUpdate');
  assert.ok(runSource.includes('openPath'), '加载到的不是 main.js 的 app:runUpdateInstaller');
  return async function invoke(channel, payload) {
    const handler = handlers.get(channel);
    if (!handler) throw new Error('缺少 IPC: ' + channel);
    return handler({ sender: { send() {} } }, payload);
  };
}

let passed = 0;
const failures = [];

async function check(name, fn) {
  openPathLikeWindows.executed.length = 0;
  resetUpdateDir();
  try {
    await fn();
    passed += 1;
    console.log('  [PASS] ' + name);
  } catch (error) {
    failures.push({ name, message: error && error.message });
    console.log('  [FAIL] ' + name);
    console.log('         ' + (error && error.stack ? error.stack : error));
  }
}

function launches() {
  return openPathLikeWindows.executed.slice();
}

async function main() {
  installLookupGuard();
  const { server, state } = await startServer();
  const invoke = await loadMain();
  const setupUrl = `https://github.com/SolitudeZY/OpinionLens/releases/download/v9.9.9/${SETUP_NAME}`;

  console.log('===== 更新安装包完整性（应拒绝未签名 / 未绑定摘要的安装包） =====');

  const health = await directGet('https://evil.example/health');
  assert.strictEqual(health.status, 200, '测试 HTTPS 替身不可用');
  const probed = await directGet(setupUrl);
  assert.strictEqual(probed.status, 302);
  assert.ok(String(probed.location).includes('objects.githubusercontent.com'), probed.location);

  await check('非允许域名与临时目录之外的路径仍被拒绝', async () => {
    const denied = await invoke('app:downloadUpdate', { url: 'http://github.com/a.exe', name: 'a.exe' });
    assert.strictEqual(denied.ok, false);
    const outside = path.join(os.tmpdir(), 'ol-not-update-dir', 'a.exe');
    fs.mkdirSync(path.dirname(outside), { recursive: true });
    fs.writeFileSync(outside, mz(MARKER_OFFICIAL));
    const run = await invoke('app:runUpdateInstaller', outside);
    assert.strictEqual(run.ok, false);
    assert.strictEqual(launches().length, 0);
    fs.rmSync(path.dirname(outside), { recursive: true, force: true });
  });

  await check('非 MZ 内容被拒绝且不留下文件', async () => {
    state.mode = 'html';
    const info = await invoke('app:checkUpdate');
    assert.strictEqual(info.ok, true);
    const dl = await invoke('app:downloadUpdate', { url: info.download.url, name: info.download.name });
    assert.strictEqual(dl.ok, false, JSON.stringify(dl));
    assert.ok(!isTransportError(dl.error), dl.error);
    assert.strictEqual(anyUpdateFileContains('not a pe'), false);
    assert.strictEqual(launches().length, 0);
  });

  await check('最新 Release 的 Setup.exe 仅有 MZ 头时不得启动', async () => {
    state.mode = 'objects';
    const appInfo = await invoke('app:info');
    assert.strictEqual(appInfo.updateRepo, 'SolitudeZY/OpinionLens');
    const info = await invoke('app:checkUpdate');
    assert.strictEqual(info.ok, true, JSON.stringify(info));
    assert.strictEqual(info.hasUpdate, true);
    assert.strictEqual(info.download && info.download.name, SETUP_NAME);
    assert.strictEqual(info.download.url, setupUrl);
    const dl = await invoke('app:downloadUpdate', { url: info.download.url, name: info.download.name });
    if (!dl.ok && isTransportError(dl.error)) {
      assert.fail('安装包请求没有到达替身：' + dl.error);
    }
    let run = { ok: false };
    if (dl.ok) {
      assert.ok(fileContains(dl.path, MARKER_OFFICIAL), '下载结果不是替身返回的安装包');
      assert.ok(fs.readFileSync(dl.path).subarray(0, 2).toString('latin1') === 'MZ');
      run = await invoke('app:runUpdateInstaller', dl.path);
    }
    const launched = launches();
    if (launched.length > 0) {
      assert.fail('未签名安装包被交给 ShellExecuteExW：' + launched.map((item) => item.sourcePath).join(', ') + ' run=' + JSON.stringify(run));
    }
    assert.notStrictEqual(run.ok, true, '运行安装包返回了成功');
    if (dl.ok === false) assert.ok(!isTransportError(dl.error), dl.error);
    assert.strictEqual(anyUpdateFileContains(MARKER_OFFICIAL), false, '拒绝前已把未签名安装包写入更新目录');
  });

  await check('允许名单上的地址跳转到名单外主机时不得保存或启动', async () => {
    state.mode = 'redirect-evil';
    const info = await invoke('app:checkUpdate');
    assert.strictEqual(info.download.url, setupUrl);
    const dl = await invoke('app:downloadUpdate', { url: info.download.url, name: info.download.name });
    if (!dl.ok && isTransportError(dl.error)) assert.fail('重定向请求没有走完 TLS：' + dl.error);
    if (dl.ok) {
      const run = await invoke('app:runUpdateInstaller', dl.path);
      if (launches().length > 0 || run.ok === true) {
        assert.fail('名单外重定向的安装包被启动：' + JSON.stringify({ run, launches: launches(), path: dl.path }));
      }
    }
    assert.strictEqual(launches().length, 0, JSON.stringify(launches()));
    assert.notStrictEqual(dl.ok, true, '名单外重定向被当成安装包接受：' + JSON.stringify(dl));
    assert.strictEqual(anyUpdateFileContains(MARKER_EVIL), false, '名单外重定向的内容已写入磁盘');
    if (dl.ok === false && isTransportError(dl.error)) assert.fail(dl.error);
  });

  await check('下载地址必须是本次检查更新选中的资产，不能换成同仓库另一个 exe', async () => {
    state.mode = 'objects';
    const info = await invoke('app:checkUpdate');
    assert.strictEqual(info.download.url, setupUrl);
    const otherUrl = setupUrl.replace(SETUP_NAME, OTHER_NAME);
    const dl = await invoke('app:downloadUpdate', { url: otherUrl, name: OTHER_NAME });
    if (!dl.ok && isTransportError(dl.error)) assert.fail(dl.error);
    if (dl.ok) await invoke('app:runUpdateInstaller', dl.path);
    assert.strictEqual(anyUpdateFileContains(MARKER_OTHER), false, '未选中的安装包被写入更新目录');
    assert.strictEqual(launches().some((item) => fileContains(item.sourcePath, MARKER_OTHER) || String(item.sourcePath).includes(OTHER_NAME)), false);
    assert.notStrictEqual(dl.ok === true && fileContains(dl.path, MARKER_OTHER), true);
  });

  await check('检查通过后、启动前替换文件内容仍不得启动', async () => {
    state.mode = 'objects';
    const info = await invoke('app:checkUpdate');
    const dl = await invoke('app:downloadUpdate', { url: info.download.url, name: info.download.name });
    if (!dl.ok && isTransportError(dl.error)) assert.fail(dl.error);
    const dest = dl.ok
      ? dl.path
      : path.join(updateDir(), SETUP_NAME);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, mz(MARKER_SWAP));
    const run = await invoke('app:runUpdateInstaller', dest);
    const launched = launches();
    if (launched.length > 0) {
      assert.fail('路径复查之后仍启动了被替换的文件：' + launched.map((item) => item.sourcePath).join(', ') + ' body=' + fs.readFileSync(dest).toString('latin1'));
    }
    assert.notStrictEqual(run.ok, true, JSON.stringify(run));
  });

  await check('Release API 跳到名单外主机返回的清单不得被采纳', async () => {
    state.mode = 'api-redirect';
    const info = await invoke('app:checkUpdate');
    if (info.ok && info.download && String(info.download.url).includes(EVIL_ASSET_NAME)) {
      const dl = await invoke('app:downloadUpdate', { url: info.download.url, name: info.download.name });
      if (dl.ok) await invoke('app:runUpdateInstaller', dl.path);
      assert.fail('名单外重定向给出的 Release 被采纳：' + JSON.stringify({ info, launches: launches() }));
    }
    assert.ok(!info.ok || !info.download || !String(info.download.url).includes(EVIL_ASSET_NAME));
    assert.strictEqual(anyUpdateFileContains(MARKER_API), false);
    assert.strictEqual(launches().length, 0);
    if (info.ok === false) assert.ok(!isTransportError(info.error), info.error);
  });

  await new Promise((resolve) => server.close(resolve));
  resetUpdateDir();
  console.log('----- 结果：PASS ' + passed + ' / FAIL ' + failures.length + ' -----');
  if (failures.length > 0) {
    failures.forEach((item) => console.log('  - FAIL: ' + item.name));
    process.exit(1);
  }
  console.log('更新安装包完整性单测通过');
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(2);
});

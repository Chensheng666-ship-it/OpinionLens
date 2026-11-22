// =============================================================================
// tests/js/test_electron_mirror_supply_chain.js
//
// 安装包构建会把 Electron 压缩包打进用户安装的程序。本测试用本地镜像替身
// 替换构建脚本配置的镜像主机，确认一份只被该镜像自己的校验文件背书的
// 压缩包不能成为被打包的 Electron。
//
// 依赖：Node >= 22。首次运行会按 app/desktop/package.json 的版本，从
// registry.npmjs.org 安装 electron 与 app-builder-lib（--ignore-scripts），
// 安装位置在系统临时目录，不写入仓库。
//
// 运行：
//   node tests/js/test_electron_mirror_supply_chain.js
//
// 在镜像校验被独立校验替换之前，安装包用例会失败，进程退出码为 1。
// 不加入离线 selftest（需要安装上述两个包，并在本机起 HTTP 服务）。
// =============================================================================
'use strict';

const assert = require('node:assert');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const zlib = require('node:zlib');

const repoRoot = path.resolve(__dirname, '..', '..');
const desktopDir = path.join(repoRoot, 'app', 'desktop');
const installerScript = path.join(repoRoot, 'app', 'scripts', 'build_installer.ps1');
const builderConfig = path.join(desktopDir, 'electron-builder.yml');
const ciWorkflow = path.join(repoRoot, '.github', 'workflows', 'ci.yml');
const depsDir = path.join(os.tmpdir(), 'opinionlens-electron-mirror-test-deps');

const MARKER = Buffer.from('OPINIONLENS-MIRROR-TAMPER\n');

let passed = 0;
const failures = [];

function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      passed += 1;
      console.log('  [PASS] ' + name);
    })
    .catch((error) => {
      failures.push({ name, message: error && error.stack ? error.stack : String(error) });
      console.log('  [FAIL] ' + name);
      console.log(error && error.stack ? error.stack : error);
    });
}

function read(file) {
  return fs.readFileSync(file, 'utf8');
}

function powershellCode(line) {
  const hash = line.indexOf('#');
  return hash === -1 ? line : line.slice(0, hash);
}

// 构建脚本在调用 electron-builder 时实际生效的镜像变量。
// 只认赋值与 Remove-Item，调用行之后的改动不算进这次打包。
function mirrorEnvAtBuilder(scriptText) {
  let electronMirror = null;
  let binariesMirror = null;
  for (const line of scriptText.split(/\r?\n/)) {
    const code = powershellCode(line);
    if (/&\s*\$builderCmd\b/.test(code)) break;
    let match = code.match(/\$env:ELECTRON_MIRROR\s*=\s*['"]([^'"]+)['"]/);
    if (match) electronMirror = match[1];
    if (/Remove-Item\s+Env:ELECTRON_MIRROR\b/.test(code)) electronMirror = null;
    match = code.match(/\$env:ELECTRON_BUILDER_BINARIES_MIRROR\s*=\s*['"]([^'"]+)['"]/);
    if (match) binariesMirror = match[1];
    if (/Remove-Item\s+Env:ELECTRON_BUILDER_BINARIES_MIRROR\b/.test(code)) binariesMirror = null;
  }
  return { electronMirror, binariesMirror };
}

function ciElectronMirror(workflowText) {
  const match = workflowText.match(/^\s*ELECTRON_MIRROR:\s*(\S+)\s*$/m);
  return match ? match[1] : null;
}

function isVendorElectronHost(rawUrl) {
  if (!rawUrl) return true;
  const parsed = new URL(rawUrl);
  return parsed.hostname === 'github.com'
    && parsed.pathname.includes('/electron/electron/releases/download');
}

function isVendorBuilderHost(rawUrl) {
  if (!rawUrl) return true;
  const parsed = new URL(rawUrl);
  return parsed.hostname === 'github.com'
    && parsed.pathname.includes('/electron-userland/electron-builder-binaries/');
}

function standInBase(configuredUrl, port) {
  const parsed = new URL(configuredUrl);
  const pathname = parsed.pathname.endsWith('/') ? parsed.pathname : parsed.pathname + '/';
  return `http://127.0.0.1:${port}${pathname}`;
}

function zipStored(files) {
  const parts = [];
  const central = [];
  let offset = 0;
  for (const file of files) {
    const name = Buffer.from(file.name);
    const data = file.data;
    const crc = zlib.crc32(data) >>> 0;
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0, 8);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(name.length, 26);
    parts.push(local, name, data);
    const cen = Buffer.alloc(46);
    cen.writeUInt32LE(0x02014b50, 0);
    cen.writeUInt16LE(20, 4);
    cen.writeUInt16LE(20, 6);
    cen.writeUInt32LE(crc, 16);
    cen.writeUInt32LE(data.length, 20);
    cen.writeUInt32LE(data.length, 24);
    cen.writeUInt16LE(name.length, 28);
    cen.writeUInt32LE(offset, 42);
    central.push(cen, name);
    offset += local.length + name.length + data.length;
  }
  const centralBuf = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(files.length, 8);
  end.writeUInt16LE(files.length, 10);
  end.writeUInt32LE(centralBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...parts, centralBuf, end]);
}

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function freshHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'opinionlens-mirror-home-'));
}

function listen(handler) {
  const server = http.createServer(handler);
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

function isChecksumRejection(error) {
  const message = String(error && error.message ? error.message : error);
  return /did not match expected checksum|No checksum found|checksum mismatch/i.test(message);
}

function artifactNames(version) {
  const normalized = version.startsWith('v') ? version : `v${version}`;
  return {
    normalized,
    zipName: `electron-${normalized}-win32-x64.zip`,
  };
}

function installDeps(electronVersion, builderVersion) {
  fs.mkdirSync(depsDir, { recursive: true });
  const marker = path.join(depsDir, '.versions');
  const want = electronVersion + '\n' + builderVersion;
  const electronPkg = path.join(depsDir, 'node_modules', 'electron', 'package.json');
  const builderPkg = path.join(depsDir, 'node_modules', 'app-builder-lib', 'package.json');
  if (fs.existsSync(marker) && read(marker) === want && fs.existsSync(electronPkg) && fs.existsSync(builderPkg)) {
    return;
  }
  fs.writeFileSync(path.join(depsDir, 'package.json'), JSON.stringify({
    private: true,
    dependencies: {
      electron: electronVersion,
      'app-builder-lib': builderVersion,
    },
  }));
  const result = spawnSync('npm', [
    'install', '--ignore-scripts', '--no-audit', '--no-fund',
    '--registry', 'https://registry.npmjs.org',
  ], { cwd: depsDir, encoding: 'utf8' });
  if (result.status !== 0) {
    throw new Error('npm install failed\n' + (result.stderr || '') + (result.stdout || ''));
  }
  fs.writeFileSync(marker, want);
}

function loadBuilderConfig() {
  const yaml = require(path.join(depsDir, 'node_modules', 'js-yaml'));
  return yaml.load(read(builderConfig)) || {};
}

function desktopVersions() {
  const pkg = JSON.parse(read(path.join(desktopDir, 'package.json')));
  return {
    electron: pkg.devDependencies.electron,
    builder: pkg.devDependencies['electron-builder'],
  };
}

async function withAttackerMirror(configuredUrl, zipName, zipBytes, fn) {
  const digest = sha256(zipBytes);
  const shasumBody = `${digest} *${zipName}\n`;
  const seen = [];
  const server = await listen((req, res) => {
    const urlPath = req.url.split('?')[0];
    seen.push(urlPath);
    if (urlPath.endsWith('/' + zipName)) {
      res.writeHead(200, { 'content-type': 'application/octet-stream', 'content-length': zipBytes.length });
      res.end(zipBytes);
      return;
    }
    if (urlPath.endsWith('/SHASUMS256.txt')) {
      const body = Buffer.from(shasumBody);
      res.writeHead(200, { 'content-type': 'text/plain', 'content-length': body.length });
      res.end(body);
      return;
    }
    res.writeHead(404);
    res.end('missing');
  });
  const port = server.address().port;
  try {
    return await fn({
      mirror: standInBase(configuredUrl, port),
      digest,
      seen,
    });
  } finally {
    await closeServer(server);
  }
}

function applyIsolatedCache(home) {
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.XDG_CACHE_HOME = path.join(home, 'xdg');
  process.env.LOCALAPPDATA = path.join(home, 'Local');
  process.env.ELECTRON_BUILDER_CACHE = path.join(home, 'eb');
  fs.mkdirSync(process.env.XDG_CACHE_HOME, { recursive: true });
  fs.mkdirSync(process.env.LOCALAPPDATA, { recursive: true });
  fs.mkdirSync(process.env.ELECTRON_BUILDER_CACHE, { recursive: true });
  delete process.env.electron_use_remote_checksums;
  delete process.env.npm_config_electron_use_remote_checksums;
  delete process.env.ELECTRON_GET_USE_PROXY;
}

async function main() {
  const versions = desktopVersions();
  assert.strictEqual(typeof versions.electron, 'string');
  assert.strictEqual(typeof versions.builder, 'string');
  installDeps(versions.electron, versions.builder);
  const installedElectron = JSON.parse(read(path.join(depsDir, 'node_modules', 'electron', 'package.json'))).version;
  const installedBuilder = JSON.parse(read(path.join(depsDir, 'node_modules', 'app-builder-lib', 'package.json'))).version;
  assert.strictEqual(installedElectron, versions.electron);
  assert.strictEqual(installedBuilder, versions.builder);

  const scriptEnv = mirrorEnvAtBuilder(read(installerScript));
  const config = loadBuilderConfig();
  const version = config.electronVersion || versions.electron;
  const names = artifactNames(version);
  const attackerZip = zipStored([{ name: 'electron.exe', data: MARKER }]);
  const { downloadElectronArtifactZip, downloadBuilderToolset } = require(path.join(
    depsDir, 'node_modules', 'app-builder-lib', 'out', 'util', 'electronGet.js'));

  console.log('===== Electron 镜像供给链 =====');
  console.log('  安装脚本镜像: ' + (scriptEnv.electronMirror || '(未设置)'));
  console.log('  构建工具镜像: ' + (scriptEnv.binariesMirror || '(未设置)'));

  await check('安装包打包的 Electron 不接受只由镜像校验文件背书的压缩包', async () => {
    const dist = config.electronDist;
    if (typeof dist === 'string' && dist.trim()) {
      const normalized = dist.replace(/\\/g, '/');
      assert.match(
        normalized,
        /node_modules\/electron(?:\/dist)?\/?$/,
        'electronDist 必须指向由 electron 包内 checksums.json 校验过的安装目录'
      );
      return;
    }
    if (isVendorElectronHost(scriptEnv.electronMirror)) {
      return;
    }
    const home = freshHome();
    applyIsolatedCache(home);
    const downloadOptions = {
      platform: 'win32',
      arch: 'x64',
      version,
    };
    if (config.electronDownload && typeof config.electronDownload === 'object') {
      Object.assign(downloadOptions, config.electronDownload);
    }
    await withAttackerMirror(scriptEnv.electronMirror, names.zipName, attackerZip, async (attacker) => {
      process.env.ELECTRON_MIRROR = attacker.mirror;
      if (typeof downloadOptions.mirror === 'string') downloadOptions.mirror = attacker.mirror;
      if (downloadOptions.mirrorOptions && typeof downloadOptions.mirrorOptions.mirror === 'string') {
        downloadOptions.mirrorOptions.mirror = attacker.mirror;
      }
      let zipPath;
      try {
        zipPath = await downloadElectronArtifactZip({
          electronDownload: downloadOptions,
          artifactName: 'electron',
          platformName: 'win32',
          arch: 'x64',
          version,
          cacheDir: path.join(home, 'electron-get-cache'),
        });
      } catch (error) {
        if (isChecksumRejection(error)) return;
        throw error;
      }
      const packed = fs.readFileSync(zipPath);
      assert.notStrictEqual(
        sha256(packed),
        attacker.digest,
        'electron-builder 接受了镜像同时提供的压缩包和 SHASUMS256.txt，这份压缩包会被解进安装目录并改名为产品可执行文件'
      );
    });
  });

  await check('CI 冒烟拉起 Electron 时拒绝与包内 checksums.json 不符的镜像压缩包', async () => {
    const mirror = ciElectronMirror(read(ciWorkflow));
    assert.ok(mirror, 'ci.yml 的 e2e job 应记录它使用的 ELECTRON_MIRROR；当前工作流没有这一项时无法核对冒烟路径');
    if (isVendorElectronHost(mirror)) return;
    const workflow = read(ciWorkflow);
    const remoteChecksums = /electron_use_remote_checksums/.test(workflow);
    const checksums = remoteChecksums
      ? undefined
      : JSON.parse(read(path.join(depsDir, 'node_modules', 'electron', 'checksums.json')));
    const home = freshHome();
    await withAttackerMirror(mirror, names.zipName, attackerZip, async (attacker) => {
      const installEnv = {
        ...process.env,
        HOME: home,
        USERPROFILE: home,
        XDG_CACHE_HOME: path.join(home, 'xdg'),
        LOCALAPPDATA: path.join(home, 'Local'),
        ELECTRON_MIRROR: attacker.mirror,
        ELECTRON_INSTALL_PLATFORM: 'win32',
        ELECTRON_INSTALL_ARCH: 'x64',
        electron_config_cache: path.join(home, 'electron-cache'),
        ELECTRON_GET_NO_PROGRESS: '1',
      };
      delete installEnv.electron_use_remote_checksums;
      delete installEnv.npm_config_electron_use_remote_checksums;
      const child = spawn(process.execPath, [path.join(depsDir, 'node_modules', 'electron', 'install.js')], {
        cwd: depsDir,
        env: installEnv,
      });
      let stderr = '';
      child.stderr.on('data', (chunk) => { stderr += chunk; });
      const status = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          child.kill();
          reject(new Error('electron install.js 超时。已见请求: ' + attacker.seen.join(', ')));
        }, 20000);
        child.on('error', reject);
        child.on('exit', (code) => {
          clearTimeout(timer);
          resolve(code);
        });
      });
      const distExe = path.join(depsDir, 'node_modules', 'electron', 'dist', 'electron.exe');
      assert.strictEqual(fs.existsSync(distExe), false, '校验失败后不应留下可执行文件');
      if (!remoteChecksums) {
        assert.notStrictEqual(status, 0, 'install.js 接受了镜像压缩包。stderr:\n' + stderr);
        assert.match(stderr, /did not match expected checksum|No checksum found/);
        assert.ok(
          !attacker.seen.some((item) => item.endsWith('/SHASUMS256.txt')),
          'install.js 不应再向镜像请求 SHASUMS256.txt，已见请求: ' + attacker.seen.join(', ')
        );
      } else {
        assert.notStrictEqual(
          status,
          0,
          'CI 打开了 electron_use_remote_checksums，镜像可以同时提供压缩包和校验文件'
        );
      }
      // checksums 变量保留，是为了和 install.js 一样：未打开远程校验时用包内文件。
      assert.ok(remoteChecksums || checksums[names.zipName], 'checksums.json 缺少 ' + names.zipName);
    });
  });

  await check('安装包使用的 electron-builder 工具压缩包不接受镜像自签的校验', async () => {
    if (isVendorBuilderHost(scriptEnv.binariesMirror)) return;
    const toolFile = '7zip-win-x64.tar.gz';
    const toolSource = read(path.join(depsDir, 'node_modules', 'app-builder-lib', 'out', 'toolsets', '7zip.js'));
    const match = toolSource.match(new RegExp(toolFile.replace(/\./g, '\\.') + '": "([a-f0-9]{64})"'));
    assert.ok(match, 'app-builder-lib 的 7zip 工具集里没有 ' + toolFile + ' 的独立校验');
    const payload = Buffer.from('NOT-THE-VENDOR-7ZIP');
    const home = freshHome();
    applyIsolatedCache(home);
    await withAttackerMirror(scriptEnv.binariesMirror, toolFile, payload, async (attacker) => {
      process.env.ELECTRON_BUILDER_BINARIES_MIRROR = attacker.mirror;
      delete process.env.ELECTRON_MIRROR;
      try {
        await downloadBuilderToolset({
          releaseName: '7zip@1.0.0',
          filenameWithExt: toolFile,
          checksums: { [toolFile]: match[1] },
        });
      } catch (error) {
        if (isChecksumRejection(error)) return;
        throw error;
      }
      throw new Error('构建工具下载接受了与 app-builder-lib 内置校验不符、但与镜像校验文件一致的压缩包');
    });
  });

  console.log('');
  if (failures.length) {
    console.log('失败 ' + failures.length + '，通过 ' + passed);
    process.exit(1);
  }
  console.log('通过 ' + passed);
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});

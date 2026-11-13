// =============================================================================
// app/scripts/test_update_logic.js — 更新逻辑离线单测（无网络、无 Electron）
//
// 运行：
//   node app/scripts/test_update_logic.js
// 由 app/scripts/selftest.ps1 自动调用；失败时退出码 1。
// =============================================================================
'use strict';

const assert = require('node:assert');
const path = require('node:path');

const mod = require(path.join(__dirname, '..', 'desktop', 'update.js'));
const { compareVersions, isNewer, pickInstallerAsset, normalizeVersion } = mod;

let passed = 0;
const failures = [];

function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log('  [PASS] ' + name);
  } catch (error) {
    failures.push({ name, message: error && error.message });
    console.log('  [FAIL] ' + name + '：' + (error && error.message));
  }
}

console.log('===== 更新逻辑单测（版本比较 / 安装包选择） =====');

check('normalizeVersion 去掉 v 前缀与 build 元数据', () => {
  assert.deepStrictEqual(normalizeVersion('v0.4.0+build.7').core, [0, 4, 0]);
  assert.strictEqual(normalizeVersion('V1.2').core.length, 3);
});

check('compareVersions：相等', () => {
  assert.strictEqual(compareVersions('0.3.0', '0.3.0'), 0);
  assert.strictEqual(compareVersions('v0.4.0', '0.4.0'), 0);
});

check('compareVersions：按数值而非字符串比较', () => {
  assert.strictEqual(compareVersions('0.10.0', '0.9.9'), 1);
  assert.strictEqual(compareVersions('0.9.9', '0.10.0'), -1);
});

check('compareVersions：正式版 > 预发布版', () => {
  assert.strictEqual(compareVersions('1.0.0', '1.0.0-beta.1'), 1);
  assert.strictEqual(compareVersions('1.0.0-beta.1', '1.0.0'), -1);
});

check('isNewer：远端更新才为 true', () => {
  assert.strictEqual(isNewer('0.3.0', 'v0.3.1'), true);
  assert.strictEqual(isNewer('0.3.0', '0.3.0'), false);
  assert.strictEqual(isNewer('0.3.0', '0.2.9'), false);
  assert.strictEqual(isNewer('0.3.0', ''), false);
  assert.strictEqual(isNewer('0.3.0', null), false);
});

check('pickInstallerAsset：挑中 Setup.exe，而非 blockmap/yml/zip', () => {
  const asset = pickInstallerAsset([
    { name: 'latest.yml', size: 900, browser_download_url: 'https://x/latest.yml' },
    { name: 'OpinionLens-Setup-0.4.0.exe', size: 248_000_000, browser_download_url: 'https://x/setup.exe' },
    { name: 'OpinionLens-Setup-0.4.0.exe.blockmap', size: 1000, browser_download_url: 'https://x/bm' },
    { name: 'OpinionLens-portable-0.4.0.zip', size: 200_000_000, browser_download_url: 'https://x/portable.zip' },
  ], 'v0.4.0');
  assert.ok(asset, '应挑到安装包');
  assert.strictEqual(asset.name, 'OpinionLens-Setup-0.4.0.exe');
  assert.strictEqual(asset.url, 'https://x/setup.exe');
});

check('pickInstallerAsset：只有一个 exe 时也能选中；无 exe 返回 null', () => {
  const one = pickInstallerAsset([{ name: 'Some-0.4.0.exe', size: 10, browser_download_url: 'https://x/a.exe' }]);
  assert.strictEqual(one && one.name, 'Some-0.4.0.exe');
  assert.strictEqual(pickInstallerAsset([{ name: 'a.zip', browser_download_url: 'https://x/a.zip' }]), null);
  assert.strictEqual(pickInstallerAsset([]), null);
  assert.strictEqual(pickInstallerAsset(null), null);
});

console.log('----- 结果：PASS ' + passed + ' / FAIL ' + failures.length + ' -----');
if (failures.length > 0) {
  failures.forEach((f) => console.log('  - FAIL: ' + f.name + ' -> ' + f.message));
  process.exit(1);
}
console.log('更新逻辑单测通过');

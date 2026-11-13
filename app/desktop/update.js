// =============================================================================
// 舆见 OpinionLens — 更新相关的纯逻辑（无 Electron / 无网络依赖，可离线单测）
//
// 被测入口：app/scripts/test_update_logic.js（selftest.ps1 会跑），
// 调用方：app/desktop/main.js 的 app:checkUpdate / app:downloadUpdate。
// =============================================================================
'use strict';

/**
 * 归一化版本号：去掉前缀 v/V、去掉 build 元数据（+xxx），保留预发布段。
 * @param {string} raw
 * @returns {{ core: number[], pre: string }}
 */
function normalizeVersion(raw) {
  const text = String(raw == null ? '' : raw).trim().replace(/^[vV]+/, '');
  const plus = text.indexOf('+');
  const withoutBuild = plus >= 0 ? text.slice(0, plus) : text;
  const dash = withoutBuild.indexOf('-');
  const coreText = dash >= 0 ? withoutBuild.slice(0, dash) : withoutBuild;
  const pre = dash >= 0 ? withoutBuild.slice(dash + 1) : '';
  const core = coreText.split('.').map((part) => {
    const n = parseInt(String(part).replace(/[^0-9]/g, ''), 10);
    return Number.isFinite(n) ? n : 0;
  });
  while (core.length < 3) core.push(0);
  return { core, pre };
}

/**
 * 比较两个版本号（语义化，忽略大小写与 v 前缀）。
 * @returns {number} -1: a<b, 0: 相等, 1: a>b
 */
function compareVersions(a, b) {
  const left = normalizeVersion(a);
  const right = normalizeVersion(b);
  const len = Math.max(left.core.length, right.core.length);
  for (let i = 0; i < len; i += 1) {
    const x = left.core[i] || 0;
    const y = right.core[i] || 0;
    if (x !== y) return x > y ? 1 : -1;
  }
  // 正式版 > 预发布版（1.0.0 > 1.0.0-beta.1）
  if (!left.pre && right.pre) return 1;
  if (left.pre && !right.pre) return -1;
  if (left.pre === right.pre) return 0;
  return left.pre > right.pre ? 1 : -1;
}

/**
 * latest 是否比 current 更新（相等或更旧都返回 false）。
 */
function isNewer(current, latest) {
  if (!String(latest || '').trim()) return false;
  return compareVersions(latest, current) > 0;
}

/**
 * 从 GitHub release assets 里挑 Windows 安装包。
 * 规则：只要 .exe；排除 .blockmap / .yml / 便携版 zip；优先名字含 Setup 且与 tag 版本一致的。
 * @param {Array<{name:string, size?:number, browser_download_url?:string}>} assets
 * @param {string} [versionHint] 例如 v0.4.0
 * @returns {{name:string, size:number, url:string}|null}
 */
function pickInstallerAsset(assets, versionHint) {
  const list = Array.isArray(assets) ? assets : [];
  const hint = String(versionHint || '').replace(/^[vV]+/, '');
  const candidates = list
    .filter((a) => a && typeof a.name === 'string' && /\.exe$/i.test(a.name))
    .filter((a) => !/\.blockmap$/i.test(a.name))
    .map((a) => ({
      name: a.name,
      size: Number(a.size) || 0,
      url: a.browser_download_url || a.url || '',
    }))
    .filter((a) => a.url);
  if (candidates.length === 0) return null;
  const score = (a) => {
    let s = 0;
    if (/setup/i.test(a.name)) s += 4;
    if (hint && a.name.indexOf(hint) >= 0) s += 2;
    if (/x64|amd64/i.test(a.name)) s += 1;
    return s;
  };
  candidates.sort((x, y) => score(y) - score(x) || y.size - x.size);
  return candidates[0];
}

module.exports = { normalizeVersion, compareVersions, isNewer, pickInstallerAsset };

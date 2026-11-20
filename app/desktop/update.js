// =============================================================================
// 舆见 OpinionLens — 更新相关的纯逻辑（无 Electron / 无网络依赖，可离线单测）
//
// 被测入口：tests/js/test_update_logic.js（selftest.ps1 会跑），
// 调用方：app/desktop/main.js 的 app:checkUpdate / app:downloadUpdate；
// 发布侧签名：app/scripts/sign_update.js（与这里共用 signedUpdateMessage）。
// =============================================================================
'use strict';

const crypto = require('node:crypto');

/**
 * 允许签发更新包的发布者公钥（Ed25519，SPKI DER 的 base64）。
 * 只有能用其中某把私钥签出 `<安装包名>.sig` 的 Release 才能在应用内下载并启动；
 * 列表为空时应用内安装一律拒绝，用户只能去发布页手动下载。
 * 生成密钥与签名见 app/scripts/sign_update.js；私钥不得进仓库。
 */
const UPDATE_SIGNING_PUBLIC_KEYS = Object.freeze([]);

const UPDATE_SIGNATURE_FORMAT = 'opinionlens-update-signature/1';
const UPDATE_SIGNATURE_SUFFIX = '.sig';
const MAX_SIGNATURE_BYTES = 16 * 1024;
const MAX_INSTALLER_BYTES = 2 * 1024 * 1024 * 1024;

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

/**
 * 找与安装包同名的签名清单资产（`<安装包名>.sig`）。
 * @returns {{name:string, size:number, url:string}|null}
 */
function pickSignatureAsset(assets, installerName) {
  const list = Array.isArray(assets) ? assets : [];
  const wanted = String(installerName || '') + UPDATE_SIGNATURE_SUFFIX;
  if (wanted === UPDATE_SIGNATURE_SUFFIX) return null;
  const found = list.find((a) => a && a.name === wanted && (a.browser_download_url || a.url));
  if (!found) return null;
  return { name: found.name, size: Number(found.size) || 0, url: found.browser_download_url || found.url };
}

/** 被签名的字节串：安装包名 + 版本 + 大小 + SHA-256 一起绑定，防止拿旧版/别的文件的签名冒充。 */
function signedUpdateMessage({ name, version, size, sha256 }) {
  return Buffer.from([
    UPDATE_SIGNATURE_FORMAT,
    `name=${name}`,
    `version=${version}`,
    `size=${size}`,
    `sha256=${sha256}`,
    '',
  ].join('\n'), 'utf8');
}

/**
 * 解析签名清单（JSON）。字段不全/格式不对直接抛错。
 * @returns {{name:string, version:string, size:number, sha256:string, signature:Buffer}}
 */
function parseUpdateSignature(text) {
  let data;
  try {
    data = JSON.parse(String(text || ''));
  } catch (_) {
    throw new Error('签名清单不是合法 JSON');
  }
  if (!data || typeof data !== 'object' || data.format !== UPDATE_SIGNATURE_FORMAT) {
    throw new Error('签名清单格式不受支持');
  }
  const name = typeof data.name === 'string' ? data.name : '';
  const version = typeof data.version === 'string' ? data.version.trim().replace(/^[vV]+/, '') : '';
  const size = data.size;
  const sha256 = typeof data.sha256 === 'string' ? data.sha256.toLowerCase() : '';
  const signature = typeof data.signature === 'string' ? Buffer.from(data.signature, 'base64') : Buffer.alloc(0);
  if (!name || name.length > 200 || /[\\/:*?"<>|\0]/.test(name) || !/\.exe$/i.test(name)) {
    throw new Error('签名清单里的安装包名无效');
  }
  if (!version || version.length > 64 || /[\s=]/.test(version)) throw new Error('签名清单里的版本号无效');
  if (!Number.isSafeInteger(size) || size <= 0 || size > MAX_INSTALLER_BYTES) {
    throw new Error('签名清单里的安装包大小无效');
  }
  if (!/^[0-9a-f]{64}$/.test(sha256)) throw new Error('签名清单里的 SHA-256 无效');
  if (signature.length !== 64) throw new Error('签名清单里的签名无效');
  return { name, version, size, sha256, signature };
}

/** 用固定公钥验签；任何一把公钥验过即通过。 */
function verifyUpdateSignature(manifest, publicKeys = UPDATE_SIGNING_PUBLIC_KEYS) {
  const message = signedUpdateMessage(manifest);
  for (const raw of Array.isArray(publicKeys) ? publicKeys : []) {
    let key;
    try {
      key = crypto.createPublicKey({ key: Buffer.from(String(raw), 'base64'), format: 'der', type: 'spki' });
    } catch (_) {
      continue;
    }
    if (key.asymmetricKeyType !== 'ed25519') continue;
    try {
      if (crypto.verify(null, message, key, manifest.signature)) return true;
    } catch (_) { /* 签名格式异常视为不通过 */ }
  }
  return false;
}

/**
 * 校验已验签清单与本次检查更新选中的 Release 是否对得上。
 * @returns {string} 空字符串 = 通过；否则为拒绝原因
 */
function checkSignedUpdateMatches(manifest, { installerName, tag, current }) {
  if (manifest.name !== installerName) return '签名清单与所选安装包不一致';
  if (manifest.version !== String(tag || '').trim().replace(/^[vV]+/, '')) return '签名清单与 Release 版本不一致';
  if (!isNewer(current, manifest.version)) return '签名清单的版本不比当前版本新';
  return '';
}

module.exports = {
  normalizeVersion,
  compareVersions,
  isNewer,
  pickInstallerAsset,
  pickSignatureAsset,
  signedUpdateMessage,
  parseUpdateSignature,
  verifyUpdateSignature,
  checkSignedUpdateMatches,
  UPDATE_SIGNING_PUBLIC_KEYS,
  UPDATE_SIGNATURE_FORMAT,
  UPDATE_SIGNATURE_SUFFIX,
  MAX_SIGNATURE_BYTES,
};

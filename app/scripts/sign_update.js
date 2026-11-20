// =============================================================================
// 舆见 OpinionLens — 更新安装包签名（发布侧，Node 18+，无第三方依赖）
//
// 应用内「下载并安装」只接受带 `<安装包名>.sig` 且能被 app/desktop/update.js 里
// UPDATE_SIGNING_PUBLIC_KEYS 某把公钥验过的 Release。
//
// 1) 生成发布密钥（只做一次；私钥放仓库外、离线保存，不要提交、不要上传到 Release）：
//      node app/scripts/sign_update.js keygen --out D:\keys\opinionlens-update.pem
//    输出的 public key 粘贴到 app/desktop/update.js 的 UPDATE_SIGNING_PUBLIC_KEYS。
//
// 2) 每次发版签名（version 必须与 Release tag 去掉 v 后一致）：
//      node app/scripts/sign_update.js sign --key D:\keys\opinionlens-update.pem `
//        --version 0.8.0 build\installer\output\OpinionLens-Setup-0.8.0.exe
//    生成同目录 OpinionLens-Setup-0.8.0.exe.sig，与安装包一起挂到 Release。
// =============================================================================
'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const update = require(path.join(__dirname, '..', 'desktop', 'update.js'));

function usage(message) {
  if (message) console.error(message);
  console.error('用法：');
  console.error('  node app/scripts/sign_update.js keygen --out <private-key.pem>');
  console.error('  node app/scripts/sign_update.js sign --key <private-key.pem> --version <x.y.z> <installer.exe>');
  process.exit(2);
}

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg.startsWith('--')) {
      const value = argv[i + 1];
      if (value === undefined || value.startsWith('--')) usage(`缺少 ${arg} 的值`);
      out[arg.slice(2)] = value;
      i += 1;
    } else {
      out._.push(arg);
    }
  }
  return out;
}

function sha256File(filePath) {
  const hash = crypto.createHash('sha256');
  const fd = fs.openSync(filePath, 'r');
  try {
    const buf = Buffer.alloc(1024 * 1024);
    let n;
    while ((n = fs.readSync(fd, buf, 0, buf.length, null)) > 0) hash.update(buf.subarray(0, n));
  } finally {
    fs.closeSync(fd);
  }
  return hash.digest('hex');
}

function keygen(args) {
  if (!args.out) usage('keygen 需要 --out');
  const outPath = path.resolve(args.out);
  const repoRoot = path.resolve(__dirname, '..', '..');
  if ((outPath + path.sep).toLowerCase().startsWith(repoRoot.toLowerCase() + path.sep)) {
    usage('私钥不能放在仓库目录内');
  }
  const { publicKey, privateKey } = crypto.generateKeyPairSync('ed25519');
  fs.writeFileSync(outPath, privateKey.export({ format: 'pem', type: 'pkcs8' }), { flag: 'wx', mode: 0o600 });
  console.log(`私钥已写入：${outPath}`);
  console.log('把下面这行加入 app/desktop/update.js 的 UPDATE_SIGNING_PUBLIC_KEYS：');
  console.log(`  '${publicKey.export({ format: 'der', type: 'spki' }).toString('base64')}',`);
}

function sign(args) {
  const installer = args._[1];
  if (!args.key || !args.version || !installer) usage('sign 需要 --key、--version 与安装包路径');
  const privateKey = crypto.createPrivateKey(fs.readFileSync(path.resolve(args.key)));
  if (privateKey.asymmetricKeyType !== 'ed25519') usage('私钥必须是 Ed25519');
  const installerPath = path.resolve(installer);
  const manifest = {
    name: path.basename(installerPath),
    version: String(args.version).trim().replace(/^[vV]+/, ''),
    size: fs.statSync(installerPath).size,
    sha256: sha256File(installerPath),
  };
  const signature = crypto.sign(null, update.signedUpdateMessage(manifest), privateKey);
  const text = `${JSON.stringify({ format: update.UPDATE_SIGNATURE_FORMAT, ...manifest, signature: signature.toString('base64') }, null, 2)}\n`;
  update.parseUpdateSignature(text);
  const outPath = installerPath + update.UPDATE_SIGNATURE_SUFFIX;
  fs.writeFileSync(outPath, text, 'utf8');
  const pinned = update.verifyUpdateSignature(update.parseUpdateSignature(text));
  console.log(`签名清单已写入：${outPath}`);
  if (!pinned) {
    console.warn('警告：该私钥对应的公钥不在 UPDATE_SIGNING_PUBLIC_KEYS 里，应用内会拒绝此安装包。');
  }
}

const args = parseArgs(process.argv.slice(2));
if (args._[0] === 'keygen') keygen(args);
else if (args._[0] === 'sign') sign(args);
else usage();

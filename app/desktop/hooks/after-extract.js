'use strict';

// electron-builder afterExtract 钩子（见 electron-builder.yml 的 electronDist）。
// 使用解压好的 Electron 目录时，electron-builder 不会像下载 zip 那样删除
// Electron 自带的默认应用与版本文件。这里在同一时机（解出 Electron 之后、
// 计算 asar 完整性之前）补齐，保持安装包内容不变。
const fs = require('node:fs');
const path = require('node:path');

module.exports = async function afterExtract(context) {
  const { appOutDir, electronPlatformName } = context;
  if (electronPlatformName === 'darwin' || electronPlatformName === 'mas') return;
  for (const rel of [path.join('resources', 'default_app.asar'), 'version']) {
    await fs.promises.rm(path.join(appOutDir, rel), { force: true });
  }
};

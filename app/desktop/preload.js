// =============================================================================
// 舆见 OpinionLens — Electron preload
// 契约：app/CONTRACT.md §4.2
//   window.fsapi = { getInfo, openPath, showItemInFolder, openExternal,
//                    checkUpdate, downloadUpdate, runUpdateInstaller, onUpdateProgress,
//                    chooseDataDir, migrateDataDir, resetDataDir, onDataDirProgress }
// 安全：只暴露白名单方法，不暴露 ipcRenderer / Node API；
//       contextIsolation=true + nodeIntegration=false + sandbox=true。
// =============================================================================

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

/** 订阅一个主进程推送通道，返回取消订阅函数（跨 contextBridge 传输时会被代理成可调用函数）。 */
function subscribe(channel, handler) {
  if (typeof handler !== 'function') return () => {};
  const listener = (_event, payload) => {
    try {
      handler(payload);
    } catch (_) {
      /* 渲染层回调异常不污染主进程推送循环 */
    }
  };
  ipcRenderer.on(channel, listener);
  return () => {
    try {
      ipcRenderer.removeListener(channel, listener);
    } catch (_) {
      /* ignore */
    }
  };
}

const fsapi = Object.freeze({
  /** @returns {Promise<{baseUrl:string|null, token:string, python:string, dataDir:string, dataDirDefault:string, dataDirCustom:boolean, userConfigPath:string, appVersion:string, electronVersion:string, platform:string, arch:string, updateRepo:string, sidecar:object, dev:boolean}>} */
  getInfo: () => ipcRenderer.invoke('app:info'),

  /** @param {string} targetPath @returns {Promise<{ok:boolean, error:string|null, path?:string}>} */
  openPath: (targetPath) => ipcRenderer.invoke('app:openPath', targetPath),

  /** @param {string} targetPath @returns {Promise<{ok:boolean, error?:string}>} */
  showItemInFolder: (targetPath) => ipcRenderer.invoke('app:showItemInFolder', targetPath),

  /** 采集动态「原文链接」：由主进程用系统浏览器打开（主进程校验仅 http/https）。@param {string} url @returns {Promise<{ok:boolean, error?:string}>} */
  openExternal: (url) => ipcRenderer.invoke('app:openExternal', url),

  // ---- 版本更新（主进程代为联网；renderer 不直连外网） ----------------------
  /** @returns {Promise<{ok:boolean, current:string, latest?:string, hasUpdate?:boolean, releaseUrl?:string, notes?:string, download?:object|null, source?:string, warning?:string, error?:string}>} */
  checkUpdate: () => ipcRenderer.invoke('app:checkUpdate'),

  /** @param {{url:string, name:string}} payload @returns {Promise<{ok:boolean, path?:string, size?:number, error?:string}>} */
  downloadUpdate: (payload) => ipcRenderer.invoke('app:downloadUpdate', payload),

  /** @param {string} filePath 只允许运行下载到临时目录的安装包 @returns {Promise<{ok:boolean, error?:string}>} */
  runUpdateInstaller: (filePath) => ipcRenderer.invoke('app:runUpdateInstaller', filePath),

  /** @param {(progress:{received:number,total:number,percent:number|null})=>void} handler @returns {() => void} 取消订阅 */
  onUpdateProgress: (handler) => subscribe('app:updateProgress', handler),

  // ---- 数据目录（默认 %APPDATA%\OpinionLens，可迁到其他盘） -----------------
  /** @returns {Promise<{ok:boolean, canceled?:boolean, path?:string}>} */
  chooseDataDir: () => ipcRenderer.invoke('app:chooseDataDir'),

  /** @param {string} targetPath @returns {Promise<{ok:boolean, source?:string, target?:string, needRestart?:boolean, oldDirKept?:boolean, code?:string, error?:string}>} */
  migrateDataDir: (targetPath) => ipcRenderer.invoke('app:migrateDataDir', targetPath),

  /** 恢复默认数据目录（%APPDATA%\OpinionLens） @returns {Promise<{ok:boolean, target?:string, needRestart?:boolean, error?:string}>} */
  resetDataDir: () => ipcRenderer.invoke('app:resetDataDir'),

  /** @param {(progress:{step:string,label:string,percent?:number})=>void} handler @returns {() => void} 取消订阅 */
  onDataDirProgress: (handler) => subscribe('app:dataDirProgress', handler),
});

contextBridge.exposeInMainWorld('fsapi', fsapi);

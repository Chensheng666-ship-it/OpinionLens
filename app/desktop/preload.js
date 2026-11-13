// =============================================================================
// 舆见 OpinionLens — Electron preload（M1）
// 契约：app/CONTRACT.md §4.2
//   window.fsapi = { getInfo, openPath, showItemInFolder }
// 安全：只暴露白名单方法，不暴露 ipcRenderer / Node API；
//       contextIsolation=true + nodeIntegration=false + sandbox=true。
// =============================================================================

'use strict';

const { contextBridge, ipcRenderer } = require('electron');

const fsapi = Object.freeze({
  /** @returns {Promise<{baseUrl:string|null, token:string, python:string, dataDir:string, appVersion:string, sidecar:object, dev:boolean}>} */
  getInfo: () => ipcRenderer.invoke('app:info'),

  /** @param {string} targetPath @returns {Promise<{ok:boolean, error:string|null, path?:string}>} */
  openPath: (targetPath) => ipcRenderer.invoke('app:openPath', targetPath),

  /** @param {string} targetPath @returns {Promise<{ok:boolean, error?:string}>} */
  showItemInFolder: (targetPath) => ipcRenderer.invoke('app:showItemInFolder', targetPath),
});

contextBridge.exposeInMainWorld('fsapi', fsapi);

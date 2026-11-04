// 只把白名单里的 IPC 通道暴露给页面，页面拿不到 Node / Electron 的其他能力。
const { contextBridge, ipcRenderer } = require('electron');

const SEND = new Set([
  'state:save', 'overlay:move', 'overlay:set-bounds', 'overlay:set-height', 'overlay:commit-bounds',
  'overlay:set-lock', 'overlay:interactive', 'overlay:hide', 'menu:popup', 'settings:open', 'settings:close',
  'app:quit', 'shell:open', 'shell:external', 'smoke:overlay-ready', 'smoke:settings-ready',
]);
const INVOKE = new Set(['state:get', 'overlay:bounds', 'hotkeys:register', 'app:restart-backend']);
const ON = new Set(['action', 'lock-changed', 'fatal']);

contextBridge.exposeInMainWorld('lcs', {
  send: (ch, ...args) => { if (SEND.has(ch)) ipcRenderer.send(ch, ...args); },
  invoke: (ch, ...args) => (INVOKE.has(ch) ? ipcRenderer.invoke(ch, ...args) : Promise.reject(new Error(ch))),
  on: (ch, cb) => { if (ON.has(ch)) ipcRenderer.on(ch, (_e, ...args) => cb(...args)); },
});

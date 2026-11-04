// 课堂实时字幕 —— Electron 主进程
//
// 职责：
//   1. 启动 Python 后端（python main.py --server），读到 @@READY 后拿到端口和令牌
//   2. 字幕窗：无边框、透明、永远置顶、不占任务栏；锁定时鼠标穿透
//   3. 设置窗口、托盘图标、全局快捷键、右键菜单
//   4. 后端崩溃或设置改动后自动重启后端
//
// 界面（src/*.html）直接用 WebSocket 连后端收字幕、发命令；和主进程之间只通过 preload.js 暴露的少量 IPC 通信。

const { app, BrowserWindow, ipcMain, globalShortcut, Tray, Menu, nativeImage, screen, shell, nativeTheme } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const STATE_FILE = path.join(ROOT, 'ui_state.json');
const LOG_DIR = path.join(ROOT, 'logs');
const ICON = path.join(__dirname, 'assets', 'icon.png');
const PRELOAD = path.join(__dirname, 'preload.js');

// 命令行：electron . [--smoke] [--shots 目录] [-- 传给 Python 后端的参数]
const argv = process.argv.slice(app.isPackaged ? 1 : 2);
const dd = argv.indexOf('--');
const ownArgs = dd >= 0 ? argv.slice(0, dd) : argv;
const backendArgs = dd >= 0 ? argv.slice(dd + 1) : [];
const SMOKE = ownArgs.includes('--smoke');
const SHOTS = ownArgs.includes('--shots') ? path.resolve(ownArgs[ownArgs.indexOf('--shots') + 1]) : null;

let overlay = null;
let settingsWin = null;
let tray = null;
let backend = null;          // 子进程
let conn = null;             // { port, token }
let locked = false;
let quitting = false;
let restarting = false;
let backendFailures = 0;
let state = loadState();

// ---------- 日志 ----------
fs.mkdirSync(LOG_DIR, { recursive: true });
const logStream = fs.createWriteStream(path.join(LOG_DIR, 'desktop.log'), { flags: 'a' });
function log(...parts) {
  const line = `${new Date().toISOString()} ${parts.join(' ')}`.trimEnd();
  logStream.write(line + '\n');
  if (SMOKE) process.stdout.write(line + '\n');
}

// ---------- 窗口状态记忆 ----------
function loadState() {
  try {
    const s = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'));
    return s.version === 3 ? s : {}; // 旧版（tkinter）的坐标单位不同，不沿用
  } catch {
    return {};
  }
}
function saveState(patch) {
  state = { ...state, ...patch, version: 3 };
  if (SMOKE) return; // 测试不改动真实的窗口记忆
  try {
    fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
  } catch (e) {
    log('保存窗口状态失败', e.message);
  }
}

// ---------- Python 后端 ----------
function pythonExe() {
  const venv = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
  return process.env.LCS_PYTHON || (fs.existsSync(venv) ? venv : 'python');
}

function startBackend() {
  return new Promise((resolve, reject) => {
    const exe = pythonExe();
    log('启动后端：', exe, 'main.py --server', backendArgs.join(' '));
    const p = spawn(exe, ['main.py', '--server', ...backendArgs], {
      cwd: ROOT,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    backend = p;
    let buf = '';
    let settled = false;
    const timer = setTimeout(() => finish(new Error('后端 120 秒内没有启动')), 120000);
    function finish(err, info) {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      err ? reject(err) : resolve(info);
    }
    p.stdout.on('data', (d) => {
      buf += d.toString('utf8');
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (line.startsWith('@@READY ')) finish(null, JSON.parse(line.slice(8)));
        else if (line) log('[py]', line);
      }
    });
    p.stderr.on('data', (d) => log('[py!]', d.toString('utf8').trimEnd()));
    p.on('error', (e) => finish(new Error(`找不到 Python（${exe}）：${e.message}`)));
    p.on('exit', (code) => {
      log('后端退出，代码', code);
      finish(new Error(`后端启动失败（退出代码 ${code}），详见 logs/app.log`));
      if (backend === p) {
        backend = null;
        if (!quitting && !restarting) onBackendCrash(code);
      }
    });
  });
}

function stopBackend() {
  const p = backend;
  backend = null;
  if (!p) return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => resolve();
    p.once('exit', done);
    try { p.stdin.end(); } catch { /* 已经关了 */ }   // 后端读到 EOF 会自己退出
    setTimeout(() => { try { p.kill(); } catch { /* 已退出 */ } done(); }, 4000);
  });
}

async function bootBackend() {
  try {
    conn = await startBackend();
    backendFailures = 0;
    log('后端就绪，端口', conn.port);
    return true;
  } catch (e) {
    log('后端启动失败：', e.message);
    conn = null;
    showFatal(e.message);
    return false;
  }
}

function onBackendCrash(code) {
  backendFailures += 1;
  if (backendFailures <= 2) {
    log('后端意外退出，自动重启');
    restartBackend();
  } else {
    showFatal(`后端反复退出（代码 ${code}），请查看 logs/app.log`);
  }
}

async function restartBackend() {
  if (restarting) return;
  restarting = true;
  try {
    await stopBackend();
    if (await bootBackend()) loadOverlay();
  } finally {
    restarting = false;
  }
}

function showFatal(message) {
  if (overlay && !overlay.isDestroyed()) overlay.webContents.send('fatal', message);
}

// ---------- 字幕窗 ----------
function clampToScreen(b) {
  const d = screen.getDisplayMatching(b).workArea;
  const width = Math.min(Math.max(b.width, 360), d.width);
  const height = Math.min(Math.max(b.height, 80), d.height);
  const x = Math.min(Math.max(b.x, d.x), d.x + d.width - width);
  const y = Math.min(Math.max(b.y, d.y), d.y + d.height - height);
  return { x, y, width, height };
}

function createOverlay() {
  const wa = screen.getPrimaryDisplay().workArea;
  const width = state.width || Math.round(wa.width * 0.62);
  const height = state.fixedHeight || 150;
  const bottom = state.bottom ?? wa.y + wa.height - 28;
  const x = state.x ?? Math.round(wa.x + (wa.width - width) / 2);
  const bounds = clampToScreen({ x, y: bottom - height, width, height });

  overlay = new BrowserWindow({
    ...bounds,
    frame: false,
    transparent: true,
    resizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    hasShadow: false,
    show: false,
    title: '课堂实时字幕',
    icon: ICON,
    backgroundColor: '#00000000',
    webPreferences: { preload: PRELOAD, contextIsolation: true, sandbox: true, spellcheck: false },
  });
  overlay.setAlwaysOnTop(true, 'screen-saver'); // 盖过会议软件的全屏共享
  overlay.setVisibleOnAllWorkspaces(true);
  overlay.on('closed', () => { overlay = null; });
  overlay.webContents.on('render-process-gone', (_e, d) => log('字幕窗渲染进程退出', d.reason));
  loadOverlay();
  overlay.once('ready-to-show', () => overlay.showInactive());
}

function loadOverlay() {
  if (!overlay) return;
  const query = conn ? { port: String(conn.port), token: conn.token } : { error: '1' };
  if (SMOKE) query.smoke = '3';
  overlay.loadFile(path.join(__dirname, 'src', 'overlay.html'), { query });
  setLocked(false);
}

function setLocked(on) {
  locked = !!on;
  if (!overlay) return;
  overlay.setIgnoreMouseEvents(locked, { forward: true }); // forward：锁定时仍能感知鼠标悬停
  overlay.webContents.send('lock-changed', locked);
  updateTray();
}

function toggleVisible() {
  if (!overlay) return;
  if (overlay.isVisible()) overlay.hide();
  else overlay.showInactive();
  updateTray();
}

// ---------- 设置窗口 ----------
function openSettings(firstRun = false) {
  if (settingsWin) {
    settingsWin.show();
    settingsWin.focus();
    return;
  }
  if (!conn) return;
  settingsWin = new BrowserWindow({
    width: 680,
    height: 820,
    minWidth: 560,
    minHeight: 560,
    title: '设置 · 课堂实时字幕',
    icon: ICON,
    show: false,
    backgroundColor: '#0b0d12',
    titleBarStyle: 'hidden',
    titleBarOverlay: { color: '#0b0d12', symbolColor: '#a3adbd', height: 44 },
    webPreferences: { preload: PRELOAD, contextIsolation: true, sandbox: true, spellcheck: false },
  });
  settingsWin.setMenu(null);
  settingsWin.loadFile(path.join(__dirname, 'src', 'settings.html'), {
    query: { port: String(conn.port), token: conn.token, first: firstRun ? '1' : '0', smoke: SMOKE ? '1' : '0' },
  });
  settingsWin.once('ready-to-show', () => settingsWin.show());
  settingsWin.on('closed', () => { settingsWin = null; });
}

// ---------- 全局快捷键 ----------
const HOTKEY_ACTIONS = {
  toggle_visible: () => toggleVisible(),
  toggle_lock: () => setLocked(!locked),
  toggle_pause: () => overlay && overlay.webContents.send('action', { act: 'pause' }),
};

function toAccelerator(spec) {
  const map = { ctrl: 'Control', control: 'Control', alt: 'Alt', shift: 'Shift', win: 'Super' };
  return spec.split('+').map((p) => p.trim()).filter(Boolean)
    .map((p) => map[p.toLowerCase()] || (p.length === 1 ? p.toUpperCase() : p[0].toUpperCase() + p.slice(1).toLowerCase()))
    .join('+');
}

function registerHotkeys(hotkeys) {
  globalShortcut.unregisterAll();
  const registered = {};
  const failed = {};
  for (const [action, spec] of Object.entries(hotkeys || {})) {
    if (!spec || !HOTKEY_ACTIONS[action]) continue;
    const acc = toAccelerator(spec);
    let ok = false;
    try { ok = globalShortcut.register(acc, HOTKEY_ACTIONS[action]); } catch { ok = false; }
    (ok ? registered : failed)[action] = acc;
  }
  if (Object.keys(failed).length) log('快捷键注册失败：', JSON.stringify(failed));
  return { registered, failed };
}

// ---------- 托盘 ----------
function sendAction(act, extra = {}) {
  if (overlay) overlay.webContents.send('action', { act, ...extra });
}

function updateTray() {
  if (!tray) return;
  const visible = overlay && overlay.isVisible();
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: visible ? '隐藏字幕' : '显示字幕', click: toggleVisible },
    { label: '暂停 / 继续', click: () => sendAction('pause') },
    { label: locked ? '解锁字幕' : '锁定字幕（鼠标穿透）', click: () => setLocked(!locked) },
    { type: 'separator' },
    { label: '打开本节课记录', click: () => sendAction('open-transcript') },
    { label: '打开记录文件夹', click: () => sendAction('open-transcript-dir') },
    { label: '生成本节课笔记', click: () => sendAction('notes') },
    { type: 'separator' },
    { label: '设置…', click: () => openSettings(false) },
    { label: '重启识别服务', click: () => restartBackend() },
    { label: '退出', click: () => app.quit() },
  ]));
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(ICON).resize({ width: 16, height: 16 }));
  tray.setToolTip('课堂实时字幕');
  tray.on('click', toggleVisible);
  updateTray();
}

// ---------- 右键菜单 ----------
function popupMenu(info) {
  const deviceItems = [
    { label: '跟随系统默认输出', type: 'radio', checked: !info.device, click: () => sendAction('set-device', { name: '' }) },
    { type: 'separator' },
    ...(info.devices || []).map((name) => ({
      label: name,
      type: 'radio',
      checked: !!info.device && name.toLowerCase().includes(info.device.toLowerCase()),
      click: () => sendAction('set-device', { name }),
    })),
  ];
  if (info.onlyDevices) {
    Menu.buildFromTemplate(deviceItems).popup({ window: overlay });
    return;
  }
  const hk = info.hotkeys || {};
  Menu.buildFromTemplate([
    { label: info.paused ? '继续' : '暂停', accelerator: hk.toggle_pause, registerAccelerator: false, click: () => sendAction('pause') },
    { label: '隐藏字幕', accelerator: hk.toggle_visible, registerAccelerator: false, click: toggleVisible },
    { label: '锁定（鼠标穿透）', accelerator: hk.toggle_lock, registerAccelerator: false, click: () => setLocked(true) },
    { label: `显示${info.langName || ''}原文`, type: 'checkbox', checked: info.showSource, click: () => sendAction('toggle-source') },
    { label: '监听设备', submenu: deviceItems },
    { type: 'separator' },
    { label: '打开本节课记录', click: () => sendAction('open-transcript') },
    { label: '打开记录文件夹', click: () => sendAction('open-transcript-dir') },
    { label: '生成本节课笔记', click: () => sendAction('notes') },
    { type: 'separator' },
    { label: '背景更深', click: () => sendAction('bg', { delta: 0.08 }) },
    { label: '背景更浅', click: () => sendAction('bg', { delta: -0.08 }) },
    { label: '清空字幕', click: () => sendAction('clear') },
    { label: '高度随内容自动', type: 'checkbox', checked: !info.fixedHeight, click: () => sendAction('auto-height') },
    { label: '重置位置和大小', click: resetLayout },
    { type: 'separator' },
    { label: '设置…', click: () => openSettings(false) },
    { label: '退出', click: () => app.quit() },
  ]).popup({ window: overlay });
}

function resetLayout() {
  if (!overlay) return;
  const wa = screen.getDisplayMatching(overlay.getBounds()).workArea;
  const width = Math.round(wa.width * 0.62);
  const b = overlay.getBounds();
  overlay.setBounds({ x: Math.round(wa.x + (wa.width - width) / 2), y: wa.y + wa.height - 28 - b.height, width, height: b.height });
  saveState({ x: undefined, bottom: undefined, width: undefined, fixedHeight: null });
  sendAction('reset');
}

// ---------- IPC ----------
function inProject(p) {
  const rel = path.relative(ROOT, path.resolve(p));
  return rel && !rel.startsWith('..') && !path.isAbsolute(rel);
}

ipcMain.handle('state:get', () => state);
ipcMain.on('state:save', (_e, patch) => saveState(patch));
ipcMain.handle('overlay:bounds', () => overlay && overlay.getBounds());
ipcMain.on('overlay:move', (_e, x, y) => overlay && overlay.setPosition(Math.round(x), Math.round(y)));
ipcMain.on('overlay:set-bounds', (_e, b) => overlay && overlay.setBounds(clampToScreen({
  x: Math.round(b.x), y: Math.round(b.y), width: Math.round(b.width), height: Math.round(b.height),
})));
ipcMain.on('overlay:set-height', (_e, h) => {
  if (!overlay) return;
  const b = overlay.getBounds();
  const height = Math.max(70, Math.round(h));
  if (height !== b.height) overlay.setBounds({ x: b.x, y: b.y + b.height - height, width: b.width, height });
});
ipcMain.on('overlay:commit-bounds', (_e, fixedHeight) => {
  if (!overlay) return;
  const b = overlay.getBounds();
  saveState({ x: b.x, bottom: b.y + b.height, width: b.width, fixedHeight: fixedHeight || null });
});
ipcMain.on('overlay:set-lock', (_e, on) => setLocked(on));
ipcMain.on('overlay:interactive', (_e, on) => {
  // 锁定状态下鼠标移到“解锁”按钮上时，临时恢复可点击
  if (overlay && locked) overlay.setIgnoreMouseEvents(!on, { forward: true });
});
ipcMain.on('overlay:hide', () => toggleVisible());
ipcMain.on('menu:popup', (_e, info) => popupMenu(info));
ipcMain.handle('hotkeys:register', (_e, hotkeys) => registerHotkeys(hotkeys));
ipcMain.on('settings:open', (_e, firstRun) => openSettings(!!firstRun));
ipcMain.on('settings:close', () => settingsWin && settingsWin.close());
ipcMain.handle('app:restart-backend', async () => {
  if (settingsWin) settingsWin.close();
  await restartBackend();
  return !!conn;
});
ipcMain.on('app:quit', () => app.quit());
ipcMain.on('shell:open', (_e, p) => {
  if (p && inProject(p)) shell.openPath(path.resolve(p));
});
ipcMain.on('shell:external', (_e, url) => {
  if (url === 'https://console.anthropic.com/settings/keys') shell.openExternal(url);
});

// ---------- 自动化冒烟测试（npm run smoke） ----------
const smokeResults = [];
function check(name, ok, detail = '') {
  smokeResults.push({ name, ok });
  process.stdout.write(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? `  (${detail})` : ''}\n`);
}
async function capture(win, file) {
  if (!SHOTS || !win) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  const img = await win.webContents.capturePage();
  fs.writeFileSync(path.join(SHOTS, file), img.toPNG());
  check(`截图 ${file}`, true, `${img.getSize().width}x${img.getSize().height}`);
}
ipcMain.on('smoke:overlay-ready', async (_e, info) => {
  check('字幕窗出现正式译文', info.finals >= 1, `${info.finals} 句`);
  check('快捷键注册', Object.keys(info.hotkeys.failed || {}).length === 0, JSON.stringify(info.hotkeys.registered));
  await new Promise((r) => setTimeout(r, 1200));
  await capture(overlay, 'screenshot.png');
  setLocked(true);
  check('锁定（鼠标穿透）', locked);
  setLocked(false);
  toggleVisible();
  check('隐藏字幕', !overlay.isVisible());
  toggleVisible();
  check('重新显示字幕', overlay.isVisible());
  openSettings(false);
});
ipcMain.on('smoke:settings-ready', async (_e, info) => {
  check('设置窗口读取到设置', !!info.language, info.language);
  await new Promise((r) => setTimeout(r, 600));
  await capture(settingsWin, 'settings.png');
  const failed = smokeResults.filter((r) => !r.ok).length;
  process.stdout.write(`\n${smokeResults.length - failed}/${smokeResults.length} 通过\n`);
  process.exitCode = failed ? 1 : 0;
  app.quit();
});

// ---------- 生命周期 ----------
if (!SMOKE && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => overlay && overlay.showInactive());
  app.whenReady().then(async () => {
    nativeTheme.themeSource = 'dark';
    Menu.setApplicationMenu(null);
    createTray();
    createOverlay();
    if (await bootBackend()) loadOverlay();
    if (SMOKE) setTimeout(() => { check('冒烟测试在 5 分钟内完成', false); app.exit(1); }, 300000);
  });
  app.on('window-all-closed', () => { /* 托盘程序：窗口全关了也不退出，从托盘退出 */ });
  app.on('before-quit', () => { quitting = true; });
  app.on('will-quit', async (e) => {
    globalShortcut.unregisterAll();
    if (backend) {
      e.preventDefault();
      await stopBackend();
      app.exit(process.exitCode || 0);
    }
  });
}

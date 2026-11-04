// 字幕窗页面逻辑
const $ = (s) => document.querySelector(s);
const q = new URLSearchParams(location.search);
const SMOKE = Number(q.get('smoke') || 0);
const body = document.body;
const card = $('#card');
const linesEl = $('#lines');

const ui = { lines: 3, keepFixed: 60, zh: 26, src: 15, bg: 0.8, showSource: true, fixedHeight: null };
let backend = null;
let hello = null;
let hotkeys = { registered: {}, failed: {} };
let paused = false;
let speaking = false;
let finals = 0;
let smokeSent = false;
const entries = new Map(); // id -> { el, srcEl, zhEl, partial, finalText }

// ---------- 小工具 ----------
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// “原词（中文）”：原词蓝色高亮、释义变灰，方便一眼看到老师讲的生词
const TERM = /([฀-๿][฀-๿ ]*|[A-Za-zÀ-ɏḀ-ỿ][A-Za-zÀ-ɏḀ-ỿ' -]*|[가-힯]+|[぀-ヿ]+)(\s*)（([^（）]{1,40})）/g;
function formatZh(text) {
  return escapeHtml(text).replace(TERM, '<span class="term">$1</span>$2<span class="gloss">（$3）</span>');
}

function hk(action) {
  const a = hotkeys.registered[action];
  return a ? `（${a}）` : '';
}

// ---------- 状态栏 ----------
function setStatus(text) {
  $('#status-text').textContent = text;
  const err = /^错误|无法|反复退出/.test(text);
  const warn = !err && /跟不上|失败|跳过|没听到|占用|超时/.test(text);
  $('#status').classList.toggle('warn', warn);
  $('#status').classList.toggle('err', err);
  updateDot(warn ? 'warn' : err ? 'err' : null);
}

function updateDot(level) {
  const dot = $('#dot');
  const loading = /加载|启动/.test($('#status-text').textContent);
  dot.className = 'dot';
  if (level) dot.classList.add(level);
  else if (paused) dot.classList.add('paused');
  else if (loading) dot.classList.add('idle');
  else if (speaking) dot.classList.add('speaking');
}

// ---------- 字幕渲染 ----------
function keepCount() {
  return ui.fixedHeight ? ui.keepFixed : ui.lines;
}

function nearBottom() {
  return linesEl.scrollHeight - linesEl.scrollTop - linesEl.clientHeight < 40;
}

function afterChange(stick) {
  const ids = [...entries.keys()];
  while (ids.length > keepCount()) {
    const id = ids.shift();
    entries.get(id).el.remove();
    entries.delete(id);
  }
  ids.forEach((id, i) => entries.get(id).el.classList.toggle('old', i < ids.length - 1));
  $('#empty').classList.toggle('hidden', entries.size > 0);
  if (ui.fixedHeight) {
    if (stick) linesEl.scrollTop = linesEl.scrollHeight;
    else $('#jump').hidden = false;
  }
}

function entry(id) {
  let e = entries.get(id);
  if (!e) {
    const el = document.createElement('div');
    el.className = 'line partial';
    el.innerHTML = '<div class="src"></div><div class="zh"></div>';
    linesEl.appendChild(el);
    e = { el, srcEl: el.firstChild, zhEl: el.lastChild, partial: true, finalText: false, text: null };
    entries.set(id, e);
  }
  return e;
}

function renderZh(e) {
  if (e.text === null) {
    e.zhEl.innerHTML = `<span class="pending">${e.partial ? '识别中' : '翻译中'}</span>`;
  } else if (!e.text) {
    e.zhEl.innerHTML = '';
  } else {
    e.zhEl.innerHTML = formatZh(e.text) + (e.finalText ? '' : '<span class="typing"></span>');
  }
}

function onSubtitle(ev) {
  const stick = !ui.fixedHeight || nearBottom();
  if (ev.type === 'partial' || ev.type === 'transcript') {
    const e = entry(ev.id);
    e.partial = ev.type === 'partial';
    e.el.classList.toggle('partial', e.partial);
    e.srcEl.textContent = ev.source;
    renderZh(e);
  } else if (ev.type === 'translation') {
    const e = entries.get(ev.id);
    if (!e || (ev.partial && e.finalText)) return; // 正式译文已到，迟到的临时译文不覆盖
    e.text = ev.text;
    e.finalText = !ev.partial;
    if (e.finalText) {
      e.el.classList.remove('partial');
      finals += 1;
    }
    renderZh(e);
    if (SMOKE && !smokeSent && finals >= SMOKE) {
      smokeSent = true;
      window.lcs.send('smoke:overlay-ready', { finals, hotkeys });
    }
  } else if (ev.type === 'discard') {
    const e = entries.get(ev.id);
    if (!e || !e.partial) return;
    e.el.remove();
    entries.delete(ev.id);
  }
  afterChange(stick);
}

// ---------- 外观 ----------
function applyUi() {
  document.documentElement.style.setProperty('--zh-size', `${ui.zh}px`);
  document.documentElement.style.setProperty('--src-size', `${ui.src}px`);
  document.documentElement.style.setProperty('--bg-alpha', ui.bg);
  body.classList.toggle('no-src', !ui.showSource);
  $('#btn-source').classList.toggle('on', ui.showSource);
  body.classList.toggle('fixed', !!ui.fixedHeight);
  body.classList.toggle('auto', !ui.fixedHeight);
}

function savePrefs() {
  window.lcs.send('state:save', { fontZh: ui.zh, fontSrc: ui.src, bgAlpha: ui.bg, showSource: ui.showSource });
}

function changeFont(delta) {
  ui.zh = Math.min(64, Math.max(14, ui.zh + delta));
  ui.src = Math.min(40, Math.max(10, ui.src + Math.sign(delta)));
  applyUi();
  savePrefs();
}

function setAutoHeight() {
  ui.fixedHeight = null;
  applyUi();
  afterChange(true);
  window.lcs.send('overlay:commit-bounds', null);
}

// 自动高度：卡片高度变了就让窗口跟着变（底边不动）
new ResizeObserver(() => {
  if (ui.fixedHeight) return;
  const pad = parseFloat(getComputedStyle(body).paddingTop) + parseFloat(getComputedStyle(body).paddingBottom);
  window.lcs.send('overlay:set-height', Math.ceil(card.getBoundingClientRect().height + pad));
}).observe(card);

// ---------- 按钮 / 菜单 ----------
function refreshButtons() {
  $('#btn-pause').innerHTML = icon(paused ? 'play' : 'pause');
  $('#btn-pause').title = (paused ? '继续' : '暂停') + hk('toggle_pause');
  $('#btn-pause').classList.toggle('alert', paused);
  $('#btn-lock').innerHTML = icon('lock');
  $('#btn-lock').title = '锁定：鼠标点击穿透到下面的窗口' + hk('toggle_lock');
  $('#btn-device').innerHTML = icon('volume');
  $('#btn-device').title = '选择监听哪个输出设备';
  $('#btn-settings').innerHTML = icon('sliders');
  $('#btn-more').innerHTML = icon('more');
  $('#btn-close').innerHTML = icon('close', 16);
  $('#unlock').innerHTML = `${icon('unlock', 15)}<span>已锁定 · 点这里解锁${hotkeys.registered.toggle_lock ? `或按 ${hotkeys.registered.toggle_lock}` : ''}</span>`;
  $('#jump').innerHTML = `${icon('arrowDown', 14)}<span>最新字幕</span>`;
  updateDot();
}

async function devices() {
  try {
    return await backend.call('list_devices');
  } catch {
    return { devices: [], current: hello ? hello.device : '' };
  }
}

async function popupMenu(onlyDevices = false) {
  const d = await devices();
  window.lcs.send('menu:popup', {
    onlyDevices,
    devices: d.devices,
    device: d.current,
    paused,
    showSource: ui.showSource,
    fixedHeight: ui.fixedHeight,
    langName: hello ? hello.language.name : '',
    hotkeys: hotkeys.registered,
  });
}

async function run(act, extra = {}) {
  try {
    switch (act) {
      case 'pause': {
        const r = await backend.call('toggle_pause');
        paused = r.paused;
        refreshButtons();
        break;
      }
      case 'device': return popupMenu(true);
      case 'more': return popupMenu(false);
      case 'set-device': await backend.call('set_device', { name: extra.name }); break;
      case 'source':
      case 'toggle-source': ui.showSource = !ui.showSource; applyUi(); savePrefs(); break;
      case 'font-up': changeFont(2); break;
      case 'font-down': changeFont(-2); break;
      case 'bg': ui.bg = Math.min(1, Math.max(0.2, +(ui.bg + extra.delta).toFixed(2))); applyUi(); savePrefs(); break;
      case 'lock': window.lcs.send('overlay:set-lock', true); break;
      case 'settings': window.lcs.send('settings:open', false); break;
      case 'close': window.lcs.send('app:quit'); break;
      case 'clear': entries.forEach((e) => e.el.remove()); entries.clear(); afterChange(true); break;
      case 'auto-height': setAutoHeight(); break;
      case 'reset': ui.zh = 26; ui.src = 15; ui.bg = 0.8; setAutoHeight(); savePrefs(); break;
      case 'open-transcript': {
        const r = await backend.call('transcript');
        if (r.path) window.lcs.send('shell:open', r.path);
        else setStatus('这节课还没有记录（还没识别到内容）');
        break;
      }
      case 'open-transcript-dir': {
        const r = await backend.call('transcript');
        window.lcs.send('shell:open', r.dir);
        break;
      }
      case 'notes': {
        const r = await backend.call('generate_notes');
        setStatus(`正在整理笔记：${r.transcript.split(/[\\/]/).pop()}`);
        break;
      }
      default: break;
    }
  } catch (e) {
    setStatus(`操作失败：${e.message}`);
  }
}

$('#tools').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-act]');
  if (b) run(b.dataset.act);
});
$('#jump').addEventListener('click', () => {
  linesEl.scrollTop = linesEl.scrollHeight;
  $('#jump').hidden = true;
});
linesEl.addEventListener('scroll', () => { if (nearBottom()) $('#jump').hidden = true; });
$('#fatal-retry').addEventListener('click', async () => {
  $('#fatal-text').textContent = '正在重启识别服务…';
  await window.lcs.invoke('app:restart-backend');
});

// 锁定时：鼠标移到“解锁”按钮上才临时恢复可点击
const unlockBtn = $('#unlock');
unlockBtn.addEventListener('mouseenter', () => window.lcs.send('overlay:interactive', true));
unlockBtn.addEventListener('mouseleave', () => window.lcs.send('overlay:interactive', false));
unlockBtn.addEventListener('click', () => window.lcs.send('overlay:set-lock', false));

document.addEventListener('mouseenter', () => body.classList.add('hover'));
document.addEventListener('mouseleave', () => body.classList.remove('hover'));
document.addEventListener('contextmenu', (e) => {
  e.preventDefault();
  if (!body.classList.contains('locked')) popupMenu(false);
});
document.addEventListener('wheel', (e) => {
  if (e.ctrlKey) {
    e.preventDefault();
    changeFont(e.deltaY < 0 ? 2 : -2);
  }
}, { passive: false });

// ---------- 拖动 / 调整大小 ----------
let gesture = null;
document.addEventListener('pointerdown', async (e) => {
  if (e.button !== 0 || body.classList.contains('locked')) return;
  const edge = e.target.closest('.edge');
  if (!edge && e.target.closest('button')) return;
  e.target.setPointerCapture(e.pointerId);
  gesture = { kind: edge ? edge.dataset.edge : 'move', sx: e.screenX, sy: e.screenY, b: null, target: e.target };
  gesture.b = await window.lcs.invoke('overlay:bounds');
});
document.addEventListener('pointermove', (e) => {
  if (!gesture || !gesture.b) return;
  const dx = e.screenX - gesture.sx;
  const dy = e.screenY - gesture.sy;
  const { x, y, width, height } = gesture.b;
  if (gesture.kind === 'move') {
    window.lcs.send('overlay:move', x + dx, y + dy);
    return;
  }
  const nb = { x, y, width, height };
  if (gesture.kind.includes('l')) { nb.x = x + dx; nb.width = width - dx; }
  if (gesture.kind.includes('r')) nb.width = width + dx;
  if (gesture.kind.includes('t')) {
    nb.y = y + dy;
    nb.height = height - dy;
    ui.fixedHeight = Math.max(90, nb.height);
    applyUi();
  }
  window.lcs.send('overlay:set-bounds', nb);
});
document.addEventListener('pointerup', () => {
  if (!gesture) return;
  const moved = gesture.b;
  gesture = null;
  if (moved) window.lcs.send('overlay:commit-bounds', ui.fixedHeight);
});
$('.edge-t').addEventListener('dblclick', setAutoHeight);

// ---------- 主进程发来的事件 ----------
window.lcs.on('action', (a) => run(a.act, a));
window.lcs.on('lock-changed', (on) => body.classList.toggle('locked', on));
window.lcs.on('fatal', (msg) => showFatal(msg));

function showFatal(msg) {
  $('#fatal').hidden = false;
  $('#fatal-text').textContent = msg;
  $('#empty').classList.add('hidden');
  setStatus('错误：识别服务没有运行');
}

// ---------- 启动 ----------
async function init() {
  if (SMOKE) body.classList.add('smoke', 'hover'); // 截图用：模拟背后的视频画面，并显示工具栏
  const st = await window.lcs.invoke('state:get');
  if (st.fontZh) ui.zh = st.fontZh;
  if (st.fontSrc) ui.src = st.fontSrc;
  if (st.bgAlpha) ui.bg = st.bgAlpha;
  if (typeof st.showSource === 'boolean') ui.showSource = st.showSource;
  ui.fixedHeight = st.fixedHeight || null;
  applyUi();
  refreshButtons();

  if (q.get('error')) {
    showFatal('识别服务没能启动，请查看 logs/app.log');
    return;
  }
  backend = backendFromQuery();
  backend
    .on('hello', async (h) => {
      hello = h;
      paused = h.paused;
      $('#btn-source').textContent = h.language.short;
      $('#btn-source').title = `显示 / 隐藏${h.language.name}原文`;
      document.documentElement.style.setProperty('--font-src', [...h.language.fonts, 'Segoe UI', 'Microsoft YaHei UI'].map((f) => `"${f}"`).join(','));
      hotkeys = await window.lcs.invoke('hotkeys:register', h.hotkeys);
      refreshButtons();
      setStatus(h.status);
      if (Object.keys(hotkeys.failed).length) setStatus(`部分快捷键被其他程序占用：${Object.values(hotkeys.failed).join('、')}`);
      if (h.needs_setup) window.lcs.send('settings:open', true);
    })
    .on('status', (ev) => setStatus(ev.text))
    .on('fatal', (ev) => showFatal(ev.text))
    .on('speech', (ev) => { speaking = ev.active; updateDot(); })
    .on('partial', onSubtitle)
    .on('transcript', onSubtitle)
    .on('translation', onSubtitle)
    .on('discard', onSubtitle)
    .on('notes_done', (ev) => { setStatus('笔记已生成'); window.lcs.send('shell:open', ev.path); })
    .on('close', () => setStatus('正在重新连接识别服务…'));
  backend.connect();
}

init();

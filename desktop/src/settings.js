// 设置窗口逻辑：从后端读取设置和可选项，保存时交给后端校验并写入 user_settings.json / .env
const $ = (s) => document.querySelector(s);
const q = new URLSearchParams(location.search);
const FIRST = q.get('first') === '1';
const SMOKE = q.get('smoke') === '1';

let values = null;
let options = null;
const backend = backendFromQuery();

function msg(text, kind = '') {
  $('#msg').textContent = text;
  $('#msg').className = `msg ${kind}`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// 分段按钮
function seg(id, items, key) {
  const el = $(`#${id}`);
  el.innerHTML = items.map((o) => `<button data-value="${o.value}" title="${escapeHtml(o.desc || '')}">${escapeHtml(o.label)}</button>`).join('');
  const sync = () => el.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b.dataset.value === values[key]));
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    values[key] = b.dataset.value;
    sync();
    onChange();
  });
  sync();
}

// 单选卡片
function choices(id, items, key) {
  const el = $(`#${id}`);
  el.innerHTML = items.map((o) => `
    <button class="choice" data-value="${o.value}">
      <div class="choice-title">${escapeHtml(o.label)}${o.tag ? `<span class="tag">${escapeHtml(o.tag)}</span>` : ''}</div>
      <div class="choice-desc">${escapeHtml(o.desc || '')}</div>
      <span class="radio"></span>
    </button>`).join('');
  const sync = () => el.querySelectorAll('.choice').forEach((b) => b.classList.toggle('on', b.dataset.value === values[key]));
  el.addEventListener('click', (e) => {
    const b = e.target.closest('.choice');
    if (!b) return;
    values[key] = b.dataset.value;
    sync();
    onChange();
  });
  sync();
}

function onChange() {
  $('#claude-only').classList.toggle('disabled', values.backend !== 'claude');
  msg('');
}

function render() {
  seg('language', options.languages, 'language');
  choices('backend', options.backends, 'backend');
  choices('model', options.claude_models, 'model');
  choices('asr_model', options.asr_models, 'asr_model');
  seg('asr_device', options.asr_devices, 'asr_device');
  $('#glossary').value = values.glossary;
  $('#partials').setAttribute('aria-checked', String(values.partials));
  $('#key-hint').innerHTML = values.has_key
    ? '已经设置过 Key。留空表示不修改。'
    : '在 <a id="console-link">console.anthropic.com</a> 创建。只保存在本机的 .env 文件里，不会上传。';
  const link = $('#console-link');
  if (link) link.addEventListener('click', () => window.lcs.send('shell:external', 'https://console.anthropic.com/settings/keys'));
  onChange();
}

async function save(restart) {
  values.glossary = $('#glossary').value;
  const apiKey = $('#api-key').value.trim();
  document.querySelectorAll('.footer .btn').forEach((b) => { b.disabled = true; });
  msg('正在保存…');
  try {
    await backend.call('save_settings', { values, api_key: apiKey });
    if (restart) {
      msg('已保存，正在重启识别服务…', 'ok');
      await window.lcs.invoke('app:restart-backend'); // 主进程会关掉这个窗口
    } else {
      msg('已保存，重启后生效', 'ok');
      setTimeout(() => window.lcs.send('settings:close'), 900);
    }
  } catch (e) {
    msg(e.message, 'err');
    document.querySelectorAll('.footer .btn').forEach((b) => { b.disabled = false; });
  }
}

// ---------- 事件 ----------
$('#partials').addEventListener('click', () => {
  values.partials = !values.partials;
  $('#partials').setAttribute('aria-checked', String(values.partials));
});
$('#key-eye').addEventListener('click', () => {
  const input = $('#api-key');
  input.type = input.type === 'password' ? 'text' : 'password';
  $('#key-eye').innerHTML = icon(input.type === 'password' ? 'eye' : 'eyeOff', 17);
});
$('#cancel').addEventListener('click', () => window.lcs.send('settings:close'));
$('#save').addEventListener('click', () => save(false));
$('#save-restart').addEventListener('click', () => save(true));
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') window.lcs.send('settings:close');
});

// ---------- 启动 ----------
document.querySelectorAll('[data-ic]').forEach((el) => { el.innerHTML = icon(el.dataset.ic, 16); });
$('#app-ic').innerHTML = icon('captions', 18);
$('#key-eye').innerHTML = icon('eye', 17);
if (FIRST) {
  $('#welcome').hidden = false;
  $('#cancel').textContent = '稍后再说';
  $('#save').hidden = true;
  $('#save-restart').textContent = '开始使用';
}

backend.on('open', async () => {
  if (values) return;
  try {
    const r = await backend.call('get_settings');
    values = r.values;
    options = r.options;
    render();
    if (SMOKE) window.lcs.send('smoke:settings-ready', { language: values.language });
  } catch (e) {
    msg(`读取设置失败：${e.message}`, 'err');
  }
});
backend.connect();

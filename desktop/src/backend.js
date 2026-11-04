// 连接 Python 后端的 WebSocket 客户端：收事件、发命令（带回复）、断线自动重连。
class Backend {
  constructor(port, token) {
    this.url = `ws://127.0.0.1:${port}/?token=${encodeURIComponent(token)}`;
    this.handlers = {};
    this.pending = new Map();
    this.nextId = 1;
    this.ws = null;
    this.closed = false;
    this.retry = 0;
  }

  on(type, cb) {
    (this.handlers[type] ||= []).push(cb);
    return this;
  }

  emit(type, data) {
    (this.handlers[type] || []).forEach((cb) => cb(data));
    (this.handlers['*'] || []).forEach((cb) => cb(data));
  }

  connect() {
    if (this.closed) return;
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => { this.retry = 0; this.emit('open', {}); };
    ws.onmessage = (e) => {
      let msg;
      try { msg = JSON.parse(e.data); } catch { return; }
      if (msg.type === 'reply') {
        const p = this.pending.get(msg.id);
        if (p) {
          this.pending.delete(msg.id);
          msg.ok ? p.resolve(msg.data) : p.reject(new Error(msg.error || '命令失败'));
        }
        return;
      }
      this.emit(msg.type, msg);
    };
    ws.onclose = () => {
      this.pending.forEach((p) => p.reject(new Error('与后端的连接断开了')));
      this.pending.clear();
      this.emit('close', {});
      if (!this.closed && this.retry < 20) {
        this.retry += 1;
        setTimeout(() => this.connect(), Math.min(500 * this.retry, 3000));
      }
    };
  }

  call(cmd, params = {}) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error('后端还没连上'));
        return;
      }
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ cmd, id, ...params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error('后端没有响应'));
        }
      }, 15000);
    });
  }

  close() {
    this.closed = true;
    if (this.ws) this.ws.close();
  }
}

function backendFromQuery() {
  const q = new URLSearchParams(location.search);
  return q.get('port') ? new Backend(q.get('port'), q.get('token')) : null;
}

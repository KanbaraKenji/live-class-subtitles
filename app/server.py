"""给 Electron 界面用的本机 WebSocket 服务。

Python 负责听、识别、翻译；界面（desktop/ 下的 Electron 应用）只负责显示。
两边通过 ws://127.0.0.1:<端口>/?token=<令牌> 通信，令牌防止本机其他网页连进来。

启动后在标准输出打印一行：
  @@READY {"port": 51234, "token": "..."}
Electron 读到这一行就知道该连哪里。父进程退出（标准输入关闭）时服务自动退出，不会残留进程占着显卡。

服务端 → 界面：流水线的所有事件（见 pipeline.py），外加
  {"type": "hello", ...}        连接后的初始状态
  {"type": "notes_done", "path"} 笔记生成完毕
  {"type": "reply", "id", "ok", "data" | "error"}  命令的回复
界面 → 服务端：{"cmd": "...", "id": n, ...}，见 Server._commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import secrets
import sys
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import has_api_key, load_config, save_env_var, save_user_settings
from .languages import LANGUAGES, get_language
from .pipeline import Pipeline

log = logging.getLogger(__name__)

# 设置页的可选项（界面直接用这些生成下拉框/选项卡）
SETTINGS_OPTIONS = {
    "languages": [{"value": code, "label": "自动识别" if code == "auto" else lang.name}
                  for code, lang in LANGUAGES.items()],
    "backends": [
        {"value": "claude", "label": "Claude", "desc": "质量最好，需要 API Key"},
        {"value": "free", "label": "免费翻译", "desc": "Google → MyMemory，质量一般"},
        {"value": "none", "label": "不翻译", "desc": "只显示原文"},
    ],
    "claude_models": [
        {"value": "claude-haiku-5-5", "label": "Haiku 5.5", "desc": "最快，每句约 1 秒", "tag": "推荐"},
        {"value": "claude-sonnet-5-5", "label": "Sonnet 5.5", "desc": "更准，稍慢"},
        {"value": "claude-opus-5-5", "label": "Opus 5.5", "desc": "最准，每句 2~3 秒"},
    ],
    "asr_models": [
        {"value": "large-v3", "label": "large-v3", "desc": "最准，约 4GB 显存"},
        {"value": "large-v3-turbo", "label": "large-v3-turbo", "desc": "更快，准确度略低"},
        {"value": "medium", "label": "medium", "desc": "约 2GB 显存"},
        {"value": "small", "label": "small", "desc": "约 1GB，没有独显也能跑"},
    ],
    "asr_devices": [
        {"value": "auto", "label": "自动", "desc": "有 NVIDIA 显卡就用显卡"},
        {"value": "cuda", "label": "显卡", "desc": "CUDA"},
        {"value": "cpu", "label": "CPU", "desc": "很慢"},
    ],
}


def settings_view(cfg: dict) -> dict:
    a, t = cfg["asr"], cfg["translate"]
    return {
        "language": a.get("language", "th"),
        "backend": t.get("backend", "claude"),
        "model": t.get("model", "claude-haiku-5-5"),
        "asr_model": a.get("model", "large-v3"),
        "asr_device": a.get("device", "auto"),
        "partials": float(cfg["vad"].get("partial_interval_s", 2)) > 0,
        "glossary": t.get("glossary", "") or "",
        "has_key": has_api_key(),
    }


def settings_patch(values: dict) -> dict:
    """把设置页提交的值转成 user_settings.json 的补丁（只认识的字段）。"""
    valid = {k: {o["value"] for o in v} for k, v in SETTINGS_OPTIONS.items()}

    def pick(key: str, options: str, default: str) -> str:
        v = values.get(key, default)
        return v if v in valid[options] else default

    partials = bool(values.get("partials", True))
    return {
        "asr": {
            "language": pick("language", "languages", "th"),
            "model": pick("asr_model", "asr_models", "large-v3"),
            "device": pick("asr_device", "asr_devices", "auto"),
        },
        "translate": {
            "backend": pick("backend", "backends", "claude"),
            "model": pick("model", "claude_models", "claude-haiku-5-5"),
            "glossary": str(values.get("glossary", "")).strip()[:2000],
            "translate_partials": partials,
        },
        "vad": {"partial_interval_s": 2.0 if partials else 0.0},
    }


class Server:
    def __init__(self, cfg: dict, pipeline: Pipeline | None, events: queue.Queue) -> None:
        self.cfg = cfg
        self.pipeline = pipeline
        self.events = events
        self.token = secrets.token_urlsafe(24)
        self.clients: set = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.last_status = "启动中…"
        self._stopping = threading.Event()
        self._notes_running = False

    # ---------- 状态 ----------
    def hello(self) -> dict:
        lang = get_language(self.cfg["asr"]["language"])
        hk = self.cfg.get("hotkeys", {})
        p = self.pipeline
        return {
            "type": "hello",
            "status": self.last_status,
            "paused": bool(p and p.paused),
            "language": {"code": lang.code, "name": lang.name, "short": lang.short, "fonts": lang.fonts},
            "device": p.device_hint if p else "",
            "hotkeys": {k: v for k, v in hk.items() if k != "enabled"} if hk.get("enabled", True) else {},
            "ui": self.cfg["ui"],
            "needs_setup": self.cfg["translate"]["backend"] == "claude" and not has_api_key(),
        }

    # ---------- 事件转发 ----------
    def _pump(self) -> None:
        """后台线程：把流水线事件队列里的东西转给所有界面连接。"""
        while not self._stopping.is_set():
            try:
                ev = self.events.get(timeout=0.5)
            except queue.Empty:
                continue
            if ev.get("type") == "status":
                self.last_status = ev["text"]
            self._send_threadsafe(ev)

    def _send_threadsafe(self, ev: dict) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(asyncio.ensure_future, self._broadcast(ev))

    async def _broadcast(self, ev: dict) -> None:
        msg = json.dumps(ev, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                self.clients.discard(ws)

    # ---------- 命令 ----------
    def _cmd_toggle_pause(self, msg: dict) -> dict:
        return {"paused": self.pipeline.toggle_pause() if self.pipeline else False}

    def _cmd_list_devices(self, msg: dict) -> dict:
        names = Pipeline.list_devices()
        return {"devices": names, "current": self.pipeline.device_hint if self.pipeline else ""}

    def _cmd_set_device(self, msg: dict) -> dict:
        name = str(msg.get("name", ""))
        if self.pipeline:
            self.pipeline.set_device(name)
        save_user_settings({"audio": {"device": name}})
        return {"device": name}

    def _cmd_get_settings(self, msg: dict) -> dict:
        return {"values": settings_view(self.cfg), "options": SETTINGS_OPTIONS}

    def _cmd_save_settings(self, msg: dict) -> dict:
        values = msg.get("values") or {}
        key = str(msg.get("api_key") or "").strip()
        patch = settings_patch(values)
        if patch["translate"]["backend"] == "claude" and not key and not has_api_key():
            raise ValueError("选了 Claude 翻译就需要填 API Key；没有的话可以先选「免费翻译」")
        if key and not key.startswith("sk-ant-"):
            raise ValueError("这看起来不像 Claude API Key（应该以 sk-ant- 开头）")
        save_user_settings(patch)
        if key:
            save_env_var("ANTHROPIC_API_KEY", key)
        log.info("设置已保存（API Key %s）", "已更新" if key else "未修改")
        return {"saved": True}

    def _cmd_transcript(self, msg: dict) -> dict:
        lb = self.pipeline.logbook if self.pipeline else None
        directory = lb.directory if lb else Path(self.cfg["_project_dir"]) / self.cfg["log"]["dir"]
        path = lb.path if lb and lb.exists else None
        return {"dir": str(directory), "path": str(path) if path else None}

    def _cmd_generate_notes(self, msg: dict) -> dict:
        from .review import generate_notes, latest_transcript

        if self._notes_running:
            raise ValueError("笔记正在生成中")
        info = self._cmd_transcript(msg)
        path = Path(info["path"]) if info["path"] else latest_transcript(info["dir"])
        if path is None:
            raise ValueError("没有可以生成笔记的课堂记录")
        self._notes_running = True

        def work() -> None:
            try:
                out = generate_notes(path, self.cfg,
                                     on_progress=lambda s: self.events.put({"type": "status", "text": s}))
                self._send_threadsafe({"type": "notes_done", "path": str(out)})
            except Exception as e:
                log.exception("生成笔记失败")
                self.events.put({"type": "status", "text": f"生成笔记失败：{e}"})
            finally:
                self._notes_running = False

        threading.Thread(target=work, name="notes", daemon=True).start()
        return {"started": True, "transcript": str(path)}

    def _cmd_quit(self, msg: dict) -> dict:
        self._send_threadsafe({"type": "bye"})
        threading.Timer(0.3, self.shutdown).start()
        return {}

    _commands = {
        "toggle_pause": _cmd_toggle_pause,
        "list_devices": _cmd_list_devices,
        "set_device": _cmd_set_device,
        "get_settings": _cmd_get_settings,
        "save_settings": _cmd_save_settings,
        "transcript": _cmd_transcript,
        "generate_notes": _cmd_generate_notes,
        "quit": _cmd_quit,
    }

    async def _handle(self, ws) -> None:
        query = parse_qs(urlparse(ws.request.path).query)
        if query.get("token", [""])[0] != self.token:
            await ws.close(code=4401, reason="bad token")
            return
        self.clients.add(ws)
        try:
            await ws.send(json.dumps(self.hello(), ensure_ascii=False))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                cmd, mid = msg.get("cmd"), msg.get("id")
                fn = self._commands.get(cmd)
                if fn is None:
                    reply = {"type": "reply", "id": mid, "ok": False, "error": f"未知命令：{cmd}"}
                else:
                    try:
                        data = await asyncio.get_running_loop().run_in_executor(None, fn, self, msg)
                        reply = {"type": "reply", "id": mid, "ok": True, "data": data}
                    except Exception as e:
                        if not isinstance(e, ValueError):
                            log.exception("命令 %s 出错", cmd)
                        reply = {"type": "reply", "id": mid, "ok": False, "error": str(e)}
                await ws.send(json.dumps(reply, ensure_ascii=False))
        finally:
            self.clients.discard(ws)

    # ---------- 生命周期 ----------
    def shutdown(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        try:
            if self.pipeline:
                self.pipeline.stop()
        finally:
            if self.loop is not None:
                self.loop.call_soon_threadsafe(self.loop.stop)

    def _watch_parent(self) -> None:
        """Electron 退出时我们的标准输入会被关闭，读到 EOF 就跟着退出。"""
        try:
            while sys.stdin.readline():
                pass
        except Exception:
            pass
        log.info("父进程已退出，后端随之关闭")
        self.shutdown()

    async def _main(self, port: int) -> None:
        from websockets.asyncio.server import serve

        self.loop = asyncio.get_running_loop()
        async with serve(self._handle, "127.0.0.1", port, max_size=2**20) as server:
            real_port = server.sockets[0].getsockname()[1]
            print("@@READY " + json.dumps({"port": real_port, "token": self.token}), flush=True)
            log.info("界面服务已启动：127.0.0.1:%s", real_port)
            await asyncio.Future()  # 一直运行，直到 loop.stop()

    def run(self, port: int = 0, watch_parent: bool = True) -> None:
        threading.Thread(target=self._pump, name="ws-pump", daemon=True).start()
        if watch_parent:
            threading.Thread(target=self._watch_parent, name="parent-watch", daemon=True).start()
        try:
            asyncio.run(self._main(port))
        except RuntimeError:
            pass  # loop.stop() 打断 asyncio.run 时会抛这个，属正常退出


def run_server(cfg: dict, port: int = 0, wav: str | None = None, realtime: bool = True) -> int:
    events: queue.Queue = queue.Queue()
    pipeline = Pipeline(cfg, events)
    server = Server(cfg, pipeline, events)
    pipeline.start(wav_file=wav, realtime=realtime)
    server.run(port)
    os._exit(0)  # 识别/音频线程都是守护线程，直接退出最干净

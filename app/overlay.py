"""置顶悬浮字幕窗（tkinter，无需额外依赖）。

- 无边框、半透明、永远置顶；高度随内容自动变化，底边固定（往上长）
- 顶栏：状态 + 按钮（暂停 / 监听设备 / 原文开关 / 字号 / 锁定 / 设置 / 关闭）
- 鼠标左键按住任意位置拖动；滚轮调字号；右键弹菜单
- 右下角 ◢ 拖动改宽度和高度，双击恢复自动高度
- 锁定后鼠标点击会穿透到下面的窗口，用全局快捷键解锁
- 位置、宽度、字号、透明度、原文开关会记到 ui_state.json，下次启动恢复
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import font as tkfont
from typing import Callable

from .hotkeys import set_click_through
from .languages import Language, get_language

log = logging.getLogger(__name__)

BG = "#0F1115"
FG_ZH = "#FFFFFF"
FG_ZH_PARTIAL = "#C3C9D3"
FG_TH = "#9AA4B2"
FG_TH_PARTIAL = "#6B7280"
FG_STATUS = "#6EE7B7"
FG_WARN = "#FBBF24"
FG_PENDING = "#60A5FA"
FG_BTN = "#8B95A5"
FG_BTN_HOVER = "#FFFFFF"
STATE_FILE = "ui_state.json"


def _enable_dpi_awareness() -> None:
    """让 Windows 不要把窗口当成低分辨率程序去位图放大（否则 150% 缩放下文字发虚）。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 每显示器 DPI 感知
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _pick_font(preferred: list[str], fallback: str) -> str:
    families = set(tkfont.families())
    for name in preferred:
        if name in families:
            return name
    return fallback


class SubtitleOverlay:
    def __init__(
        self,
        cfg: dict,
        events: queue.Queue,
        on_quit: Callable[[], None],
        on_toggle_pause: Callable[[], bool] | None = None,
        state_dir: str | Path | None = None,
        list_devices: Callable[[], list[str]] | None = None,
        on_select_device: Callable[[str], None] | None = None,
        current_device: str = "",
        source_lang: Language | None = None,
        full_cfg: dict | None = None,
        on_restart: Callable[[], None] | None = None,
        transcript_info: Callable[[], tuple[Path, Path | None]] | None = None,
        hotkey_hints: dict[str, str] | None = None,
    ) -> None:
        _enable_dpi_awareness()
        self.cfg = cfg
        self.full_cfg = full_cfg or {}
        self.events = events
        self.on_quit = on_quit
        self.on_toggle_pause = on_toggle_pause
        self.list_devices = list_devices
        self.on_select_device = on_select_device
        self.on_restart = on_restart
        self.transcript_info = transcript_info
        self.hotkey_hints = hotkey_hints or {}
        self.device_hint = current_device  # 空 = 跟随系统默认
        self.lang = source_lang or get_language("th")
        self._state_path = Path(state_dir or ".") / STATE_FILE

        self.entries: list[dict] = []   # [{"id", "source", "text", "partial", "final_text"}]
        self.max_lines = int(cfg["lines"])
        self.show_source = bool(cfg["show_source"])
        self.font_zh_size = int(cfg["font_zh"])
        self.font_th_size = int(cfg.get("font_src", cfg.get("font_th", 14)))
        self.opacity = float(cfg["opacity"])
        self.speech_active = False
        self.paused = False
        self.locked = False
        self.hidden = False
        self._notes_running = False
        self.status_text = "启动中…"
        self.status_color = FG_STATUS
        self._drag: tuple[int, int] | None = None
        self._resize: tuple[int, int, int, int] | None = None
        self.fixed_height: int | None = None   # None = 高度随内容自动；否则为用户拖出来的固定高度
        self._closed = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.scale = self.root.winfo_fpixels("1i") / 96.0   # 150% 缩放时为 1.5
        self._load_state()
        self._build()
        self.root.deiconify()

    # ---------- 状态记忆 ----------
    def _load_state(self) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.width = int(sw * float(self.cfg["width_ratio"]))
        self.x = (sw - self.width) // 2
        self.y_bottom = sh - self.px(70)
        try:
            st = json.loads(self._state_path.read_text(encoding="utf-8"))
            self.width = int(st.get("width", self.width))
            self.x = int(st.get("x", self.x))
            self.y_bottom = int(st.get("y_bottom", self.y_bottom))
            self.font_zh_size = int(st.get("font_zh", self.font_zh_size))
            self.font_th_size = int(st.get("font_src", st.get("font_th", self.font_th_size)))
            self.opacity = float(st.get("opacity", self.opacity))
            self.show_source = bool(st.get("show_source", self.show_source))
            fh = st.get("fixed_height")
            self.fixed_height = int(fh) if fh else None
        except Exception:
            pass
        # 防止上次拖到屏幕外
        self.width = max(self.px(320), min(self.width, sw))
        self.x = max(0, min(self.x, sw - self.width))
        self.y_bottom = max(self.px(120), min(self.y_bottom, sh))

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(json.dumps({
                "width": self.width, "x": self.x, "y_bottom": self.y_bottom,
                "font_zh": self.font_zh_size, "font_src": self.font_th_size,
                "opacity": self.opacity, "show_source": self.show_source,
                "fixed_height": self.fixed_height,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            log.exception("保存窗口状态失败")

    def px(self, logical: int) -> int:
        return int(logical * self.scale)

    # ---------- 窗口 ----------
    def _build(self) -> None:
        r = self.root
        r.title("课堂实时字幕")
        r.overrideredirect(True)
        r.attributes("-topmost", True)
        r.attributes("-alpha", self.opacity)
        r.configure(bg=BG)

        zh_family = _pick_font([self.cfg.get("font_family", ""), "Microsoft YaHei UI", "Microsoft YaHei"], "TkDefaultFont")
        th_family = _pick_font(self.lang.fonts + ["Segoe UI"], zh_family)
        self.font_zh = tkfont.Font(family=zh_family, size=self.font_zh_size, weight="bold")
        self.font_th = tkfont.Font(family=th_family, size=self.font_th_size)
        self.font_status = tkfont.Font(family=zh_family, size=10)
        self.font_btn = tkfont.Font(family=zh_family, size=11)

        # 顶栏：状态 + 按钮
        bar = tk.Frame(r, bg=BG)
        bar.pack(fill="x", padx=self.px(10), pady=(self.px(4), 0))
        self.status = tk.Label(bar, text="", bg=BG, fg=FG_STATUS, font=self.font_status, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self._bind_drag(bar)
        self._bind_drag(self.status)

        self._buttons: dict[str, tk.Label] = {}
        hk = self.hotkey_hints
        for key, text, tip, cmd in [
            ("close", "✕", "关闭", self.quit),
            ("settings", "⚙", "设置", self.open_settings),
            ("lock", "🔒", f"锁定：鼠标点击穿透到下面的窗口（{hk.get('toggle_lock', '')} 解锁）", self.toggle_lock),
            ("bigger", "A+", "字号加大", lambda: self._change_font(2)),
            ("smaller", "A−", "字号减小", lambda: self._change_font(-2)),
            ("source", self.lang.short, f"显示/隐藏{self.lang.name}原文", self._toggle_source),
            ("device", "🔊", "选择监听哪个输出设备", self._popup_device_menu),
            ("pause", "‖", f"暂停/继续（{hk.get('toggle_pause', '')}）", self._toggle_pause),
        ]:
            b = tk.Label(bar, text=text, bg=BG, fg=FG_BTN, font=self.font_btn, cursor="hand2", padx=self.px(6))
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, w=b: w.configure(fg=FG_BTN_HOVER))
            b.bind("<Leave>", lambda e, w=b, k=key: self._style_button(k))
            self._buttons[key] = b
            self._tooltip(b, tip)
        self._style_button("source")

        # 字幕区
        self.body = tk.Frame(r, bg=BG, cursor="fleur")
        self.body.pack(fill="both", expand=True, padx=self.px(16), pady=(self.px(2), self.px(12)))
        self._bind_drag(self.body)

        # 右下角把手：拖动改宽度和高度；双击恢复"高度随内容自动"
        grip = tk.Label(r, text="◢", bg=BG, fg="#6B7280", font=self.font_btn, cursor="size_nw_se")
        grip.place(relx=1.0, rely=1.0, anchor="se", x=-self.px(2))
        grip.bind("<ButtonPress-1>", self._resize_start)
        grip.bind("<B1-Motion>", self._resize_drag)
        grip.bind("<ButtonRelease-1>", self._resize_end)
        grip.bind("<Double-Button-1>", lambda e: self._auto_height())
        self._tooltip(grip, "拖动改宽度/高度，双击恢复自动高度")

        r.bind_all("<MouseWheel>", self._on_wheel)
        r.bind_all("<Button-3>", self._popup_menu)
        r.bind("<Control-q>", lambda e: self.quit())
        r.bind("<Escape>", lambda e: self.quit())
        self._bind_drag(r)

        def acc(action: str) -> str:
            return hk.get(action, "").upper()

        self.menu = tk.Menu(r, tearoff=0)
        self.menu.add_command(label="暂停 / 继续", accelerator=acc("toggle_pause"), command=self._toggle_pause)
        self.menu.add_command(label="隐藏字幕", accelerator=acc("toggle_visible"), command=self.toggle_visible)
        self.menu.add_command(label="锁定（鼠标穿透）", accelerator=acc("toggle_lock"), command=self.toggle_lock)
        self.menu.add_command(label=f"显示 / 隐藏{self.lang.name}原文", command=self._toggle_source)
        self.device_menu = tk.Menu(self.menu, tearoff=0)
        self.menu.add_cascade(label="监听设备", menu=self.device_menu)
        self.device_menu.configure(postcommand=self._fill_device_menu)
        self.menu.add_separator()
        self.menu.add_command(label="打开本节课记录", command=self._open_transcript)
        self.menu.add_command(label="打开记录文件夹", command=self._open_transcript_dir)
        self.menu.add_command(label="生成本节课笔记", command=self._generate_notes)
        self.menu.add_separator()
        self.menu.add_command(label="字号 +", command=lambda: self._change_font(2))
        self.menu.add_command(label="字号 −", command=lambda: self._change_font(-2))
        self.menu.add_command(label="更不透明", command=lambda: self._change_opacity(0.08))
        self.menu.add_command(label="更透明", command=lambda: self._change_opacity(-0.08))
        self.menu.add_command(label="清空字幕", command=self._clear)
        self.menu.add_command(label="高度随内容自动", command=self._auto_height)
        self.menu.add_command(label="重置位置和大小", command=self._reset_layout)
        self.menu.add_separator()
        self.menu.add_command(label="设置…", command=self.open_settings)
        self.menu.add_command(label="退出", command=self.quit)

        self._render()
        r.after(80, self._poll)

    def _style_button(self, key: str) -> None:
        b = self._buttons.get(key)
        if not b:
            return
        if key == "source":
            b.configure(fg=FG_STATUS if self.show_source else FG_BTN)
        elif key == "pause":
            b.configure(text="▶" if self.paused else "‖", fg=FG_WARN if self.paused else FG_BTN)
        elif key == "lock":
            b.configure(fg=FG_WARN if self.locked else FG_BTN)
        else:
            b.configure(fg=FG_BTN)

    def _tooltip(self, widget: tk.Widget, text: str) -> None:
        """极简悬停提示。"""
        tip: dict = {"win": None}

        def show(_e):
            if tip["win"] is not None:
                return
            w = tk.Toplevel(widget)
            w.overrideredirect(True)
            w.attributes("-topmost", True)
            tk.Label(w, text=text, bg="#1F2937", fg="#E5E7EB", font=self.font_status,
                     padx=self.px(6), pady=self.px(2)).pack()
            w.geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() - self.px(28)}")
            tip["win"] = w

        def hide(_e):
            if tip["win"] is not None:
                tip["win"].destroy()
                tip["win"] = None

        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<Button-1>", hide, add="+")

    # ---------- 事件轮询 ----------
    def _poll(self) -> None:
        if self._closed:
            return
        changed = False
        try:
            while True:
                ev = self.events.get_nowait()
                changed |= self._handle(ev)
        except queue.Empty:
            pass
        try:
            if changed:
                self._render()
            self.root.after(80, self._poll)
        except tk.TclError:
            pass  # 窗口已销毁

    def _handle(self, ev: dict) -> bool:
        t = ev.get("type")
        if t == "status":
            self.status_text, self.status_color = ev["text"], FG_STATUS
            if any(k in ev["text"] for k in ("跟不上", "失败", "跳过", "没听到", "错误", "占用", "超时")):
                self.status_color = FG_WARN
            return True
        if t == "speech":
            self.speech_active = bool(ev["active"])
            return True
        if t in ("partial", "transcript"):
            e = self._entry(ev["id"])
            e["source"] = ev["source"]
            e["partial"] = t == "partial"
            return True
        if t == "translation":
            e = self._find(ev["id"])
            if e is None:
                return False
            if ev.get("partial") and e["final_text"]:
                return False  # 正式译文已经到了，迟到的中间译文不要覆盖
            e["text"] = ev["text"]
            e["final_text"] = not ev.get("partial")
            return True
        if t == "discard":
            e = self._find(ev["id"])
            if e is not None and e["partial"]:
                self.entries.remove(e)
                return True
            return False
        if t == "fatal":
            self.status_text, self.status_color = "错误：" + ev["text"], FG_WARN
            return True
        if t == "hotkey":
            action = ev.get("action")
            if action == "toggle_visible":
                self.toggle_visible()
            elif action == "toggle_pause":
                self._toggle_pause()
            elif action == "toggle_lock":
                self.toggle_lock()
            return True
        if t == "open_file":
            self._startfile(ev["path"])
            return False
        return False

    def _find(self, sid: int) -> dict | None:
        for e in self.entries:
            if e["id"] == sid:
                return e
        return None

    def _entry(self, sid: int) -> dict:
        e = self._find(sid)
        if e is None:
            e = {"id": sid, "source": "", "text": None, "partial": True, "final_text": False}
            self.entries.append(e)
            del self.entries[:-self._keep_lines()]
        return e

    def _keep_lines(self) -> int:
        """自动高度时只留 lines 句；固定高度时多留一些，由窗口高度决定能看到几句。"""
        return self.max_lines if self.fixed_height is None else max(self.max_lines, 12)

    # ---------- 渲染 ----------
    def _render(self) -> None:
        dot = "  ●" if self.speech_active else ""
        lock = f"🔒 已锁定（{self.hotkey_hints.get('toggle_lock', '').upper()} 解锁）  " if self.locked else ""
        self.status.configure(text=lock + self.status_text + dot, fg=self.status_color)

        for child in self.body.winfo_children():
            child.destroy()
        wrap = max(self.px(200), self.width - self.px(40))
        # 从最新一句开始、自底向上摆放：固定高度放不下时，被挤掉的是顶部的旧句子
        for e in reversed(self.entries):
            if e["text"] is None:
                self._line("识别中…" if e["partial"] else "翻译中…", self.font_th, FG_PENDING, wrap, pady=(0, self.px(4)))
            elif e["text"]:
                if e["final_text"]:
                    self._line(e["text"], self.font_zh, FG_ZH, wrap, pady=(0, self.px(4)))
                else:
                    self._line(e["text"] + " …", self.font_zh, FG_ZH_PARTIAL, wrap, pady=(0, self.px(4)))
            if self.show_source and e["source"]:
                self._line(e["source"], self.font_th, FG_TH_PARTIAL if e["partial"] else FG_TH, wrap, pady=(self.px(6), 0))
        if not self.entries:
            self._line("等待老师说话…", self.font_th, FG_TH_PARTIAL, wrap, pady=(self.px(8), self.px(8)))
        self._fit()

    def _line(self, text: str, font: tkfont.Font, fg: str, wrap: int, pady) -> None:
        lbl = tk.Label(self.body, text=text, font=font, fg=fg, bg=BG, wraplength=wrap, justify="center")
        lbl.pack(side="bottom", fill="x", pady=pady)
        self._bind_drag(lbl)

    def _fit(self) -> None:
        """自动模式：高度随内容变化；固定模式：用用户拖出来的高度。底边固定不动。"""
        self.root.update_idletasks()
        if self.fixed_height is not None:
            h = self.fixed_height
        else:
            h = max(self.px(60), self.root.winfo_reqheight())
        self.root.geometry(f"{self.width}x{h}+{self.x}+{self.y_bottom - h}")

    # ---------- 交互 ----------
    def _bind_drag(self, w: tk.Widget) -> None:
        w.bind("<ButtonPress-1>", self._drag_start, add="+")
        w.bind("<B1-Motion>", self._drag_move, add="+")
        w.bind("<ButtonRelease-1>", self._drag_end, add="+")

    def _drag_start(self, ev) -> str:
        self._drag = (ev.x_root - self.root.winfo_x(), ev.y_root - self.root.winfo_y())
        return "break"

    def _drag_move(self, ev) -> str:
        if self._drag:
            ox, oy = self._drag
            self.root.geometry(f"+{ev.x_root - ox}+{ev.y_root - oy}")
        return "break"

    def _drag_end(self, ev) -> str:  # noqa: ARG002
        if self._drag:
            self._drag = None
            self.x = self.root.winfo_x()
            self.y_bottom = self.root.winfo_y() + self.root.winfo_height()
            self._save_state()
        return "break"

    def _resize_start(self, ev) -> str:
        self._resize = (ev.x_root, ev.y_root, self.width, self.root.winfo_height())
        return "break"

    def _resize_drag(self, ev) -> str:
        if self._resize:
            x0, y0, w0, h0 = self._resize
            self.width = max(self.px(320), w0 + ev.x_root - x0)
            dy = ev.y_root - y0
            if abs(dy) >= self.px(6):
                # 往下拖窗口变高（底边固定，所以实际是整体往上长）
                self.fixed_height = max(self.px(70), h0 + dy)
            self._render()
        return "break"

    def _resize_end(self, ev) -> str:  # noqa: ARG002
        self._resize = None
        self._save_state()
        return "break"

    def _auto_height(self) -> None:
        self.fixed_height = None
        del self.entries[:-self.max_lines]
        self._render()
        self._save_state()

    def _popup_menu(self, ev) -> None:
        try:
            self.menu.tk_popup(ev.x_root, ev.y_root)
        finally:
            self.menu.grab_release()

    # ---------- 监听设备选择 ----------
    def _fill_device_menu(self) -> None:
        self.device_menu.delete(0, "end")
        self._device_var = tk.StringVar(value=self.device_hint)
        self.device_menu.add_radiobutton(label="跟随系统默认输出", value="", variable=self._device_var,
                                         command=lambda: self._select_device(""))
        self.device_menu.add_separator()
        names = self.list_devices() if self.list_devices else []
        if not names:
            self.device_menu.add_command(label="（没有找到可监听的设备）", state="disabled")
        for name in names:
            # 选中态：当前设置的关键字能匹配到这个设备名
            if self.device_hint and self.device_hint.lower() in name.lower():
                self._device_var.set(name)
            self.device_menu.add_radiobutton(label=name, value=name, variable=self._device_var,
                                             command=lambda n=name: self._select_device(n))

    def _popup_device_menu(self) -> None:
        b = self._buttons.get("device")
        if b is None:
            return
        self._fill_device_menu()
        try:
            self.device_menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            self.device_menu.grab_release()

    def _select_device(self, name: str) -> None:
        self.device_hint = name
        if self.on_select_device:
            try:
                self.on_select_device(name)
            except Exception:
                log.exception("切换设备失败")
        self._save_state()

    def _on_wheel(self, ev) -> str:
        self._change_font(2 if ev.delta > 0 else -2)
        return "break"

    def _change_font(self, delta: int) -> None:
        self.font_zh_size = max(12, min(72, self.font_zh_size + delta))
        self.font_th_size = max(9, min(48, self.font_th_size + (1 if delta > 0 else -1)))
        self.font_zh.configure(size=self.font_zh_size)
        self.font_th.configure(size=self.font_th_size)
        self._render()
        self._save_state()

    def _change_opacity(self, delta: float) -> None:
        self.opacity = min(1.0, max(0.3, self.opacity + delta))
        self.root.attributes("-alpha", self.opacity)
        self._save_state()

    def _toggle_source(self) -> None:
        self.show_source = not self.show_source
        self._style_button("source")
        self._render()
        self._save_state()

    def _toggle_pause(self) -> None:
        if self.on_toggle_pause:
            self.paused = bool(self.on_toggle_pause())
            self._style_button("pause")

    # ---------- 隐藏 / 锁定 ----------
    def toggle_visible(self) -> None:
        if self.hidden:
            self.root.deiconify()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            self.root.attributes("-alpha", self.opacity)
            if self.locked:  # 重新显示后窗口样式会被重置，锁定状态要再设一次
                set_click_through(self.root.winfo_id(), True)
            self._render()
        else:
            self.root.withdraw()
        self.hidden = not self.hidden

    def toggle_lock(self) -> None:
        if not set_click_through(self.root.winfo_id(), not self.locked):
            self.status_text, self.status_color = "当前系统不支持锁定", FG_WARN
            self._render()
            return
        self.locked = not self.locked
        self._style_button("lock")
        self._render()

    # ---------- 记录 / 笔记 / 设置 ----------
    @staticmethod
    def _startfile(path) -> None:
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]  # Windows 专用
        except Exception:
            log.exception("打开文件失败：%s", path)

    def _open_transcript(self) -> None:
        if not self.transcript_info:
            return
        directory, path = self.transcript_info()
        if path is not None and path.exists():
            self._startfile(path)
        else:
            self.status_text, self.status_color = "这节课还没有记录（还没识别到内容）", FG_WARN
            self._render()

    def _open_transcript_dir(self) -> None:
        if not self.transcript_info:
            return
        directory, _ = self.transcript_info()
        directory.mkdir(parents=True, exist_ok=True)
        self._startfile(directory)

    def _generate_notes(self) -> None:
        if self._notes_running or not self.transcript_info:
            return
        directory, path = self.transcript_info()
        if path is None or not path.exists():
            from .review import latest_transcript
            path = latest_transcript(directory)
        if path is None:
            self.status_text, self.status_color = "没有可以生成笔记的课堂记录", FG_WARN
            self._render()
            return
        self._notes_running = True
        events = self.events

        def work():
            from .review import generate_notes
            try:
                out = generate_notes(path, self.full_cfg,
                                     on_progress=lambda s: events.put({"type": "status", "text": s}))
                events.put({"type": "open_file", "path": str(out)})
            except Exception as e:
                log.exception("生成笔记失败")
                events.put({"type": "status", "text": f"生成笔记失败：{e}"})
            finally:
                self._notes_running = False

        threading.Thread(target=work, name="notes", daemon=True).start()

    def open_settings(self) -> None:
        if not self.full_cfg:
            return
        from .settings_dialog import SettingsDialog

        def saved(restart: bool) -> None:
            if restart and self.on_restart:
                self.on_restart()
                self.quit()
            else:
                self.status_text, self.status_color = "设置已保存，重启后生效", FG_STATUS
                self._render()

        SettingsDialog(self.full_cfg, master=self.root, on_saved=saved)

    def _clear(self) -> None:
        self.entries.clear()
        self._render()

    def _reset_layout(self) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.width = int(sw * float(self.cfg["width_ratio"]))
        self.x = (sw - self.width) // 2
        self.y_bottom = sh - self.px(70)
        self.font_zh_size, self.font_th_size = int(self.cfg["font_zh"]), int(self.cfg.get("font_src", 14))
        self.font_zh.configure(size=self.font_zh_size)
        self.font_th.configure(size=self.font_th_size)
        self.fixed_height = None
        self._render()
        self._save_state()

    def quit(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._save_state()
        try:
            self.on_quit()
        except Exception:
            log.exception("停止流水线时出错")
        finally:
            try:
                self.root.destroy()
            except tk.TclError:
                pass

    def run(self) -> None:
        self.root.mainloop()

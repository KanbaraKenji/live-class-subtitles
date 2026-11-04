"""Windows 全局热键 + 鼠标穿透（只用 user32，不需要额外依赖）。

全局热键：在 Zoom / Teams 窗口里按也有效。实现方式是在一个后台线程里
RegisterHotKey，然后用 GetMessage 等 WM_HOTKEY；回调里只往队列里塞事件，
真正的界面操作交给 tkinter 主线程执行。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
GA_ROOT = 2

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_NAMED_KEYS = {
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+h' -> (修饰键位掩码, 虚拟键码)。格式不对时抛 ValueError。"""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("空的快捷键")
    mods, key = 0, None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        elif key is None:
            key = p
        else:
            raise ValueError(f"快捷键只能有一个主键：{spec}")
    if key is None:
        raise ValueError(f"缺少主键：{spec}")
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key in _NAMED_KEYS:
        vk = _NAMED_KEYS[key]
    else:
        raise ValueError(f"不认识的按键：{key}")
    if mods == 0:
        raise ValueError(f"全局快捷键至少要带一个修饰键（ctrl/alt/shift/win）：{spec}")
    return mods, vk


class GlobalHotkeys:
    """注册一组全局热键；按下时调用 on_trigger(动作名)（在热键线程里调用，回调要线程安全）。"""

    def __init__(self, bindings: dict[str, str], on_trigger: Callable[[str], None]) -> None:
        self.bindings = bindings          # {动作名: "ctrl+alt+h"}
        self.on_trigger = on_trigger
        self.registered: dict[str, str] = {}
        self.failed: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()

    def start(self) -> None:
        if sys.platform != "win32" or not self.bindings:
            return
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(2)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        ids: dict[int, str] = {}
        for i, (action, spec) in enumerate(self.bindings.items(), start=1):
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as e:
                self.failed[action] = str(e)
                continue
            if user32.RegisterHotKey(None, i, mods | MOD_NOREPEAT, vk):
                ids[i] = action
                self.registered[action] = spec
            else:
                self.failed[action] = f"{spec} 已被其他程序占用"
        if self.failed:
            log.warning("部分快捷键注册失败：%s", self.failed)
        log.info("全局快捷键：%s", self.registered)
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                try:
                    self.on_trigger(ids[msg.wParam])
                except Exception:
                    log.exception("快捷键回调出错")
        for i in ids:
            user32.UnregisterHotKey(None, i)


def set_click_through(widget_id: int, enabled: bool) -> bool:
    """让窗口的鼠标点击穿透到下面的窗口（锁定字幕时用）。返回是否成功。"""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = wintypes.HWND
    hwnd = user32.GetAncestor(wintypes.HWND(widget_id), GA_ROOT)
    if not hwnd:
        return False
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if enabled:
        style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
    else:
        style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    return True


def is_click_through(widget_id: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    user32.GetAncestor.restype = wintypes.HWND
    hwnd = user32.GetAncestor(wintypes.HWND(widget_id), GA_ROOT)
    return bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TRANSPARENT)

"""界面冒烟测试：在一个进程里启动字幕窗、回放测试音频，然后自动操作并检查。

  python tests/ui_smoke.py [tests/thai_sample.wav] [--shot docs/screenshot.png]

检查项：快捷键注册、热键消息能触发动作、锁定（鼠标穿透）开关、隐藏/显示、设置窗口能打开关闭、
字幕至少出现一句正式译文。需要 Windows + 识别模型；会真实调用翻译 API（几句话）。
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.hotkeys import WM_HOTKEY, GlobalHotkeys, is_click_through  # noqa: E402
from app.languages import get_language  # noqa: E402
from app.overlay import SubtitleOverlay, _enable_dpi_awareness  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  ({detail})" if detail else ""), flush=True)


def screenshot(widget_id: int, path: Path) -> None:
    """用 PrintWindow 截字幕窗（普通截屏抓不到分层窗口）。"""
    from ctypes import wintypes

    from PIL import Image  # faster-whisper 依赖里没有 PIL，缺了就跳过

    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetAncestor.restype = wintypes.HWND
    hwnd = user32.GetAncestor(wintypes.HWND(widget_id), 2)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]

    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB").save(path)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", default="tests/thai_sample.wav")
    ap.add_argument("--shot", default=None, help="截图保存路径")
    ap.add_argument("--realtime", action="store_true", help="按真实速度回放（截图时用，字幕更完整）")
    ap.add_argument("--min-lines", type=int, default=1, help="至少出现几句正式译文再继续")
    ap.add_argument("--asr-model", default=None, help="覆盖识别模型（例如显卡被占用时用 small + cpu）")
    ap.add_argument("--asr-device", default=None, help="覆盖识别设备 auto/cuda/cpu")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING)

    _enable_dpi_awareness()
    cfg = load_config()
    cfg["log"]["enabled"] = False
    if args.asr_model:
        cfg["asr"]["model"] = args.asr_model
    if args.asr_device:
        cfg["asr"]["device"] = args.asr_device
    events: queue.Queue = queue.Queue()
    pipeline = Pipeline(cfg, events)
    bindings = {"toggle_visible": "ctrl+alt+shift+f10", "toggle_pause": "ctrl+alt+shift+f11",
                "toggle_lock": "ctrl+alt+shift+f12"}  # 测试用冷门组合，避免和真实使用冲突
    hk = GlobalHotkeys(bindings, on_trigger=lambda a: events.put({"type": "hotkey", "action": a}))
    hk.start()
    check("快捷键全部注册成功", set(hk.registered) == set(bindings), str(hk.failed or ""))

    ov = SubtitleOverlay(cfg["ui"], events, on_quit=lambda: (hk.stop(), pipeline.stop()),
                         on_toggle_pause=pipeline.toggle_pause, state_dir=Path("."),
                         source_lang=get_language(cfg["asr"]["language"]), full_cfg=cfg,
                         transcript_info=lambda: (pipeline.logbook.directory, pipeline.logbook.path),
                         hotkey_hints=hk.registered)
    ov._save_state = lambda: None  # 测试不要改动真实的窗口记忆
    pipeline.start(wav_file=args.wav, realtime=args.realtime)
    root = ov.root
    t0 = time.time()

    def wait_for_translation():
        n = sum(1 for e in ov.entries if e.get("final_text"))
        done = n >= args.min_lines
        if done or time.time() - t0 > 180:
            check("出现正式译文", done, f"{n} 句")
            root.after(1500, step_screenshot)
        else:
            root.after(500, wait_for_translation)

    def step_screenshot():
        if args.shot:
            try:
                screenshot(root.winfo_id(), Path(args.shot))
                check("截图", Path(args.shot).exists(), args.shot)
            except Exception as e:
                check("截图", False, repr(e))
        step_lock()

    def step_lock():
        ov.toggle_lock()
        check("锁定后鼠标穿透", is_click_through(root.winfo_id()) and ov.locked)
        ov.toggle_lock()
        check("解锁后恢复点击", not is_click_through(root.winfo_id()) and not ov.locked)
        root.after(200, step_hotkey)

    def step_hotkey():
        # 往热键线程发一条 WM_HOTKEY（等同于用户按下了 toggle_lock 的组合键）
        lock_id = list(bindings).index("toggle_lock") + 1
        ctypes.windll.user32.PostThreadMessageW(hk._thread_id, WM_HOTKEY, lock_id, 0)
        root.after(600, step_hotkey_check)

    def step_hotkey_check():
        check("热键触发锁定", ov.locked and is_click_through(root.winfo_id()))
        ov.toggle_lock()
        ov.toggle_visible()
        check("隐藏字幕", root.state() == "withdrawn")
        ov.toggle_visible()
        root.after(300, step_visible_check)

    def step_visible_check():
        check("重新显示字幕", root.state() == "normal" and root.winfo_viewable() == 1)
        ov.open_settings()
        root.after(800, step_settings_check)

    def step_settings_check():
        tops = [w for w in root.winfo_children() if isinstance(w, __import__("tkinter").Toplevel)
                and "设置" in w.title()]
        check("设置窗口能打开", len(tops) == 1)
        for w in tops:
            w.destroy()
        root.after(200, finish)

    def finish():
        ov.quit()

    root.after(500, wait_for_translation)
    ov.run()
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

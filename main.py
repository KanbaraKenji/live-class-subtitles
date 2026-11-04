"""课堂实时字幕 —— 程序入口。

用法：
  python main.py                        正常使用：监听系统声音，显示字幕
  python main.py --settings             打开设置窗口
  python main.py --list-devices         列出可监听的输出设备
  python main.py --review [记录文件]     给一节课生成复习笔记（默认最新一节）
  python main.py --wav test.wav         用录音文件模拟上课（测试用）
  python main.py --wav test.wav --fast  不按实时速度回放，尽快处理完
"""

from __future__ import annotations

import argparse
import logging
import queue
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import has_api_key, load_config, save_user_settings  # noqa: E402


def setup_logging(project_dir: str) -> None:
    log_dir = Path(project_dir) / "logs"
    log_dir.mkdir(exist_ok=True)
    try:  # Windows 控制台默认 GBK，泰文/中文日志会乱码
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
        ],
    )
    for noisy in ("faster_whisper", "httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def run_review(cfg: dict, target: str | None) -> int:
    from app.review import generate_notes, latest_transcript

    directory = Path(cfg["_project_dir"]) / cfg["log"]["dir"]
    path = Path(target) if target else latest_transcript(directory)
    if path is None or not path.exists():
        print(f"找不到课堂记录（{directory}）")
        return 1
    print(f"课堂记录：{path}")
    try:
        out = generate_notes(path, cfg, on_progress=print)
    except RuntimeError as e:
        print(f"生成失败：{e}")
        return 1
    print(f"笔记：{out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="课堂实时中文字幕")
    ap.add_argument("--config", default=None, help="配置文件路径（默认 config.yaml）")
    ap.add_argument("--settings", action="store_true", help="打开设置窗口")
    ap.add_argument("--list-devices", action="store_true", help="列出可监听的音频输出设备")
    ap.add_argument("--review", nargs="?", const="", default=None, metavar="记录文件",
                    help="生成课后笔记（不写文件名则用最新一节课）")
    ap.add_argument("--wav", default=None, help="用 wav 文件代替系统声音（测试）")
    ap.add_argument("--fast", action="store_true", help="配合 --wav：不按实时速度回放")
    args = ap.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg["_project_dir"])

    if args.list_devices:
        from app.audio_capture import list_loopback_devices

        for d in list_loopback_devices():
            print(f"[{d['index']:>2}] {d['name']}  ({int(d['defaultSampleRate'])} Hz, {d['maxInputChannels']} ch)")
        return 0

    if args.review is not None:
        return run_review(cfg, args.review or None)

    from app.overlay import _enable_dpi_awareness
    from app.settings_dialog import SettingsDialog

    _enable_dpi_awareness()
    # 第一次运行（选了 Claude 但没有 Key）或手动 --settings：先弹设置窗口
    if args.settings or (cfg["translate"]["backend"] == "claude" and not has_api_key()):
        if not SettingsDialog(cfg, first_run=not args.settings).run():
            return 0
        cfg = load_config(args.config)

    from app.hotkeys import GlobalHotkeys
    from app.languages import get_language
    from app.overlay import SubtitleOverlay
    from app.pipeline import Pipeline

    events: queue.Queue = queue.Queue()
    pipeline = Pipeline(cfg, events)

    hk_cfg = cfg.get("hotkeys", {})
    bindings = {k: v for k, v in hk_cfg.items() if k != "enabled" and v} if hk_cfg.get("enabled", True) else {}
    hotkeys = GlobalHotkeys(bindings, on_trigger=lambda action: events.put({"type": "hotkey", "action": action}))
    hotkeys.start()
    if hotkeys.failed:
        events.put({"type": "status", "text": "部分快捷键被占用：" + "；".join(hotkeys.failed.values())})

    def select_device(name: str) -> None:
        pipeline.set_device(name)
        save_user_settings({"audio": {"device": name}})  # 记住选择

    def restart() -> None:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())] + sys.argv[1:],
                         cwd=cfg["_project_dir"])

    def stop() -> None:
        hotkeys.stop()
        pipeline.stop()

    overlay = SubtitleOverlay(
        cfg["ui"], events,
        on_quit=stop,
        on_toggle_pause=pipeline.toggle_pause,
        state_dir=cfg["_project_dir"],
        list_devices=pipeline.list_devices,
        on_select_device=select_device,
        current_device=pipeline.device_hint,
        source_lang=get_language(cfg["asr"]["language"]),
        full_cfg=cfg,
        on_restart=restart,
        transcript_info=lambda: (pipeline.logbook.directory, pipeline.logbook.path),
        hotkey_hints=hotkeys.registered,
    )
    pipeline.start(wav_file=args.wav, realtime=not args.fast)
    overlay.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

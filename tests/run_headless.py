"""无界面跑通完整流程：wav → VAD → 识别 → 翻译，把事件打印出来。

用法：python tests/run_headless.py tests/thai_sample.wav [--backend free|claude|none]
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--backend", default=None)
    ap.add_argument("--model", default=None, help="识别模型名")
    ap.add_argument("--timeout", type=float, default=240)
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(threadName)s] %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = load_config()
    cfg["log"]["enabled"] = False
    if args.backend:
        cfg["translate"]["backend"] = args.backend
    if args.model:
        cfg["asr"]["model"] = args.model

    events: queue.Queue = queue.Queue()
    p = Pipeline(cfg, events)
    p.start(wav_file=args.wav, realtime=False)

    pending: set[int] = set()
    finished_file = False
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            ev = events.get(timeout=0.5)
        except queue.Empty:
            if finished_file and not pending and not p._finals and p._work.empty():
                break
            continue
        t = ev["type"]
        if t == "partial":
            print(f"  (中间 #{ev['id']}) {ev['source']}")
        elif t == "transcript":
            pending.add(ev["id"])
            print(f"\n[识别 #{ev['id']}] {ev['source']}")
        elif t == "translation":
            if ev.get("partial"):
                print(f"  (中间译 #{ev['id']}) {ev['text']}")
            else:
                pending.discard(ev["id"])
                print(f"[翻译 #{ev['id']}] {ev['text']}")
        elif t == "discard":
            pending.discard(ev["id"])
        elif t == "status":
            print(f"[状态] {ev['text']}")
            if "回放结束" in ev["text"]:
                finished_file = True
        elif t == "fatal":
            print(f"[致命错误] {ev['text']}")
            p.stop()
            return 1
    p.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

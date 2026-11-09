"""显存检查：显卡被其他程序占得太满时，识别会从 1 秒慢到十几秒，提前提醒用户。

用 nvidia-smi 查显存（装了 NVIDIA 驱动就有），用 tasklist 找常见的占显卡程序。
查不到（没有 NVIDIA 显卡、命令不存在）时什么都不做。
"""

from __future__ import annotations

import csv
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

LOW_FREE_GB = 1.0      # 剩余显存低于这个值就提醒
RECOVER_FREE_GB = 1.5  # 回升到这个值以上，才允许下次再提醒（避免来回刷屏）

# 常见的会长期占着显存的程序：进程名 -> 显示名
KNOWN_GPU_APPS = {
    "lm studio.exe": "LM Studio",
    "ollama.exe": "Ollama",
    "ollama app.exe": "Ollama",
    "comfyui.exe": "ComfyUI",
    "stable diffusion.exe": "Stable Diffusion",
    "jan.exe": "Jan",
    "gpt4all.exe": "GPT4All",
    "koboldcpp.exe": "KoboldCpp",
    "obs64.exe": "OBS",
}

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=5, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def parse_vram(text: str) -> tuple[float, float] | None:
    """解析 nvidia-smi 的 'used, total'（MiB），返回 (已用 GB, 总量 GB)，只看第一块显卡。"""
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                return float(parts[0]) / 1024, float(parts[1]) / 1024
            except ValueError:
                continue
    return None


def query_vram() -> tuple[float, float] | None:
    fake = os.environ.get("LCS_FAKE_VRAM_MIB")  # 测试用，例如 "7362,8192"
    if fake:
        return parse_vram(fake)
    out = _run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"])
    return parse_vram(out) if out else None


def parse_gpu_apps(tasklist_csv: str) -> list[str]:
    """从 tasklist /FO CSV /NH 的输出里找出已知的占显卡程序（去重，保持顺序）。"""
    found: list[str] = []
    for row in csv.reader((tasklist_csv or "").splitlines()):
        if not row:
            continue
        name = KNOWN_GPU_APPS.get(row[0].strip().lower())
        if name and name not in found:
            found.append(name)
    return found


def running_gpu_apps() -> list[str]:
    if sys.platform != "win32":
        return []
    out = _run(["tasklist", "/FO", "CSV", "/NH"])
    return parse_gpu_apps(out) if out else []


def vram_warning(used_gb: float, total_gb: float, apps: list[str]) -> str | None:
    """剩余显存不足时返回给用户看的提醒，否则返回 None。"""
    free = total_gb - used_gb
    if free >= LOW_FREE_GB:
        return None
    who = "、".join(apps) if apps else "游戏、本地大模型等"
    return f"显存快满了（只剩 {max(free, 0):.1f}GB），字幕可能变慢。请关掉占用显卡的程序：{who}"


class VramWatch:
    """记住上次是否提醒过：同一次“显存紧张”只提醒一次，恢复后才会再提醒。"""

    def __init__(self) -> None:
        self.warned = False

    def check(self) -> str | None:
        v = query_vram()
        if v is None:
            return None
        used, total = v
        free = total - used
        if self.warned:
            if free >= RECOVER_FREE_GB:
                self.warned = False
                log.info("显存已恢复：剩余 %.1fGB", free)
            return None
        msg = vram_warning(used, total, running_gpu_apps() if free < LOW_FREE_GB else [])
        if msg:
            self.warned = True
            log.warning("%s（已用 %.1f / %.1fGB）", msg, used, total)
        return msg

"""语音识别：faster-whisper（本地运行，支持泰语）。

GPU（CUDA）可用时用 float16，否则退回 CPU int8。
Windows 下 CUDA 运行库通过 pip 的 nvidia-cublas-cu12 / nvidia-cudnn-cu12 提供，
这里会自动把它们的 bin 目录加入 DLL 搜索路径。
"""

from __future__ import annotations

import logging
import os
import site
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Whisper 在静音/噪音上常"幻听"出来的泰文/英文套话，直接过滤
HALLUCINATIONS = {
    "ขอบคุณครับ", "ขอบคุณค่ะ", "ขอบคุณ", "สวัสดีครับ", "สวัสดีค่ะ",
    "บรรยายโดย", "คำบรรยายโดย", "ซับไทยโดย", "แปลโดย", "โปรดติดตามตอนต่อไป",
    "thank you", "thanks for watching", "subtitles by", "you",
    "字幕由", "謝謝觀看", "谢谢观看",
}


def _add_nvidia_dll_dirs() -> None:
    """把 pip 安装的 CUDA 库（cublas / cudnn）加入 DLL 搜索路径。"""
    if sys.platform != "win32":
        return
    roots = set(site.getsitepackages() + [site.getusersitepackages()])
    for root in roots:
        nv = Path(root) / "nvidia"
        if not nv.is_dir():
            continue
        for sub in nv.iterdir():
            bin_dir = sub / "bin"
            if bin_dir.is_dir():
                try:
                    os.add_dll_directory(str(bin_dir))
                except OSError:
                    pass
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


class WhisperASR:
    def __init__(
        self,
        model: str = "large-v3",
        device: str = "auto",
        compute_type: str = "auto",
        language: str = "th",
        beam_size: int = 5,
        initial_prompt: str = "",
        workers: int = 2,
    ) -> None:
        self.model_name = model
        self.device_pref = device
        self.compute_pref = compute_type
        self.language = None if language in ("", "auto", None) else language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt or None
        self.workers = max(1, int(workers))  # 允许几个线程同时调用模型（并行识别多段）
        self.model = None
        self.device = ""
        self.compute_type = ""

    # ---------- 加载 ----------
    def load(self) -> None:
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        candidates: list[tuple[str, str]] = []
        if self.device_pref in ("auto", "cuda"):
            ct = self.compute_pref if self.compute_pref != "auto" else "float16"
            candidates.append(("cuda", ct))
        if self.device_pref in ("auto", "cpu"):
            ct = self.compute_pref if self.compute_pref != "auto" else "int8"
            candidates.append(("cpu", ct))

        last_err: Exception | None = None
        for device, ct in candidates:
            try:
                log.info("加载识别模型 %s（%s / %s）…", self.model_name, device, ct)
                self.model = WhisperModel(self.model_name, device=device, compute_type=ct, num_workers=self.workers)
                self.device, self.compute_type = device, ct
                log.info("识别模型加载完成")
                return
            except Exception as e:  # CUDA 不可用、显存不够等
                last_err = e
                log.warning("用 %s 加载失败：%s", device, e)
        raise RuntimeError(f"识别模型加载失败：{last_err}")

    # ---------- 识别 ----------
    def transcribe(self, audio: np.ndarray, fast: bool = False) -> str | None:
        """输入 16kHz float32 单声道，返回文本；噪音/幻听返回 None。

        fast=True 用于"边说边出字"的中间结果：贪心解码，速度优先。
        """
        if self.model is None:
            raise RuntimeError("模型尚未加载")
        duration = len(audio) / 16000
        segments, info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=1 if fast else self.beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=self.initial_prompt,
            no_speech_threshold=0.6,
            without_timestamps=True,
            # 关键：temperature 只给 0，关闭"压缩比过高就升温重试"的机制。
            # 老师重复念单词时 Whisper 容易陷入复读循环，默认会连续重试 5 次，一段 6 秒音频能卡 10 秒以上。
            temperature=0.0,
            repetition_penalty=1.15,
            # 泰语大约每秒 15~20 个 token，封顶防止复读循环跑满
            max_new_tokens=min(440, int(duration * 22) + 30),
        )
        texts: list[str] = []
        for seg in segments:
            t = seg.text.strip()
            if not t:
                continue
            if seg.no_speech_prob > 0.75 and seg.avg_logprob < -1.0:
                continue
            if seg.compression_ratio > 2.6:  # 重复刷屏式幻听
                continue
            texts.append(t)
        text = " ".join(texts).strip()
        if not text or text.strip(" .。!?").lower() in HALLUCINATIONS:
            return None
        # Whisper 在很短的片段上有时会把 initial_prompt 原样"复读"出来
        if self.initial_prompt:
            norm = text.replace(" ", "")
            prompt_norm = self.initial_prompt.replace(" ", "")
            if norm in prompt_norm or prompt_norm in norm:
                log.debug("丢弃疑似复读提示词的结果：%s", text)
                return None
        if self.language is None:
            log.debug("检测语言：%s (%.2f)", info.language, info.language_probability)
        return text

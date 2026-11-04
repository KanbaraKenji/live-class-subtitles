"""语音活动检测（VAD）+ 按句切分。

把连续的音频流切成"老师说的一句话"，再交给识别模型。
采用 WebRTC VAD：每 30ms 判断一次有没有人声，
连续静音超过 silence_ms 就认为一句话说完了。
"""

from __future__ import annotations

import collections
import logging
from typing import Callable

import numpy as np
import webrtcvad

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000


class SpeechSegmenter:
    def __init__(
        self,
        on_segment: Callable[[np.ndarray], None],
        on_speech_state: Callable[[bool], None] | None = None,
        on_partial: Callable[[np.ndarray], None] | None = None,
        partial_interval_s: float = 2.0,
        aggressiveness: int = 2,
        frame_ms: int = 30,
        padding_ms: int = 300,
        silence_ms: int = 700,
        max_segment_s: float = 12.0,
        min_segment_s: float = 0.5,
        soft_segment_s: float = 4.0,
        soft_silence_ms: int = 250,
    ) -> None:
        self._on_segment = on_segment
        self._on_speech_state = on_speech_state
        self._on_partial = on_partial
        # 说话过程中每隔 partial_interval_s 把"到目前为止"的音频交出去做中间识别（边说边出字）
        self._partial_frames = int(partial_interval_s * 1000 / frame_ms) if partial_interval_s > 0 else 0
        self._since_partial = 0
        self._vad = webrtcvad.Vad(int(aggressiveness))
        self._frame_ms = frame_ms
        self._frame_samples = SAMPLE_RATE * frame_ms // 1000
        self._frame_bytes = self._frame_samples * 2
        self._buf = bytearray()

        self._start_ring: collections.deque = collections.deque(maxlen=max(1, padding_ms // frame_ms))
        self._end_ring: collections.deque = collections.deque(maxlen=max(1, silence_ms // frame_ms))
        self._max_frames = int(max_segment_s * 1000 / frame_ms)
        self._min_frames = int(min_segment_s * 1000 / frame_ms)
        # 说得超过 soft_segment_s 后，只要出现 soft_silence_ms 的短停顿就先切一段，减少等待
        self._soft_frames = int(soft_segment_s * 1000 / frame_ms)
        self._soft_silence_frames = max(1, soft_silence_ms // frame_ms)

        self._triggered = False
        self._voiced: list[bytes] = []
        self._trailing_unvoiced = 0

    # ---------- 外部接口 ----------
    def feed(self, pcm: np.ndarray) -> None:
        """喂入 16kHz int16 单声道音频，长度任意。"""
        self._buf.extend(pcm.tobytes())
        while len(self._buf) >= self._frame_bytes:
            frame = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            self._process_frame(frame)

    def flush(self) -> None:
        """强制把当前累积的片段吐出（例如停止时）。"""
        if self._triggered and self._voiced:
            self._emit()
        self._reset()

    # ---------- 内部状态机 ----------
    def _process_frame(self, frame: bytes) -> None:
        try:
            is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
        except Exception:
            is_speech = False

        if not self._triggered:
            self._start_ring.append((frame, is_speech))
            voiced = sum(1 for _, s in self._start_ring if s)
            if voiced >= 0.8 * self._start_ring.maxlen:
                self._triggered = True
                self._voiced = [f for f, _ in self._start_ring]
                self._start_ring.clear()
                self._end_ring.clear()
                self._notify(True)
        else:
            self._voiced.append(frame)
            self._end_ring.append(is_speech)
            self._trailing_unvoiced = 0 if is_speech else self._trailing_unvoiced + 1
            self._since_partial += 1
            unvoiced = sum(1 for s in self._end_ring if not s)
            if unvoiced >= 0.9 * self._end_ring.maxlen and len(self._end_ring) == self._end_ring.maxlen:
                self._emit()
                self._reset()
            elif (len(self._voiced) >= self._soft_frames
                  and self._trailing_unvoiced >= self._soft_silence_frames):
                # 已经说了一阵，趁短停顿先切一段去识别，后面的继续收
                log.debug("短停顿提前切分")
                self._emit()
                self._continue_segment()
            elif len(self._voiced) >= self._max_frames:
                # 说太长了，必须切。不要正好在上限处切（容易切在词中间），
                # 而是在最后 1.5 秒里找能量最低的一帧作为切口，切口之后的帧留给下一段。
                log.debug("片段达到上限，在低能量处切分")
                tail = self._voiced[-50:]  # 50 帧 × 30ms = 1.5s
                energies = [np.abs(np.frombuffer(f, dtype=np.int16).astype(np.float32)).mean() for f in tail]
                cut = len(self._voiced) - len(tail) + int(np.argmin(energies)) + 1
                carry = self._voiced[cut:]
                self._voiced = self._voiced[:cut]
                self._emit()
                self._continue_segment()
                self._voiced = carry
            elif (self._on_partial and self._partial_frames
                  and self._since_partial >= self._partial_frames
                  and len(self._voiced) >= self._min_frames):
                self._since_partial = 0
                try:
                    self._on_partial(self._to_audio(self._voiced))
                except Exception:
                    log.exception("处理中间识别片段时出错")

    def _continue_segment(self) -> None:
        """切出一段后，继续在同一次"说话"里收下一段。"""
        self._voiced = []
        self._end_ring.clear()
        self._trailing_unvoiced = 0
        self._since_partial = 0

    @staticmethod
    def _to_audio(frames: list[bytes]) -> np.ndarray:
        raw = b"".join(frames)
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def _emit(self) -> None:
        if len(self._voiced) < self._min_frames:
            return
        try:
            self._on_segment(self._to_audio(self._voiced))
        except Exception:
            log.exception("处理语音片段时出错")

    def _reset(self) -> None:
        was = self._triggered
        self._triggered = False
        self._voiced = []
        self._trailing_unvoiced = 0
        self._since_partial = 0
        self._start_ring.clear()
        self._end_ring.clear()
        if was:
            self._notify(False)

    def _notify(self, active: bool) -> None:
        if self._on_speech_state:
            try:
                self._on_speech_state(active)
            except Exception:
                log.exception("speech_state 回调出错")

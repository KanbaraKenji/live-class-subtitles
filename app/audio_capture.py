"""系统声音回环捕获（WASAPI loopback）。

抓取的是"电脑正在播放的声音"，也就是 Zoom / Teams 里老师的声音，
不需要麦克风，也不会录到你自己说话（除非你开了"侦听此设备"）。
输出统一为 16kHz 单声道 int16。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import numpy as np
import pyaudiowpatch as pyaudio

log = logging.getLogger(__name__)

TARGET_RATE = 16000


def list_loopback_devices() -> list[dict]:
    """列出所有可用的回环设备（每个输出设备对应一个）。"""
    p = pyaudio.PyAudio()
    try:
        return [dict(d) for d in p.get_loopback_device_info_generator()]
    finally:
        p.terminate()


def default_output_name() -> str:
    """当前系统默认输出设备的名字（用户在 Windows 里切换耳机/音箱时会变）。"""
    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        return p.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"]
    except Exception:
        return ""
    finally:
        p.terminate()


def _to_mono_16k(raw: bytes, channels: int, rate: int) -> np.ndarray:
    data = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        data = data.reshape(-1, channels).astype(np.float32).mean(axis=1)
    else:
        data = data.astype(np.float32)
    if rate != TARGET_RATE:
        n_out = int(round(len(data) * TARGET_RATE / rate))
        if n_out <= 0:
            return np.zeros(0, dtype=np.int16)
        x_old = np.linspace(0.0, 1.0, num=len(data), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        data = np.interp(x_new, x_old, data)
    return np.clip(data, -32768, 32767).astype(np.int16)


class LoopbackCapture:
    """持续捕获系统播放的声音，并通过回调交给下游。"""

    def __init__(
        self,
        on_audio: Callable[[np.ndarray], None],
        device_hint: str = "",
        chunk_ms: int = 30,
    ) -> None:
        self._on_audio = on_audio
        self._device_hint = (device_hint or "").strip().lower()
        self._chunk_ms = chunk_ms
        self._pa: pyaudio.PyAudio | None = None
        self._stream = None
        self._lock = threading.Lock()
        self.device_name = ""
        self.sample_rate = 0
        self.channels = 0

    # ---------- 设备选择 ----------
    def _pick_device(self) -> dict:
        assert self._pa is not None
        p = self._pa
        if self._device_hint:
            for d in p.get_loopback_device_info_generator():
                if self._device_hint in d["name"].lower():
                    return d
            log.warning("找不到包含“%s”的回环设备，改用系统默认输出", self._device_hint)

        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if default_out.get("isLoopbackDevice"):
            return default_out
        for d in p.get_loopback_device_info_generator():
            if default_out["name"] in d["name"]:
                return d
        raise RuntimeError("没有找到任何 WASAPI 回环设备，请确认有可用的音频输出设备")

    # ---------- 生命周期 ----------
    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            self._pa = pyaudio.PyAudio()
            dev = self._pick_device()
            self.device_name = dev["name"]
            self.sample_rate = int(dev["defaultSampleRate"])
            self.channels = max(1, int(dev["maxInputChannels"]))
            frames = int(self.sample_rate * self._chunk_ms / 1000)

            def _callback(in_data, frame_count, time_info, status):  # noqa: ARG001
                try:
                    pcm = _to_mono_16k(in_data, self.channels, self.sample_rate)
                    if len(pcm):
                        self._on_audio(pcm)
                except Exception:  # 回调里绝不能抛异常，否则流会停
                    log.exception("音频回调出错")
                return (None, pyaudio.paContinue)

            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.sample_rate,
                frames_per_buffer=frames,
                input=True,
                input_device_index=dev["index"],
                stream_callback=_callback,
            )
            self._stream.start_stream()
            log.info("开始监听：%s (%d Hz, %d ch)", self.device_name, self.sample_rate, self.channels)

    def stop(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    log.exception("关闭音频流出错")
                self._stream = None
            if self._pa is not None:
                self._pa.terminate()
                self._pa = None

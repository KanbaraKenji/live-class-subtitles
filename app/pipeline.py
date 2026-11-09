"""把各模块串成流水线：捕获 → 切句 → 识别 → 翻译 → 字幕/记录。

所有重活都在后台线程，UI 只从 event_queue 里取事件。
事件格式（dict）：
  {"type": "status", "text": "..."}                        状态栏
  {"type": "speech", "active": True/False}                 老师是否正在说话
  {"type": "partial", "id": n, "source": "泰文"}            说话中途的中间识别结果（会被后续结果覆盖）
  {"type": "transcript", "id": n, "source": "泰文"}         这一段的最终识别结果
  {"type": "translation", "id": n, "text": "中文", "partial": bool}   翻译结果
  {"type": "discard", "id": n}                             这一段作废（太短/被丢弃），UI 删掉对应的中间结果
  {"type": "fatal", "text": "..."}                         致命错误

"边说边出字"的机制：老师说话过程中，每隔 partial_interval_s 秒把到目前为止的音频做一次快速识别
（partial），显示为浅色字幕；这一段说完后再做一次正式识别（final）替换掉它。
partial 的优先级永远低于 final：只要有 final 在排队，就跳过 partial。
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .asr import WhisperASR
from .audio_capture import LoopbackCapture, _to_mono_16k, default_output_name, list_loopback_devices
from .segmenter import SpeechSegmenter
from .transcript_log import TranscriptLog
from .translator import build_translator

log = logging.getLogger(__name__)

MAX_FINAL_BACKLOG = 4      # 正式识别最多排队几段，超过就丢最旧的
MERGE_MAX_SECONDS = 10.0   # 积压时合并识别的最长音频
SILENCE_WARN_SECONDS = 30  # 多久没听到声音就在状态栏提醒
AUDIO_LEVEL_THRESHOLD = 150  # int16 平均幅度，超过算"有声音"


class Pipeline:
    def __init__(self, cfg: dict, events: queue.Queue) -> None:
        self.cfg = cfg
        self.events = events
        self._stop = threading.Event()
        self.paused = False

        # 识别工作队列只放"标记"，真正的音频放在下面两个容器里
        self._work: "queue.Queue[str | None]" = queue.Queue()
        self._finals: collections.deque = collections.deque()      # (id, audio)
        self._partial: tuple[int, np.ndarray] | None = None        # 只保留最新的一个
        self._lock = threading.Lock()

        # 翻译队列是优先队列：正式译文(0) 先于 中间译文(1)；元素 = (优先级, 序号, (id, 文本, 是否中间))
        self._tr_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._tr_seq = 0
        self._next_id = 0
        self._current_id = self._new_id()       # 正在说的这一段的编号
        self._current_has_final = False

        self.asr = WhisperASR(**cfg["asr"])
        self.translator = None
        self.capture: LoopbackCapture | None = None
        self._last_audio_ts = time.time()   # 最近一次听到声音的时间
        self._silence_warned = False
        self._model_ready = False
        self.segmenter = SpeechSegmenter(
            on_segment=self._on_segment,
            on_partial=self._on_partial,
            on_speech_state=self._on_speech_state,
            **cfg["vad"],
        )
        self.logbook = TranscriptLog(
            Path(cfg["_project_dir"]) / cfg["log"]["dir"], enabled=cfg["log"]["enabled"]
        )
        self._threads: list[threading.Thread] = []

    # ---------- 事件 ----------
    def _emit(self, type_: str, **kw) -> None:
        self.events.put({"type": type_, **kw})

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _queue_translation(self, sid: int, text: str, partial: bool) -> None:
        with self._lock:
            self._tr_seq += 1
            seq = self._tr_seq
        self._tr_queue.put((1 if partial else 0, seq, (sid, text, partial)))

    # ---------- 启动 / 停止 ----------
    def start(self, wav_file: str | None = None, realtime: bool = True) -> None:
        self._spawn(self._asr_dispatcher, "asr")
        self._spawn(self._translate_dispatcher, "translate")
        if wav_file:
            self._spawn(lambda: self._feed_wav(wav_file, realtime), "wav-feeder")
        else:
            self._spawn(self._start_capture, "capture")

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        if self.capture:
            self.capture.stop()
        self.segmenter.flush()
        self._work.put(None)
        self._tr_queue.put((99, 0, None))

    def toggle_pause(self) -> bool:
        self.paused = not self.paused
        self._emit("status", text="已暂停（右键菜单可继续）" if self.paused else "监听中")
        return self.paused

    # ---------- 设备选择（供 UI 调用） ----------
    @property
    def device_hint(self) -> str:
        return (self.cfg["audio"].get("device") or "").strip()

    @staticmethod
    def list_devices() -> list[str]:
        """可监听的输出设备名（去掉 [Loopback] 后缀）。"""
        try:
            return [d["name"].replace(" [Loopback]", "") for d in list_loopback_devices()]
        except Exception:
            log.exception("枚举音频设备失败")
            return []

    def set_device(self, hint: str) -> None:
        """切换监听设备；hint 为空 = 跟随系统默认输出。"""
        self.cfg["audio"]["device"] = hint
        if self.capture is None:
            return
        try:
            self.capture.stop()
            self.capture._device_hint = hint.lower()
            self.capture.start()
            self._last_audio_ts = time.time()
            self._silence_warned = False
            self._emit("status", text=f"监听中：{self.capture.device_name}")
        except Exception as e:
            log.exception("切换监听设备失败")
            self._emit("status", text=f"切换设备失败：{e}")

    # ---------- 音频输入 ----------
    def _on_audio(self, pcm: np.ndarray) -> None:
        if np.abs(pcm).mean() > AUDIO_LEVEL_THRESHOLD:
            self._last_audio_ts = time.time()
            if self._silence_warned:
                self._silence_warned = False
                self._emit("status", text=f"监听中：{self.capture.device_name if self.capture else ''}")
        if not self.paused:
            self.segmenter.feed(pcm)

    def _start_capture(self) -> None:
        try:
            self.capture = LoopbackCapture(
                on_audio=self._on_audio,
                device_hint=self.cfg["audio"].get("device", ""),
                chunk_ms=int(self.cfg["audio"].get("chunk_ms", 30)),
            )
            self.capture.start()
            self._last_audio_ts = time.time()
            self._emit("status", text=f"监听中：{self.capture.device_name}")
        except Exception as e:
            log.exception("启动音频捕获失败")
            self._emit("fatal", text=f"无法监听系统声音：{e}")
            return
        self._spawn(self._watchdog, "watchdog")

    def _watchdog(self) -> None:
        """两件事：1) 没指定设备时，系统默认输出变了就跟着切；2) 太久没声音就提醒。"""
        while not self._stop.is_set():
            time.sleep(5)
            if self._stop.is_set() or self.capture is None:
                break
            if not self.device_hint:  # 没指定设备才跟随系统默认（UI 里可随时改）
                try:
                    current = default_output_name()
                except Exception:
                    current = ""
                if current and current not in self.capture.device_name:
                    log.info("系统默认输出已切换为 %s，重新监听", current)
                    try:
                        self.capture.stop()
                        self.capture.start()
                        self._last_audio_ts = time.time()
                        self._silence_warned = False
                        self._emit("status", text=f"已切换监听：{self.capture.device_name}")
                    except Exception:
                        log.exception("切换监听设备失败")
            silent_for = time.time() - self._last_audio_ts
            if (self._model_ready and not self.paused and not self._silence_warned
                    and silent_for > SILENCE_WARN_SECONDS):
                self._silence_warned = True
                self._emit("status", text=f"{int(silent_for)} 秒没听到声音。正在监听：{self.capture.device_name}，请确认会议声音从这个设备播放")

    def _feed_wav(self, path: str, realtime: bool) -> None:
        """用 wav 文件模拟课堂音频（测试用）。等模型加载完再开始，免得录音在加载期间就放完了。"""
        deadline = time.time() + 300
        while not self._model_ready and not self._stop.is_set() and time.time() < deadline:
            time.sleep(0.2)
        with wave.open(path, "rb") as w:
            ch, sw, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if sw != 2:
                self._emit("fatal", text="测试 wav 必须是 16-bit PCM")
                return
            self._emit("status", text=f"回放文件：{Path(path).name}")
            chunk = int(rate * 0.03)
            while not self._stop.is_set():
                raw = w.readframes(chunk)
                if not raw:
                    break
                self._on_audio(_to_mono_16k(raw, ch, rate))
                if realtime:
                    time.sleep(0.03)
        # 文件读完，补一段静音让最后一句能被切出来
        self.segmenter.feed(np.zeros(16000, dtype=np.int16))
        self.segmenter.flush()
        self._emit("status", text="文件回放结束")

    # ---------- 切句回调（音频线程） ----------
    def _on_speech_state(self, active: bool) -> None:
        self._emit("speech", active=active)
        if not active:
            # 这一次说话结束。如果当前编号没有产生过正式片段（太短被丢），把它的中间结果撤掉
            if not self._current_has_final:
                self._emit("discard", id=self._current_id)
            self._current_id = self._new_id()
            self._current_has_final = False

    def _on_partial(self, audio: np.ndarray) -> None:
        with self._lock:
            self._partial = (self._current_id, audio)
        self._work.put("partial")

    def _on_segment(self, audio: np.ndarray) -> None:
        sid = self._current_id
        self._current_has_final = True
        self._current_id = self._new_id()
        self._current_has_final = False
        with self._lock:
            self._finals.append((sid, audio))
            dropped = []
            while len(self._finals) > MAX_FINAL_BACKLOG:
                dropped.append(self._finals.popleft()[0])
        if dropped:
            log.warning("识别跟不上，丢弃 %d 段旧音频", len(dropped))
            for d in dropped:
                self._emit("discard", id=d)
            self._emit("status", text=f"识别跟不上，已跳过 {len(dropped)} 段旧内容")
        self._work.put("final")

    # ---------- 识别线程 ----------
    def _asr_dispatcher(self) -> None:
        """加载模型（只加载一次），然后开 N 个识别线程并行处理不同片段。"""
        self._emit("status", text=f"正在加载识别模型 {self.asr.model_name}…（首次需下载）")
        try:
            self.asr.load()
        except Exception as e:
            log.exception("识别模型加载失败")
            self._emit("fatal", text=f"识别模型加载失败：{e}")
            return
        try:  # 预热：第一次推理会慢 3~4 秒，提前在静音上跑一次
            self.asr.transcribe(np.zeros(16000, dtype=np.float32), fast=True)
        except Exception:
            log.exception("模型预热失败（可忽略）")
        self._model_ready = True
        self._last_audio_ts = time.time()
        self._emit("status", text=f"模型就绪（{self.asr.device}/{self.asr.compute_type}），监听中")
        for i in range(self.asr.workers):
            self._spawn(self._asr_worker, f"asr-{i}")
        if self.asr.device == "cuda":
            self._spawn(self._vram_watch, "vram-watch")

    def _vram_watch(self) -> None:
        """模型加载完马上查一次剩余显存，之后每分钟查一次；不够时在状态栏提醒。"""
        from .gpu import VramWatch

        watch = VramWatch()
        delay = 1.0  # 先让“模型就绪”显示一下，再给提醒
        while not self._stop.wait(delay):
            try:
                msg = watch.check()
            except Exception:
                log.exception("显存检查出错")
                msg = None
            if msg:
                self._emit("status", text=msg)
            delay = 60.0

    def _asr_worker(self) -> None:
        gap = np.zeros(int(16000 * 0.3), dtype=np.float32)
        while not self._stop.is_set():
            kind = self._work.get()
            if kind is None:
                self._work.put(None)  # 让其他识别线程也能退出
                break
            if kind == "partial":
                with self._lock:
                    if self._finals:          # 有正式片段在等，中间结果不做
                        self._partial = None
                        continue
                    item, self._partial = self._partial, None
                if item is None:
                    continue
                sid, audio = item
                t0 = time.perf_counter()
                try:
                    text = self.asr.transcribe(audio, fast=True)
                except Exception:
                    log.exception("中间识别出错")
                    continue
                if text:
                    log.info("中间识别(%.1fs 音频, 耗时 %.2fs)：%s", len(audio) / 16000, time.perf_counter() - t0, text)
                    self._emit("partial", id=sid, source=text)
                    if self.cfg["translate"].get("translate_partials", True) and self._tr_queue.qsize() < 2:
                        self._queue_translation(sid, text, True)
                continue

            # kind == "final"
            with self._lock:
                if not self._finals:
                    continue
                sid, audio = self._finals.popleft()
                merged, ids, total = [audio], [sid], len(audio)
                while self._finals and total < 16000 * MERGE_MAX_SECONDS:
                    nid, nxt = self._finals.popleft()
                    merged += [gap, nxt]
                    ids.append(nid)
                    total += len(gap) + len(nxt)
            if len(ids) > 1:
                audio = np.concatenate(merged)
                log.info("合并 %d 个积压片段后识别", len(ids))
                for extra in ids[1:]:
                    self._emit("discard", id=extra)
            t0 = time.perf_counter()
            try:
                text = self.asr.transcribe(audio)
            except Exception:
                log.exception("识别出错")
                self._emit("discard", id=sid)
                continue
            dt = time.perf_counter() - t0
            if not text:
                self._emit("discard", id=sid)
                continue
            log.info("识别(%.1fs 音频, 耗时 %.2fs)：%s", len(audio) / 16000, dt, text)
            self._emit("transcript", id=sid, source=text)
            self._queue_translation(sid, text, False)

    # ---------- 翻译线程 ----------
    def _translate_dispatcher(self) -> None:
        """初始化翻译后端，然后开 N 个并发工作线程（一次慢请求不会堵住后面的句子）。"""
        try:
            self.translator = build_translator(self.cfg["translate"], language=self.cfg["asr"]["language"])
        except Exception as e:
            log.exception("翻译后端初始化失败")
            self._emit("fatal", text=f"翻译后端初始化失败：{e}")
            return
        workers = max(1, int(self.cfg["translate"].get("workers", 5)))
        for i in range(workers):
            self._spawn(self._translate_worker, f"translate-{i}")

    def _translate_worker(self) -> None:
        while not self._stop.is_set():
            _prio, _seq, item = self._tr_queue.get()
            if item is None:
                self._tr_queue.put((99, 0, None))  # 让其他工作线程也能退出
                break
            sid, text, partial = item
            t0 = time.perf_counter()
            try:
                zh = self.translator.translate(text, partial=partial)
            except Exception as e:
                log.exception("翻译出错")
                zh = f"（翻译失败：{e}）"
            log.info("%s(耗时 %.2fs)：%s", "中间翻译" if partial else "翻译", time.perf_counter() - t0, zh)
            self._emit("translation", id=sid, text=zh, partial=partial)
            if not partial:
                self.logbook.write(text, zh)

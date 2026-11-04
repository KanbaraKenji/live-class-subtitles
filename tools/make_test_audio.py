"""生成一段泰语测试音频（用微软 Edge 在线 TTS），用来在没上课时测试整个流程。

用法：
  pip install edge-tts
  python tools/make_test_audio.py            -> 生成 tests/thai_sample.wav
然后：
  python main.py --wav tests/thai_sample.wav
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SENTENCES = [
    "สวัสดีค่ะนักเรียนทุกคน วันนี้เราจะเรียนเรื่องการใช้คำว่า ถึงแม้ว่า",
    "ถึงแม้ว่าฝนจะตก แต่ฉันก็ยังไปทำงาน ใครช่วยแปลประโยคนี้หน่อยคะ",
    "ดีมากค่ะ ต่อไปเปิดหนังสือไปที่หน้าสี่สิบสอง แล้วอ่านตามครูนะคะ",
    "คำว่า ไวยากรณ์ แปลว่า grammar ภาษาอังกฤษ จำไว้นะคะ",
]

OUT_DIR = Path(__file__).resolve().parent.parent / "tests"


def _gen(text: str, mp3: Path) -> None:
    # 走 edge-tts 命令行；微软的服务偶尔会返回空音频（NoAudioReceived），所以换音色重试几次
    last = None
    for attempt, voice in enumerate(["th-TH-PremwadeeNeural", "th-TH-NiwatNeural"] * 3):
        r = subprocess.run(
            [sys.executable, "-m", "edge_tts", "-v", voice, "-t", text, "--write-media", str(mp3)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and mp3.exists() and mp3.stat().st_size > 1000:
            return
        last = r.stderr.strip().splitlines()[-1:] if r.stderr else r.returncode
        print(f"  第 {attempt + 1} 次生成失败（{voice}），重试… {last}")
    raise RuntimeError(f"TTS 多次失败：{last}")


def main() -> int:
    try:
        import edge_tts  # noqa: F401
    except ImportError:
        print("请先：pip install edge-tts")
        return 1
    OUT_DIR.mkdir(exist_ok=True)
    parts: list[Path] = []
    for i, s in enumerate(SENTENCES):
        mp3 = OUT_DIR / f"part{i}.mp3"
        _gen(s, mp3)
        parts.append(mp3)
        print("已生成", mp3.name)

    # 用 PyAV（faster-whisper 自带依赖）解码并拼接成 16k 单声道 wav，句间留 1.2 秒静音
    import av
    import numpy as np
    import wave

    pcm_chunks: list[np.ndarray] = []
    gap = np.zeros(int(16000 * 1.2), dtype=np.int16)
    for mp3 in parts:
        container = av.open(str(mp3))
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        buf: list[np.ndarray] = []
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                buf.append(rf.to_ndarray().reshape(-1).astype(np.int16))
        container.close()
        pcm_chunks.append(np.concatenate(buf))
        pcm_chunks.append(gap)
        mp3.unlink()

    out = OUT_DIR / "thai_sample.wav"
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.concatenate(pcm_chunks).tobytes())
    print("测试音频：", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())

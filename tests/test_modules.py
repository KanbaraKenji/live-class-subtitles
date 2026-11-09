"""不依赖显卡和网络的模块自检：python tests/test_modules.py"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_resample() -> None:
    from app.audio_capture import _to_mono_16k

    raw = (np.random.randint(-3000, 3000, size=4800 * 2)).astype(np.int16).tobytes()  # 48k 立体声 100ms
    out = _to_mono_16k(raw, channels=2, rate=48000)
    assert out.dtype == np.int16 and len(out) == 1600, len(out)
    print("重采样 OK:", len(out), "samples")


def test_segmenter() -> None:
    from app.segmenter import SpeechSegmenter

    got: list[np.ndarray] = []
    states: list[bool] = []
    seg = SpeechSegmenter(on_segment=got.append, on_speech_state=states.append,
                          silence_ms=600, padding_ms=300, min_segment_s=0.3)

    sr = 16000
    t = np.arange(sr * 2) / sr
    # 2 秒"人声"：带调制的多谐波信号，WebRTC VAD 会判成语音
    voice = (0.3 * np.sin(2 * np.pi * 180 * t) + 0.2 * np.sin(2 * np.pi * 360 * t)
             + 0.1 * np.sin(2 * np.pi * 720 * t)) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    voice = (voice * 32767 * 0.5).astype(np.int16)
    silence = np.zeros(sr * 1, dtype=np.int16)

    for chunk in np.array_split(np.concatenate([silence, voice, silence, voice, silence]), 200):
        seg.feed(chunk)
    seg.flush()
    assert len(got) >= 1, "没有切出任何片段"
    assert states and states[0] is True
    print("VAD 切句 OK:", [round(len(a) / sr, 2) for a in got], "秒")

    # 中间识别：连续说 5 秒，每 1 秒应触发一次 partial，且音频长度递增
    partials: list[np.ndarray] = []
    seg2 = SpeechSegmenter(on_segment=lambda a: None, on_partial=partials.append,
                           partial_interval_s=1.0, max_segment_s=20, soft_segment_s=20)
    long_voice = np.tile(voice, 3)[: sr * 5]
    for chunk in np.array_split(np.concatenate([silence, long_voice, silence]), 300):
        seg2.feed(chunk)
    assert len(partials) >= 3, f"partial 触发次数不足：{len(partials)}"
    assert all(len(partials[i]) < len(partials[i + 1]) for i in range(len(partials) - 1))
    print("中间识别回调 OK:", [round(len(a) / sr, 1) for a in partials], "秒")


def test_claude_request_shape() -> None:
    """用假客户端验证 Claude 请求参数，不真正联网。"""
    import os

    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test")
    from app import translator as tr

    calls: list[dict] = []

    class _Resp:
        stop_reason = "end_turn"
        content = [types.SimpleNamespace(type="text", text="大家好")]

    class _Beta:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                calls.append(kw)
                return _Resp()

    t = tr.ClaudeTranslator(model="claude-opus-5-5", effort="low", context_turns=2, glossary="课程名：泰语3")
    t._client = types.SimpleNamespace(beta=_Beta(), messages=_Beta.messages)
    assert t.translate("สวัสดีค่ะ") == "大家好"
    t.translate("ขอบคุณ")
    kw = calls[-1]
    assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["output_config"]["effort"] == "low" and kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["messages"][0]["content"] == "สวัสดีค่ะ" and kw["messages"][1]["role"] == "assistant"
    assert "泰语3" in kw["system"]
    print("Claude 请求参数 OK:", len(kw["messages"]), "条消息")


def test_config() -> None:
    import json
    import tempfile

    from app.config import load_config, save_env_var, save_user_settings

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        # 没有个人设置时用仓库默认值
        cfg = load_config(user_settings=d / "none.json")
        assert cfg["asr"]["language"] == "th" and cfg["ui"]["lines"] >= 1
        # 个人设置覆盖仓库默认值，且多次保存是深度合并
        us = d / "user_settings.json"
        save_user_settings({"asr": {"language": "en"}}, us)
        save_user_settings({"translate": {"model": "claude-opus-5-5"}}, us)
        cfg2 = load_config(user_settings=us)
        assert cfg2["asr"]["language"] == "en" and cfg2["translate"]["model"] == "claude-opus-5-5"
        assert cfg2["asr"]["model"] == cfg["asr"]["model"]  # 没改的项保持不变
        assert json.loads(us.read_text(encoding="utf-8")) == {"asr": {"language": "en"},
                                                              "translate": {"model": "claude-opus-5-5"}}
        # .env：替换已有的行，保留其他行
        env = d / ".env"
        env.write_text("# comment\nOTHER=1\nTEST_KEY_X=old\n", encoding="utf-8")
        save_env_var("TEST_KEY_X", "new", env)
        assert env.read_text(encoding="utf-8") == "# comment\nOTHER=1\nTEST_KEY_X=new\n"
    print("配置分层 / .env 写入 OK")


def test_languages_and_cleaning() -> None:
    from app.languages import LANGUAGES, get_language
    from app.translator import build_system_prompt, clean_translation, missing_glosses

    for code, lang in LANGUAGES.items():
        p = build_system_prompt(lang, "课程：测试")
        assert lang.name in p and lang.example in p and "课程：测试" in p, code
    assert "偶尔夹杂英语" not in build_system_prompt(get_language("en"))
    assert get_language("xx").code == "auto"

    cases = {
        "几点？几点钟（กี่โมง（几点））是吧？": "几点？几点钟（กี่โมง）是吧？",   # 双重标注
        "กิทุม": "…",                                                     # 照抄原文
        "Hello there": "…",
        "老师说：大家好。": "大家好。",                                   # 前缀
        "这段识别内容不清楚，我省略。": "…",                              # 旁白
        "然后是 ทำงาน（工作）。": "然后是 ทำงาน（工作）。",
    }
    for src, want in cases.items():
        got = clean_translation(src)
        assert got == want, (src, got)
    assert missing_glosses("泰国人用“พี่”和“นม”。") == ["พี่", "นม"]
    assert missing_glosses("ありがとう（谢谢）") == []
    assert missing_glosses("감사합니다 是谢谢") == ["감사합니다"]
    print("多语言提示词 / 译文清理 OK:", ", ".join(LANGUAGES))


def test_hotkey_parse() -> None:
    from app.hotkeys import MOD_ALT, MOD_CONTROL, MOD_SHIFT, parse_hotkey

    assert parse_hotkey("ctrl+alt+h") == (MOD_CONTROL | MOD_ALT, ord("H"))
    assert parse_hotkey("Ctrl + Shift + F9") == (MOD_CONTROL | MOD_SHIFT, 0x78)
    for bad in ("h", "ctrl+alt", "ctrl+a+b", "ctrl+wat"):
        try:
            parse_hotkey(bad)
        except ValueError:
            continue
        raise AssertionError(f"应当拒绝：{bad}")
    print("快捷键解析 OK")


def test_review_compact() -> None:
    from app.review import compact_transcript, notes_path_for

    md = ("# 课堂记录\n\n**[21:00:01]** สวัสดีค่ะ\n\n> 大家好\n\n**[21:00:05]** อืม\n\n> …\n\n"
          "**[21:00:09]** ไปทำงานไหม\n\n> （翻译失败：503）\n\n**[21:00:12]** กินข้าวไหม\n\n> 吃饭吗？\n\n")
    body, n = compact_transcript(md)
    assert n == 2 and body.splitlines() == ["[21:00:01] สวัสดีค่ะ => 大家好", "[21:00:12] กินข้าวไหม => 吃饭吗？"]
    assert notes_path_for(Path("t/2026-11-04_2103.md")).name == "2026-11-04_2103_笔记.md"
    print("笔记记录压缩 OK")


def test_vram_check() -> None:
    from app import gpu

    assert gpu.parse_vram("7362, 8192\n") == (7362 / 1024, 8.0)
    assert gpu.parse_vram("garbage") is None
    csv_text = '"System","4","Services","0","5,624 K"\n"LM Studio.exe","36000","Console","1","200 K"\n' \
               '"LM Studio.exe","37676","Console","1","100 K"\n"ollama.exe","1","Console","1","1 K"\n'
    assert gpu.parse_gpu_apps(csv_text) == ["LM Studio", "Ollama"]
    # 上节课的真实情况：已用 7362 / 8192 MiB → 只剩 0.8GB，应该提醒并点名 LM Studio
    msg = gpu.vram_warning(7362 / 1024, 8.0, ["LM Studio"])
    assert msg and "0.8GB" in msg and "LM Studio" in msg and "占用" in msg
    assert gpu.vram_warning(5.0, 8.0, []) is None

    # 同一次显存紧张只提醒一次，恢复后才会再提醒
    seq = iter([(7.5, 8.0), (7.6, 8.0), (6.0, 8.0), (7.5, 8.0)])
    gpu.query_vram, gpu.running_gpu_apps = (lambda: next(seq)), (lambda: [])
    w = gpu.VramWatch()
    got = [w.check() is not None for _ in range(4)]
    assert got == [True, False, False, True], got
    print("显存检查 OK")


if __name__ == "__main__":
    test_vram_check()
    test_config()
    test_resample()
    test_segmenter()
    test_claude_request_shape()
    test_languages_and_cleaning()
    test_hotkey_parse()
    test_review_compact()
    print("全部通过")

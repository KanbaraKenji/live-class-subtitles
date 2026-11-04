"""课后笔记：把一节课的字幕记录交给 Claude，整理成复习笔记（概要 / 词汇表 / 句型 / 作业）。

用法：
  python main.py --review                      对最新一节课生成笔记
  python main.py --review transcripts/xxx.md   对指定记录生成笔记
也可以在字幕窗右键菜单「生成本节课笔记」。
笔记保存在记录文件旁边：xxx_笔记.md
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

from .languages import get_language

log = logging.getLogger(__name__)

_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")

_PROMPT = """下面是一节在线{src}课的自动字幕记录。每条是课堂上某人说的话（语音识别的{src}原文）和机器翻译的中文。
识别和翻译都可能有错；乱码、语气词和译文为“…”的条目可以忽略。

请为学生整理一份课后复习笔记，用 Markdown 输出，包含以下几节：

## 本节课概要
3~5 句话说明这节课讲了什么、做了哪些练习。

## 重点词汇
表格：| {src} | 中文 | 课堂上的用法或例句 |
只收老师讲解、领读或反复出现的词，按课堂出现顺序，最多 40 个。原词按正确拼写写出（修正识别错误）。

## 重点句型与语法
每个句型写出结构、中文解释，以及 1~2 个课堂上出现过的例句（原文 + 中文）。

## 课堂任务与通知
作业、下次课安排、老师提醒的事项；没有就写“无”。

## 复习建议
最多 3 条，针对这节课最容易混淆的地方。

要求：只依据记录内容整理，不要补充记录里没有的知识点；拿不准的词在后面标注“（存疑）”。
直接输出笔记正文，不要开场白。

课堂记录：
{transcript}"""

_ENTRY = re.compile(r"^\*\*\[(?P<t>[\d:]+)\]\*\* (?P<src>.*?)\n\n> (?P<zh>.*?)$", re.M)


def latest_transcript(directory: str | Path) -> Path | None:
    files = [p for p in Path(directory).glob("*.md") if not p.stem.endswith("_笔记")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def compact_transcript(text: str) -> tuple[str, int]:
    """把记录压成“时间 原文 => 译文”一行一条，去掉译文为“…”的条目以节省 token。"""
    lines = []
    for m in _ENTRY.finditer(text):
        zh = m["zh"].strip()
        if zh in ("…", "") or zh.startswith("（翻译"):
            continue
        lines.append(f"[{m['t']}] {m['src'].strip()} => {zh}")
    return "\n".join(lines), len(lines)


def notes_path_for(transcript: Path) -> Path:
    return transcript.with_name(f"{transcript.stem}_笔记.md")


def generate_notes(
    transcript: str | Path,
    cfg: dict,
    on_progress: Callable[[str], None] | None = None,
) -> Path:
    """生成笔记并写入文件，返回笔记路径。出错时抛 RuntimeError（信息可直接给用户看）。"""
    import anthropic

    progress = on_progress or (lambda s: None)
    transcript = Path(transcript)
    body, n = compact_transcript(transcript.read_text(encoding="utf-8"))
    if n < 5:
        raise RuntimeError(f"这节课的有效记录只有 {n} 条，太少了，无法生成笔记")

    lang = get_language(cfg["asr"].get("language"))
    model = cfg.get("review", {}).get("model", "claude-opus-5-5")
    effort = cfg.get("review", {}).get("effort", "medium")
    prompt = _PROMPT.format(src=lang.name, transcript=body)
    progress(f"正在用 {model} 整理 {n} 条记录…")

    client = anthropic.Anthropic(timeout=600.0)
    kwargs = dict(
        model=model,
        max_tokens=16000,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
    )
    try:
        if model.startswith(_FALLBACK_MODELS):
            stream_ctx = client.beta.messages.stream(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
            )
        else:
            stream_ctx = client.messages.stream(**kwargs)
        with stream_ctx as stream:
            resp = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise RuntimeError("API Key 无效，请在「设置」里检查") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("网络连接失败") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"Claude API 错误 {e.status_code}") from e

    if resp.stop_reason == "refusal":
        raise RuntimeError("模型拒绝了这次请求")
    notes = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not notes:
        raise RuntimeError("模型没有返回内容")
    if resp.stop_reason == "max_tokens":
        notes += "\n\n> （笔记过长被截断）"

    out = notes_path_for(transcript)
    header = f"# 课后笔记 · {transcript.stem}\n\n> 由 {model} 根据 {n} 条课堂字幕自动整理，可能有错，请以课堂内容为准。\n\n"
    out.write_text(header + notes + "\n", encoding="utf-8")
    u = resp.usage
    log.info("笔记已生成：%s（输入 %s tokens，输出 %s tokens）", out, u.input_tokens, u.output_tokens)
    progress(f"笔记已生成：{out.name}")
    return out

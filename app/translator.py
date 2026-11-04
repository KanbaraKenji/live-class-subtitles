"""翻译后端：Claude（推荐）/ 免费（Google → MyMemory）/ 不翻译。"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import threading
import time

from .languages import Language, get_language

log = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """你是一台字幕机，不是助教。学生的母语是中文，正在上在线{src}课，老师主要讲{src}{mixed}。
你会逐段收到老师说话的语音识别文本，输出对应的简体中文字幕。

硬性规则：
- 输出只能是译文本身，一行。禁止任何解释、注释、旁白、建议、揣测说明（例如“识别不清”“可能是……”“视上下文而定”“老师在强调”“请跟读”“建议以原声为准”）。禁止给出多个候选译法。
- 不加前缀（“老师说：”“译文：”），不用引号包住整句。
- 识别文本常有错字、重复、断句不完整：只译你能确定的部分，不确定的部分直接省略，不要说明你省略了什么。
- 老师在讲解、领读某个具体的{src}词/句型{or_en}时，保留原词并紧跟括号中文释义，格式为“原词（中文）”，例如“{example}”{en_example}。
- 译文里出现的任何外文词，后面都必须紧跟括号中文释义，绝不能让外文词裸露在译文里。
- 平常的整句话直接译成中文即可，不要为了加注而把普通词保留成原文。
- 格式只有一种：“原词（中文）”。不要写成“中文（原词（中文））”这种双重标注，也不要在已经译成中文的词后面再补原文。
- 老师重复同一个词好几遍，就按原样译一遍即可。
- 输入是噪音、乱码、无意义音节或纯语气词时，只输出“…”。{en_rule}
- 译文长度不要超过原文大意所需，宁短勿长。
Latency-sensitive; begin your visible answer immediately."""


def build_system_prompt(lang: Language, glossary: str = "") -> str:
    is_en = lang.code == "en"
    prompt = _PROMPT_TEMPLATE.format(
        src=lang.name,
        mixed="" if is_en else "，偶尔夹杂英语",
        or_en="" if is_en else "或英语单词",
        example=lang.example,
        en_example="" if is_en else "、“grammar（语法）”",
        en_rule="" if is_en else "\n- 输入是英语时同样译成中文（整句英语直接译；单个教学用的英语词按上面的括号形式）。",
    )
    if glossary.strip():
        prompt += "\n\n补充术语/背景：\n" + glossary.strip()
    return prompt

# 模型偶尔还是会夹带旁白，这里做最后一道过滤：含这些词的分句整句删掉
_META_MARKERS = (
    "识别不清", "识别内容", "识别不太", "识别结果", "无法确定", "建议以", "视上下文", "老师在", "老师说", "老师的意思",
    "请跟读", "请注意", "可能是", "或其他", "识别错误", "此处", "不确定", "这里应该", "疑似", "注：", "备注",
    "（译注", "译注", "我省略", "省略", "大概是", "像是", "之类", "这句话里", "这段", "含义不明", "听不清",
    "识别不完整", "不完整）", "（识别",
)
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;…])")
# “中文（原文（中文））”这种双重标注 → “中文（原文）”
_NESTED_GLOSS = re.compile(r"（([^（）]+?)（[^（）]*））")
_CJK = re.compile(r"[一-鿿]")
_LETTER = re.compile(r"[^\W\d_]")

# 外文词：泰文（可含空格）、韩文、日文假名、拉丁字母（含越南语声调字母）
_FOREIGN_WORD = re.compile(
    r"[฀-๿][฀-๿\s]*[฀-๿]|[฀-๿]"
    r"|[가-힯]+|[぀-ヿ]+"
    r"|[A-Za-zÀ-ɏḀ-ỿ][A-Za-zÀ-ɏḀ-ỿ'\-]+"
)
_SKIP_WORDS = {"ok", "okay", "no", "bts", "mrt", "ot", "kfc", "lol", "ppt", "pdf", "app", "wifi", "line", "zoom"}


def missing_glosses(text: str) -> list[str]:
    """找出译文里没有紧跟“（中文）”释义的外文词。"""
    missing: list[str] = []
    glossed: set[str] = set()
    for m in _FOREIGN_WORD.finditer(text):
        word = m.group(0).strip()
        if not word or word.lower() in _SKIP_WORDS or len(word) < 2:
            continue
        before = text[: m.start()]
        # 已经在括号里（例如“……（泰语里叫 พี่）”）就不要求再注
        if before.count("（") + before.count("(") > before.count("）") + before.count(")"):
            continue
        after = text[m.end():].lstrip(" ”\"'’」』")
        if after.startswith(("（", "(")):
            glossed.add(word.lower())
            continue
        if word.lower() in glossed or word.lower() in {w.lower() for w in missing}:
            continue  # 同一个词前面已经注过一次，后面再出现不用重复注
        missing.append(word)
    return missing


def clean_translation(text: str) -> str:
    text = text.strip()
    for prefix in ("老师说：", "老师说:", "译文：", "译文:", "翻译：", "翻译:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text[:1] in "“\"「" and text[-1:] in "”\"」":
        text = text[1:-1].strip()
    parts =[p for p in _SENT_SPLIT.split(text) if p.strip()]
    kept = [p for p in parts if not any(m in p for m in _META_MARKERS)]
    if parts and not kept:
        return "…"
    out = "".join(kept).strip() if kept else text
    out = _NESTED_GLOSS.sub(r"（\1）", out)
    # 模型没译出来、把原文照抄回来（通常是识别出的乱码）：一个汉字都没有的字幕没有意义
    if _LETTER.search(out) and not _CJK.search(out):
        return "…"
    return out or "…"

# 支持服务端 fallbacks 参数的模型（安全分类器误拒时自动换模型重试）
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")

# 结构化输出：强制模型只能返回 {"translation": "..."}，推理过程进不了正文
_OUTPUT_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"translation": {"type": "string"}},
        "required": ["translation"],
        "additionalProperties": False,
    },
}


def _extract_translation(resp) -> str:
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("translation"), str):
            return data["translation"]
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


class ClaudeTranslator:
    name = "Claude"

    def __init__(self, model: str, effort: str = "low", context_turns: int = 6, glossary: str = "",
                 fix_glosses: bool = True, language: str = "th") -> None:
        import anthropic

        self.fix_glosses = fix_glosses  # 译文里有裸露外文词时是否再请求一次补括号释义

        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            # 没有 key 时 SDK 也可能从 `ant auth login` 的配置读取，所以这里只提醒不报错
            log.warning("未检测到 ANTHROPIC_API_KEY，若未用 ant auth login 登录，翻译会失败")
        self._anthropic = anthropic
        # 字幕场景宁可失败也不能等太久：20 秒超时，不自动重试
        self._client = anthropic.Anthropic(timeout=20.0, max_retries=0)
        self.model = model
        self.effort = effort
        self._history: collections.deque = collections.deque(maxlen=max(0, context_turns))
        self._lock = threading.Lock()  # 多个翻译线程共享上下文
        self._system = build_system_prompt(get_language(language), glossary)

    def _messages(self, text: str) -> list[dict]:
        msgs: list[dict] = []
        with self._lock:
            history = list(self._history)
        for src, dst in history:
            msgs.append({"role": "user", "content": src})
            msgs.append({"role": "assistant", "content": dst})
        msgs.append({"role": "user", "content": text})
        return msgs

    def translate(self, text: str, partial: bool = False) -> str:
        """partial=True 表示这是还没说完的中间结果：照常翻译，但不计入上下文。"""
        kwargs = dict(
            model=self.model,
            max_tokens=400,
            system=self._system,
            output_config={"effort": self.effort, "format": _OUTPUT_FORMAT},
            messages=self._messages(text),
        )
        try:
            resp = self._send(kwargs)
        except self._anthropic.AuthenticationError:
            return "（翻译失败：API Key 无效，请检查 .env）"
        except self._anthropic.RateLimitError:
            return "（翻译失败：请求过于频繁）"
        except self._anthropic.APITimeoutError:
            return "（翻译超时）"
        except self._anthropic.APIConnectionError:
            return "（翻译失败：网络连接错误）"
        except self._anthropic.APIStatusError as e:
            log.error("Claude API 错误：%s", e)
            return f"（翻译失败：{e.status_code}）"

        if resp.stop_reason == "refusal":
            return "（该句被模型拒绝翻译）"
        out = clean_translation(_extract_translation(resp))

        # 正式译文里若有裸露的泰文/英文词，让模型补一次括号释义（只补一次，避免拖慢）
        if self.fix_glosses and not partial and out and out != "…":
            missing = missing_glosses(out)
            if missing:
                log.info("译文缺少释义：%s，补一次", missing)
                fixed = self._add_glosses(kwargs["messages"], out, missing)
                if fixed:
                    out = fixed

        if out and out != "…" and not partial:
            with self._lock:
                self._history.append((text, out))
        return out or "…"

    def _send(self, kwargs: dict):
        """发请求；服务端临时过载（503/529）或 5xx 时，等 0.5 秒立刻重试一次。"""
        for attempt in range(2):
            try:
                if self.model.startswith(_FALLBACK_MODELS):
                    return self._client.beta.messages.create(
                        betas=["server-side-fallback-2026-07-01"],
                        fallbacks="default",
                        **kwargs,
                    )
                return self._client.messages.create(**kwargs)
            except self._anthropic.APIStatusError as e:
                if attempt == 0 and e.status_code >= 500:
                    log.warning("Claude API %s，0.5 秒后重试一次", e.status_code)
                    time.sleep(0.5)
                    continue
                raise
        raise RuntimeError("unreachable")

    def _add_glosses(self, messages: list[dict], draft: str, missing: list[str]) -> str | None:
        words = "、".join(missing[:8])
        follow_up = messages + [
            {"role": "assistant", "content": draft},
            {"role": "user", "content": f"上面译文里的 {words} 没有中文释义。请把每个外文词改成“原词（中文）”的形式，其余不变，只输出修正后的译文。"},
        ]
        try:
            resp = self._client.messages.create(
                model=self.model, max_tokens=400, system=self._system,
                output_config={"effort": self.effort, "format": _OUTPUT_FORMAT}, messages=follow_up,
            )
        except Exception as e:
            log.warning("补释义失败：%s", e)
            return None
        if resp.stop_reason == "refusal":
            return None
        fixed = clean_translation(_extract_translation(resp))
        return fixed if fixed and fixed != "…" else None


class FreeTranslator:
    """免费备用方案（无需 Key）：先试 Google，被限流/封锁就自动换 MyMemory。质量一般。"""

    name = "免费"

    def __init__(self, language: str = "th") -> None:
        from deep_translator import GoogleTranslator, MyMemoryTranslator

        lang = get_language(language)
        self._engines = [("Google", GoogleTranslator(source="auto", target="zh-CN"))]
        if lang.mymemory:  # MyMemory 必须指定源语言，自动识别模式下不用它
            self._engines.append(("MyMemory", MyMemoryTranslator(source=lang.mymemory, target="zh-CN")))

    def translate(self, text: str, partial: bool = False) -> str:  # noqa: ARG002
        for name, engine in list(self._engines):
            try:
                out = engine.translate(text)
                if out:
                    return out
            except Exception as e:
                log.warning("%s 翻译失败，换下一个引擎：%s", name, str(e)[:120])
                # 把失败的引擎挪到最后，下次先用能用的
                self._engines.remove((name, engine))
                self._engines.append((name, engine))
        return "（翻译失败）"


class NoTranslator:
    name = "无"

    def translate(self, text: str, partial: bool = False) -> str:  # noqa: ARG002
        return ""


def build_translator(cfg: dict, language: str = "th"):
    backend = (cfg.get("backend") or "claude").lower()
    if backend == "claude":
        return ClaudeTranslator(
            model=cfg.get("model", "claude-haiku-5-5"),
            effort=cfg.get("effort", "low"),
            context_turns=int(cfg.get("context_turns", 6)),
            glossary=cfg.get("glossary", "") or "",
            fix_glosses=bool(cfg.get("fix_glosses", False)),
            language=language,
        )
    if backend in ("google", "free", "mymemory"):
        return FreeTranslator(language)
    return NoTranslator()

"""支持的课堂语言（老师讲的语言）。字幕目标语言固定为简体中文。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Language:
    code: str                 # Whisper 语言代码；"auto" 表示每段自动检测
    name: str                 # 中文名，用于提示词和界面
    short: str                # 顶栏按钮上的一个字
    fonts: list[str] = field(default_factory=list)   # 显示原文用的字体，按优先级
    mymemory: str | None = None                       # 免费翻译 MyMemory 的源语言代码
    example: str = ""         # 提示词里“原词（中文）”的示例


LANGUAGES: dict[str, Language] = {
    "th": Language("th", "泰语", "泰", ["Leelawadee UI", "Leelawadee", "Tahoma"], "th-TH", "ประโยค（句子）"),
    "en": Language("en", "英语", "英", ["Segoe UI"], "en-GB", "grammar（语法）"),
    "ja": Language("ja", "日语", "日", ["Yu Gothic UI", "Meiryo UI", "MS Gothic"], "ja-JP", "ありがとう（谢谢）"),
    "ko": Language("ko", "韩语", "韩", ["Malgun Gothic"], "ko-KR", "감사합니다（谢谢）"),
    "vi": Language("vi", "越南语", "越", ["Segoe UI"], "vi-VN", "cảm ơn（谢谢）"),
    "auto": Language("auto", "外语", "原", ["Segoe UI", "Leelawadee UI", "Malgun Gothic", "Yu Gothic UI"], None,
                     "grammar（语法）"),
}


def get_language(code: str | None) -> Language:
    return LANGUAGES.get((code or "auto").lower(), LANGUAGES["auto"])

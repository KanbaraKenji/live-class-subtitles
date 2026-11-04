"""设置窗口：常用选项不用改 YAML。

- 保存到 user_settings.json（个人设置，不进 git）；API Key 保存到 .env
- 第一次运行且没有 API Key 时，main.py 会先弹出这个窗口
- 从字幕窗打开时，保存后可以一键重启生效
"""

from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .config import has_api_key, save_env_var, save_user_settings
from .languages import LANGUAGES

log = logging.getLogger(__name__)

BACKENDS = [("claude", "Claude（推荐，质量最好，需要 API Key）"), ("free", "免费翻译（Google → MyMemory，质量一般）"),
            ("none", "不翻译，只显示原文")]
CLAUDE_MODELS = [("claude-haiku-5-5", "Haiku 5.5 — 最快（每句约 1 秒），推荐"),
                 ("claude-sonnet-5-5", "Sonnet 5.5 — 更准，稍慢"),
                 ("claude-opus-5-5", "Opus 5.5 — 最准，每句 2~3 秒")]
ASR_MODELS = [("large-v3", "large-v3 — 最准，需约 4GB 显存"), ("large-v3-turbo", "large-v3-turbo — 更快，准确度略低"),
              ("medium", "medium — 显存约 2GB"), ("small", "small — 显存约 1GB，没独显也能跑")]
ASR_DEVICES = [("auto", "自动（有 NVIDIA 显卡就用显卡）"), ("cuda", "显卡（CUDA）"), ("cpu", "CPU（很慢）")]


def _labels(pairs):
    return [label for _, label in pairs]


def _value_of(pairs, label, default):
    for value, lab in pairs:
        if lab == label:
            return value
    return default


def _label_of(pairs, value):
    for v, lab in pairs:
        if v == value:
            return lab
    return pairs[0][1]


class SettingsDialog:
    """master=None 时自己创建主窗口（首次运行），否则作为字幕窗的子窗口。"""

    def __init__(
        self,
        cfg: dict,
        master: tk.Misc | None = None,
        on_saved: Callable[[bool], None] | None = None,
        first_run: bool = False,
    ) -> None:
        self.cfg = cfg
        self.on_saved = on_saved          # 参数：是否需要重启
        self.first_run = first_run
        self.saved = False
        self._own_root = master is None
        self.win = tk.Tk() if master is None else tk.Toplevel(master)
        self.win.title("课堂字幕 · 设置" if not first_run else "课堂字幕 · 首次设置")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        self._build()
        self.win.protocol("WM_DELETE_WINDOW", self._cancel)
        self.win.update_idletasks()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
        self.win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    # ---------- 界面 ----------
    def _build(self) -> None:
        pad = {"padx": 12, "pady": 5}
        f = ttk.Frame(self.win, padding=14)
        f.grid(sticky="nsew")
        row = 0

        if self.first_run:
            ttk.Label(f, text="欢迎！先选好课堂语言和翻译方式，之后可以随时在字幕窗右键「设置…」里修改。",
                      wraplength=520).grid(row=row, column=0, columnspan=2, sticky="w", **pad)
            row += 1

        a, t = self.cfg["asr"], self.cfg["translate"]

        ttk.Label(f, text="老师讲的语言").grid(row=row, column=0, sticky="w", **pad)
        self.lang_pairs = [(code, f"{lang.name}（{code}）") for code, lang in LANGUAGES.items()]
        self.lang = ttk.Combobox(f, values=_labels(self.lang_pairs), state="readonly", width=44)
        self.lang.set(_label_of(self.lang_pairs, a.get("language", "th")))
        self.lang.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(f, text="翻译方式").grid(row=row, column=0, sticky="nw", **pad)
        self.backend = tk.StringVar(value=t.get("backend", "claude"))
        bf = ttk.Frame(f)
        bf.grid(row=row, column=1, sticky="w", **pad)
        for value, label in BACKENDS:
            ttk.Radiobutton(bf, text=label, value=value, variable=self.backend,
                            command=self._on_backend).pack(anchor="w")
        row += 1

        ttk.Label(f, text="Claude API Key").grid(row=row, column=0, sticky="w", **pad)
        kf = ttk.Frame(f)
        kf.grid(row=row, column=1, sticky="w", **pad)
        self.key = ttk.Entry(kf, width=36, show="•")
        self.key.pack(side="left")
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(kf, text="显示", variable=self.show_key,
                        command=lambda: self.key.configure(show="" if self.show_key.get() else "•")).pack(side="left", padx=6)
        row += 1
        hint = "已设置。留空表示不修改。" if has_api_key() else "在 console.anthropic.com 创建。只保存在本机 .env 文件里。"
        self.key_hint = ttk.Label(f, text=hint, foreground="#6B7280")
        self.key_hint.grid(row=row, column=1, sticky="w", padx=12)
        row += 1

        ttk.Label(f, text="Claude 模型").grid(row=row, column=0, sticky="w", **pad)
        self.model = ttk.Combobox(f, values=_labels(CLAUDE_MODELS), state="readonly", width=44)
        self.model.set(_label_of(CLAUDE_MODELS, t.get("model", "claude-haiku-5-5")))
        self.model.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(f, text="语音识别模型").grid(row=row, column=0, sticky="w", **pad)
        self.asr_model = ttk.Combobox(f, values=_labels(ASR_MODELS), state="readonly", width=44)
        self.asr_model.set(_label_of(ASR_MODELS, a.get("model", "large-v3")))
        self.asr_model.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(f, text="识别用的硬件").grid(row=row, column=0, sticky="w", **pad)
        self.asr_device = ttk.Combobox(f, values=_labels(ASR_DEVICES), state="readonly", width=44)
        self.asr_device.set(_label_of(ASR_DEVICES, a.get("device", "auto")))
        self.asr_device.grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.partials = tk.BooleanVar(value=float(self.cfg["vad"].get("partial_interval_s", 2)) > 0)
        ttk.Checkbutton(f, text="边说边出字（说话过程中先显示浅色的临时字幕）",
                        variable=self.partials).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        ttk.Label(f, text="课程背景 / 术语").grid(row=row, column=0, sticky="nw", **pad)
        self.glossary = tk.Text(f, width=50, height=4, wrap="word", font=("Microsoft YaHei UI", 9))
        self.glossary.insert("1.0", t.get("glossary", "") or "")
        self.glossary.grid(row=row, column=1, sticky="w", **pad)
        row += 1
        ttk.Label(f, text="例如：课程名、老师和同学的名字、教材里的专有名词。会帮助翻译更准确。",
                  foreground="#6B7280").grid(row=row, column=1, sticky="w", padx=12)
        row += 1

        self.error = ttk.Label(f, text="", foreground="#DC2626")
        self.error.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1

        bf2 = ttk.Frame(f)
        bf2.grid(row=row, column=0, columnspan=2, sticky="e", pady=(6, 0))
        if self.first_run:
            ttk.Button(bf2, text="开始使用", command=lambda: self._save(restart=False)).pack(side="right", padx=4)
            ttk.Button(bf2, text="退出", command=self._cancel).pack(side="right", padx=4)
        else:
            ttk.Button(bf2, text="保存并重启", command=lambda: self._save(restart=True)).pack(side="right", padx=4)
            ttk.Button(bf2, text="仅保存", command=lambda: self._save(restart=False)).pack(side="right", padx=4)
            ttk.Button(bf2, text="取消", command=self._cancel).pack(side="right", padx=4)
        self._on_backend()

    def _on_backend(self) -> None:
        state = "readonly" if self.backend.get() == "claude" else "disabled"
        self.model.configure(state=state)
        self.key.configure(state="normal" if self.backend.get() == "claude" else "disabled")

    # ---------- 保存 ----------
    def collect(self) -> tuple[dict, str]:
        """返回 (要写入 user_settings.json 的补丁, 新输入的 API Key)。"""
        patch = {
            "asr": {
                "language": _value_of(self.lang_pairs, self.lang.get(), "th"),
                "model": _value_of(ASR_MODELS, self.asr_model.get(), "large-v3"),
                "device": _value_of(ASR_DEVICES, self.asr_device.get(), "auto"),
            },
            "translate": {
                "backend": self.backend.get(),
                "model": _value_of(CLAUDE_MODELS, self.model.get(), "claude-haiku-5-5"),
                "glossary": self.glossary.get("1.0", "end").strip(),
                "translate_partials": bool(self.partials.get()),
            },
            "vad": {"partial_interval_s": 2.0 if self.partials.get() else 0.0},
        }
        return patch, self.key.get().strip()

    def _save(self, restart: bool) -> None:
        patch, key = self.collect()
        if patch["translate"]["backend"] == "claude" and not key and not has_api_key():
            self.error.configure(text="选了 Claude 翻译就需要填 API Key；没有的话可以先选「免费翻译」。")
            return
        if key and not key.startswith("sk-ant-"):
            self.error.configure(text="这看起来不像 Claude API Key（应该以 sk-ant- 开头）。")
            return
        try:
            save_user_settings(patch)
            if key:
                save_env_var("ANTHROPIC_API_KEY", key)
        except Exception as e:
            log.exception("保存设置失败")
            self.error.configure(text=f"保存失败：{e}")
            return
        # 让当前进程的配置也同步（首次运行时直接用新配置启动）
        for section, values in patch.items():
            self.cfg.setdefault(section, {}).update(values)
        self.saved = True
        log.info("设置已保存（API Key %s）", "已更新" if key else "未修改")
        self.win.destroy()
        if self.on_saved:
            self.on_saved(restart)

    def _cancel(self) -> None:
        self.win.destroy()

    def run(self) -> bool:
        """独立运行（首次设置），返回是否保存了。"""
        if self._own_root:
            self.win.mainloop()
        return self.saved

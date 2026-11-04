"""配置加载。

优先级（后者覆盖前者）：
  1. 本文件里的 DEFAULTS
  2. config.yaml          —— 随仓库分享的通用配置，带注释
  3. user_settings.json   —— 个人设置（设置窗口保存到这里，不进 git）
API Key 放在 .env（不进 git）。
"""

from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
USER_SETTINGS = PROJECT_DIR / "user_settings.json"
ENV_FILE = PROJECT_DIR / ".env"

# 默认配置：数值来自几节真实课堂的调优结果（见 docs/lessons-learned.md）
DEFAULTS: dict[str, Any] = {
    "audio": {
        "device": "",            # 设备名关键字；留空 = 跟随系统默认输出设备
        "chunk_ms": 30,
    },
    "vad": {
        "aggressiveness": 2,     # 0~3，越大越容易把声音判成"非语音"
        "padding_ms": 300,       # 判定"开始说话"所需的窗口
        "silence_ms": 600,       # 连续静音多久算一句话结束
        "max_segment_s": 6.0,    # 一段最长多少秒，超过就在低能量处切开
        "min_segment_s": 1.0,    # 太短的片段（语气词）直接丢弃
        "soft_segment_s": 4.0,   # 超过这个长度后，遇到短停顿就提前切
        "soft_silence_ms": 250,  # "短停顿"的长度
        "partial_interval_s": 2.0,  # 边说边出字：每隔几秒做一次中间识别，0 = 关闭
    },
    "asr": {
        "model": "large-v3",     # large-v3 / large-v3-turbo / medium / small
        "device": "auto",        # auto / cuda / cpu
        "compute_type": "auto",  # auto / float16 / int8_float16 / int8
        "language": "th",        # th / en / ja / ko / vi / auto
        "beam_size": 3,
        "initial_prompt": "",
        "workers": 2,            # 并行识别线程数
    },
    "translate": {
        "backend": "claude",     # claude / free / none
        "model": "claude-haiku-5-5",
        "effort": "low",
        "context_turns": 6,
        "workers": 5,
        "translate_partials": True,
        "fix_glosses": False,
        "glossary": "",
    },
    "review": {
        "model": "claude-opus-5-5",  # 课后笔记不追求速度，用质量最高的模型
        "effort": "medium",
    },
    "hotkeys": {
        "enabled": True,
        "toggle_visible": "ctrl+alt+h",
        "toggle_pause": "ctrl+alt+p",
        "toggle_lock": "ctrl+alt+l",
    },
    "ui": {
        "lines": 3,
        "show_source": True,
        "font_zh": 24,
        "font_src": 14,
        "width_ratio": 0.6,
        "opacity": 0.9,
        "font_family": "Microsoft YaHei UI",
    },
    "log": {
        "enabled": True,
        "dir": "transcripts",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------- .env ----------
def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 读取：只在环境变量没设置时填入。"""
    path = path or ENV_FILE
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def save_env_var(key: str, value: str, path: Path | None = None) -> None:
    """在 .env 里新增或替换一行 KEY=value，其余行保持不变。"""
    path = path or ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out, done = [], False
    for line in lines:
        if line.strip().startswith(f"{key}="):
            if not done:
                out.append(f"{key}={value}")
                done = True
            continue
        out.append(line)
    if not done:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.environ[key] = value


def has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


# ---------- 个人设置 ----------
def load_user_settings(path: Path | None = None) -> dict:
    path = path or USER_SETTINGS
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        log.exception("user_settings.json 读取失败，忽略")
        return {}


def save_user_settings(patch: dict, path: Path | None = None) -> None:
    """把 patch 深度合并进 user_settings.json。"""
    path = path or USER_SETTINGS
    merged = _deep_merge(load_user_settings(path), patch)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config(path: str | Path | None = None, user_settings: Path | None = None) -> dict[str, Any]:
    load_dotenv()
    path = Path(path) if path else PROJECT_DIR / "config.yaml"
    file_cfg: dict = {}
    if path.exists():
        file_cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = _deep_merge(DEFAULTS, file_cfg)
    cfg = _deep_merge(cfg, load_user_settings(user_settings))
    # 兼容旧版本的键名
    if "font_th" in cfg["ui"]:
        cfg["ui"].setdefault("font_src", cfg["ui"].pop("font_th"))
    cfg["_project_dir"] = str(PROJECT_DIR)
    return cfg

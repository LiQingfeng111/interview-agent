"""Prompt 版本管理：把 prompt 从代码里搬出来，才谈得上 A/B。

为什么必须这么做：
  prompt 是这个项目最核心的资产，但它原来硬编码在 config.py 里。
  想比较"改一句约束到底有没有用"，只能改代码、跑一遍、再改回去——
  改动不可归因，等于蒙眼开车。

设计取舍：
  - 文件按 `prompts/<name>_<version>.txt` 存放，纯文本，能 diff。
  - 加载失败就回退到 config 里的默认值，保证任何情况下跑得起来。
  - 版本列表由文件名推导，不维护额外清单（少一处会不同步的地方）。
"""
from __future__ import annotations

import os

from config import INTERVIEWER_SYSTEM

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _ensure_dir() -> None:
    os.makedirs(PROMPTS_DIR, exist_ok=True)


def list_versions(name: str = "interviewer") -> list[str]:
    """列出某个 prompt 的所有版本，例如 ['v1', 'v2']。"""
    _ensure_dir()
    out = []
    for fn in sorted(os.listdir(PROMPTS_DIR)):
        if fn.startswith(name + "_") and fn.endswith(".txt"):
            out.append(fn[len(name) + 1: -4])
    return out


def load_prompt(name: str = "interviewer", version: str = "default") -> str:
    """读取指定版本的 prompt；读不到就用 config 默认值。"""
    if version in ("default", "", None):
        return INTERVIEWER_SYSTEM
    path = os.path.join(PROMPTS_DIR, f"{name}_{version}.txt")
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        return text or INTERVIEWER_SYSTEM
    except OSError:
        return INTERVIEWER_SYSTEM


def save_prompt(name: str, version: str, text: str) -> str:
    _ensure_dir()
    path = os.path.join(PROMPTS_DIR, f"{name}_{version}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return path

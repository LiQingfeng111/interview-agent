#!/usr/bin/env python3
"""抓评分时实际发给模型的 prompt，确认历史到底有没有进去。

## 为什么要抓原始 prompt

verify_history_aware.py 显示三组场景分数完全一样。
两种可能：
  A) 历史没被拼进 prompt（代码问题）
  B) 拼进去了但模型没用（提示词问题）

不看清原始 prompt 就无法区分。这个脚本 monkeypatch 掉 chat_json，
把 messages 原样打印出来。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("IA_LEGACY_ROOT", str(ROOT))
os.environ.setdefault("IA_REDIS", "0")

from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

import logging  # noqa: E402
logging.basicConfig(level=logging.WARNING)

from app.core.bridge import ensure_legacy_importable  # noqa: E402
ensure_legacy_importable()

from evaluator import Evaluator  # noqa: E402
import llm as llm_mod  # noqa: E402


HISTORY = [
    {"q": "你项目里的检索方案是什么？", "a": "用 bge-m3 做向量检索，切块 500 字。", "verdict": "pass"},
    {"q": "召回从多少提到多少？", "a": "从 71% 提到 82%。", "verdict": "pass"},
]

RESUME = "项目一：课程问答助手\n结果：召回从 71% 提到 82%，P99 延迟 1.8 秒"


class CaptureLLM:
    """假 LLM：把收到的 messages 打印出来，再返回一个固定 JSON。"""

    is_mock = False

    def __init__(self):
        self.captured = None

    def chat_json(self, messages, **kwargs):
        self.captured = messages
        return {
            "accuracy": 3, "depth": 2, "tradeoff": 2, "boundary": 2, "clarity": 3,
            "verdict": "partial", "weakest": "depth",
            "evidence": "召回 82%", "covered": [], "followup_hint": "",
            "repeated": False, "consistent": True,
        }

    def chat(self, messages, **kwargs):
        return ""


def main():
    fake = CaptureLLM()
    ev = Evaluator(fake)

    ev.evaluate(
        "你说的这个方案，具体效果怎么样？",
        "这个方案就是我们前面说的那套，召回 82%，效果不错。",
        phase="PROJECT_PROBE",
        ladder_name="怎么做",
        project="课程问答助手",
        target_element="指标",
        history=HISTORY,
        resume_context=RESUME,
    )

    msgs = fake.captured
    if not msgs:
        print("✗ 没抓到 messages")
        return 1

    print("=" * 66)
    print("  实际发给模型的 prompt")
    print("=" * 66)

    for m in msgs:
        role = m.get("role")
        content = m.get("content", "")
        print(f"\n[{role}]  长度 {len(content)} 字符")
        print("-" * 66)
        print(content)

    print("\n" + "=" * 66)
    print("  检查点")
    print("=" * 66)

    user = next((m["content"] for m in msgs if m["role"] == "user"), "")
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    both = system + "\n" + user

    checks = [
        ("历史里第 1 轮的问题", "检索方案是什么"),
        ("历史里第 2 轮的回答", "71% 提到 82%"),
        ("简历片段", "P99 延迟 1.8 秒"),
        ("上下文判断指令", "repeated"),
    ]
    ok = 0
    for label, needle in checks:
        hit = needle in both
        print(f"  {'✓' if hit else '✗'} {label}（找 \"{needle}\"）")
        ok += hit

    print(f"\n  {ok}/{len(checks)} 项在 prompt 里")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""验证：评分时模型能否看到对话历史。

## 要证明什么

之前评分只传「当前一问一答」，模型看不到前面说过什么。
现在改成传最近 6 轮。这个脚本用**同一个答案 + 不同历史**
分别评分，看分数/证据是否随上下文变化。

如果两个结果完全一样，说明历史没起作用。

## 用法

    PYTHONPATH=backend:. .venv/bin/python backend/tests/manual/verify_history_aware.py
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
from llm import build_llm  # noqa: E402

ANSWER = "这个方案就是我们前面说的那套，召回 82%，效果不错。"

# 场景 A：前面讲过详细的技术细节和数字 → "前面说的那套"有实指
HISTORY_RICH = [
    {"q": "你项目里的检索方案是什么？", "a": "用 bge-m3 做向量检索，切块 500 字，top20 重排。", "verdict": "pass"},
    {"q": "为什么要 top20 重排而不是全量？", "a": "全量重排延迟会到 3 秒以上，用户等不了，所以只在 top20 上做。", "verdict": "pass"},
    {"q": "召回从多少提到多少？", "a": "从 71% 提到 82%，代价是索引体积多了 30%。", "verdict": "pass"},
]

# 场景 B：前面全是空话，从没给过数字 → "82%"是凭空出现的
HISTORY_VAGUE = [
    {"q": "你项目里的检索方案是什么？", "a": "就是做了一些检索优化。", "verdict": "partial"},
    {"q": "具体怎么优化的？", "a": "效果还不错，提升比较明显。", "verdict": "partial"},
    {"q": "有具体数字吗？", "a": "记不太清了。", "verdict": "fail"},
]

RESUME = (
    "项目一：课程问答助手\n"
    "做法：RAG 检索，切块 500 字，top20 重排，bge-m3 做 embedding\n"
    "结果：召回从 71% 提到 82%，P99 延迟 1.8 秒\n"
    "代价：索引体积增加 30%\n"
)


def run(llm, history, label, resume=""):
    ev = Evaluator(llm)
    print(f"\n{'=' * 62}")
    print(f"  {label}")
    print(f"{'=' * 62}")
    print(f"  历史轮次: {len(history)}   简历片段: {'有' if resume else '无'}")

    try:
        result = ev.evaluate(
            "你说的这个方案，具体效果怎么样？",
            ANSWER,
            phase="PROJECT_PROBE",
            ladder_name="怎么做",
            project="课程问答助手",
            target_element="指标",
            history=history,
            resume_context=resume,
        )
        d = result.to_dict() if hasattr(result, "to_dict") else result

        print(f"  五维: acc={d.get('accuracy')} dep={d.get('depth')} "
              f"trd={d.get('tradeoff')} bnd={d.get('boundary')} clr={d.get('clarity')}")
        print(f"  avg={getattr(result, 'avg', '?')}  verdict={d.get('verdict')}")
        print(f"  证据: {str(d.get('evidence'))[:70]}")
        # 新增字段（模型是否理解上下文）
        for k in ("repeated", "consistent"):
            if k in d:
                print(f"  {k}: {d[k]}")
        return d
    except Exception as e:
        print(f"  [失败] {type(e).__name__}: {e}")
        return None


def main():
    print("构建 LLM…")
    llm = build_llm(mode="auto")
    print(f"  {type(llm).__name__}  is_mock={getattr(llm, 'is_mock', None)}")

    if getattr(llm, "is_mock", False):
        print("\n当前是 mock 模式，无法验证真实的上下文感知。")
        print("请在 .env 里配好 LLM_API_KEY 后重跑。")
        return 1

    a = run(llm, HISTORY_RICH, "场景 A：前面讲过详细数字（82% 有出处）", RESUME)
    b = run(llm, HISTORY_VAGUE, "场景 B：前面全是空话（82% 凭空出现）", RESUME)
    c = run(llm, [], "场景 C：无历史（基线对照）", "")

    print(f"\n{'=' * 62}")
    print("  结论")
    print(f"{'=' * 62}")

    if not (a and b):
        print("  有场景失败，无法比较")
        return 1

    same_ab = (a.get("evidence") == b.get("evidence")
               and a.get("accuracy") == b.get("accuracy"))
    if same_ab:
        print("  ✗ 场景 A 和 B 结果完全相同 —— 历史上下文没有起作用")
        return 1

    print("  ✓ 不同历史得到不同评分 → 模型确实在读上下文")
    print(f"    A(有出处) evidence: {str(a.get('evidence'))[:50]}")
    print(f"    B(凭空)   evidence: {str(b.get('evidence'))[:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

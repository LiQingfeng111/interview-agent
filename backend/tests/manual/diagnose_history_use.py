#!/usr/bin/env python3
"""诊断：为什么模型读了历史却给出相同分数。

## 三种可能

  A) 模型太弱，不会做多轮推理
  B) 提示词里历史的位置/格式让它忽略了
  C) 它的评分本来就只看当前这句（训练惯性）

## 怎么区分

直接给它一个**极端对比**：同一个答案，
一次历史里已说过完全一样的话（100% 重复），
一次历史完全无关。如果 100% 重复都不扣分，说明模型确实不读历史。
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

from evaluator import Evaluator, _has_repetition_evidence  # noqa: E402
from llm import build_llm  # noqa: E402

ANSWER = "召回从 71% 提到 82%，代价是索引体积增加了 30%，P99 延迟 1.8 秒。"

# 历史：一字不差说过同样的话
HISTORY_IDENTICAL = [
    {"q": "你项目里检索效果怎么样？", "a": ANSWER, "verdict": "pass"},
]

# 历史：完全无关的内容
HISTORY_UNRELATED = [
    {"q": "你的职业规划是什么？", "a": "我想在三年内成为独当一面的算法工程师。", "verdict": "partial"},
]


def show(ev_result, label):
    d = ev_result.to_dict() if hasattr(ev_result, "to_dict") else ev_result
    print(f"\n  {label}")
    print(f"    repeated={d.get('repeated')} consistent={d.get('consistent')}")
    print(f"    acc={d.get('accuracy')} dep={d.get('depth')} "
          f"trd={d.get('tradeoff')} bnd={d.get('boundary')} clr={d.get('clarity')}")
    print(f"    avg={getattr(ev_result, 'avg', '?'):.2f}")
    return d


def main():
    # ---- 先测规则侧：3-gram Jaccard 能不能认出完全相同 ----
    print("=" * 64)
    print("  1) 规则侧重复检测（不依赖模型）")
    print("=" * 64)
    r1 = _has_repetition_evidence(ANSWER, HISTORY_IDENTICAL)
    r2 = _has_repetition_evidence(ANSWER, HISTORY_UNRELATED)
    print(f"  与'一字不差'的历史比: {r1}  （应为 True）")
    print(f"  与'完全无关'的历史比: {r2}  （应为 False）")

    if not r1:
        print("  ✗ 规则侧就认不出重复，先修这个")
        return 1

    # ---- 再测模型侧 ----
    print()
    print("=" * 64)
    print("  2) 模型侧（真实 LLM）")
    print("=" * 64)
    llm = build_llm(mode="auto")
    print(f"  {type(llm).__name__}  is_mock={getattr(llm, 'is_mock', None)}")
    if getattr(llm, "is_mock", False):
        print("  mock 模式，跳过")
        return 0

    ev = Evaluator(llm)
    kw = dict(phase="PROJECT_PROBE", ladder_name="怎么做",
              project="课程问答助手", target_element="指标")
    resume = "项目一：课程问答助手\n结果：召回从 71% 提到 82%，索引体积 +30%"

    a = show(ev.evaluate("再说说效果？", ANSWER, history=HISTORY_IDENTICAL,
                         resume_context=resume, **kw), "A. 历史里一字不差说过（应被判重复）")
    b = show(ev.evaluate("再说说效果？", ANSWER, history=HISTORY_UNRELATED,
                         resume_context=resume, **kw), "B. 历史完全无关（不该判重复）")

    print()
    print("=" * 64)
    print("  结论")
    print("=" * 64)
    same = (a.get("accuracy") == b.get("accuracy")
            and a.get("repeated") == b.get("repeated"))
    if a.get("repeated") is True or a.get("accuracy") < b.get("accuracy", 99):
        print("  ✓ 模型对'100% 重复'有反应")
        return 0
    print("  ✗ 模型对'一字不差重复'也给出同样分数")
    print("    → 这个模型不会用历史做判断，只能靠规则侧兜底")
    return 1


if __name__ == "__main__":
    sys.exit(main())

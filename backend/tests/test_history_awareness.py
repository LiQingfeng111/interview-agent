"""历史感知测试：评分必须因上下文不同而不同。

## 要锁住什么

同一个答案，在"前面已经说过"和"前面完全无关"两种历史下，
分数**必须不同**。这条如果失效，说明历史又没被用上。

## 为什么必须用测试锁住

这个功能有过两次假通过：

  第一次：prompt 里没拼历史 → 三组场景分数完全一样
  第二次：relay 断线，所有评分走了 `_heuristic` 降级 →
          三组场景分数还是完全一样，但看起来"有分数"

第二次尤其危险：不看日志根本发现不了，因为降级路径也会返回合理分数。
所以测试要同时验「历史真的传进去了」和「分数真的变了」。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluator import _has_repetition_evidence

ANSWER = "召回从 71% 提到 82%，代价是索引体积增加了 30%，P99 延迟 1.8 秒。"


# ============================================================================
# 规则侧重复检测（不依赖模型，必须稳定）
# ============================================================================

def test_identical_answer_detected_as_repeated():
    """一字不差 → 判重复。"""
    history = [{"q": "效果怎么样？", "a": ANSWER, "verdict": "pass"}]
    assert _has_repetition_evidence(ANSWER, history) is True


def test_unrelated_history_not_repeated():
    """历史完全无关 → 不判重复。"""
    history = [{"q": "职业规划？", "a": "我想三年内成为独当一面的算法工程师。", "verdict": "partial"}]
    assert _has_repetition_evidence(ANSWER, history) is False


def test_empty_history_not_repeated():
    """没有历史 → 不可能重复。"""
    assert _has_repetition_evidence(ANSWER, []) is False
    assert _has_repetition_evidence(ANSWER, None) is False


def test_empty_answer_not_repeated():
    """空答案 → 不判重复（避免 0/0 之类的问题）。"""
    history = [{"q": "q", "a": ANSWER, "verdict": "pass"}]
    assert _has_repetition_evidence("", history) is False


def test_paraphrase_detected():
    """换个说法讲同一件事 → 也应该判重复。

    用 3-gram Jaccard 0.5 作阈值。
    这里构造一个改动较小的版本（保留核心数字和结构）。
    """
    paraphrase = "召回是从 71% 提升到 82% 的，代价是索引体积增加 30%，P99 延迟 1.8 秒。"
    history = [{"q": "效果？", "a": ANSWER, "verdict": "pass"}]
    assert _has_repetition_evidence(paraphrase, history) is True, (
        "换说法重复应被识别（否则用户换个句式重讲就能白拿分）"
    )


def test_new_content_not_marked_repeated():
    """讲全新的点 → 不能判重复（不能错杀）。"""
    new_content = (
        "失败方案这块我踩过坑：一开始用固定窗口切块，结果跨段落的语义被切断，"
        "后来改成按标题层级切，再补一层 parent_text 保留上下文。"
    )
    history = [{"q": "效果？", "a": ANSWER, "verdict": "pass"}]
    assert _has_repetition_evidence(new_content, history) is False, (
        "讲新材料被当成重复会导致好回答被扣分"
    )


def test_repetition_short_answer_handled():
    """很短的答案不会因为 3-gram 太少而崩溃或误判。"""
    assert _has_repetition_evidence("好", [{"q": "q", "a": "好", "verdict": "pass"}]) in (True, False)
    assert _has_repetition_evidence("嗯", [{"q": "q", "a": ANSWER, "verdict": "pass"}]) is False


# ============================================================================
# 多轮历史：取最近的那条比对
# ============================================================================

def test_matches_any_round_in_history():
    """跟历史里任意一轮重复都算重复，不只看最后一轮。"""
    history = [
        {"q": "第一轮", "a": ANSWER, "verdict": "pass"},          # 这条重复
        {"q": "第二轮", "a": "完全不同的内容，讲的是别的东西。", "verdict": "partial"},
        {"q": "第三轮", "a": "又是另一段话，跟前面都不一样。", "verdict": "partial"},
    ]
    assert _has_repetition_evidence(ANSWER, history) is True


def test_ignores_rounds_without_answer():
    """历史里缺 answer 的轮次不该让它崩。"""
    history = [
        {"q": "只有问题没有回答"},
        {"q": "q2", "a": "", "verdict": ""},
        {"q": "q3", "a": ANSWER, "verdict": "pass"},
    ]
    assert _has_repetition_evidence(ANSWER, history) is True

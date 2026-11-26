"""双轨评分测试：逻辑分与模型分如何融合。

## 这批测试要证明什么

1. **规则分能识别硬证据**：有数字/基线/边界 → accuracy 明显高于空话
2. **规则分不会误杀**：讲得实在但没数字（保密场景）不会被判 1 分
3. **融合权重按维度不同**：accuracy 规则权重 0.6，depth 纯模型
4. **avg 是真·五维平均**（融合后重算，不是模型原来的 avg）
5. **verdict 不受影响**：阈值仍是唯一裁决者

## 为什么值得单独测

单一模型评分有两个硬伤：会漂移、会被人话术骗。
双轨是为了解决这两点。但如果融合逻辑写错（比如权重搞反），
反而比单轨更糟——所以必须用固定输入锁死行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import rubric


# ============================================================================
# 规则分：识别硬证据
# ============================================================================

def test_metrics_boost_accuracy():
    """有具体数字 → accuracy 显著高于空话。"""
    vague = "我们做了一些优化，效果还不错。"
    solid = "召回从 71% 提到 82%，P99 延迟 1.8 秒，索引体积多了 30%。"

    r_vague = rubric.rule_scores(vague)
    r_solid = rubric.rule_scores(solid)

    assert r_solid["accuracy"] > r_vague["accuracy"] + 1.0, (
        f"有数字({r_solid['accuracy']})应明显高于空话({r_vague['accuracy']})"
    )
    assert r_solid["signals"]["metrics"] >= 3
    assert r_vague["signals"]["metrics"] == 0


def test_baseline_adds_accuracy():
    """有基线对照 → 加分（说明不是自说自话）。"""
    no_base = "准确率 0.86。"
    with_base = "准确率 0.86，比原来的规则基线 0.72 高 14 个点。"

    a = rubric.rule_scores(no_base)["accuracy"]
    b = rubric.rule_scores(with_base)["accuracy"]
    assert b > a, f"有基线({b})应高于没基线({a})"


def test_empty_phrases_penalized():
    """套话密度高 → 扣分。"""
    plain = "召回从 71% 到 82%。"
    empty = "效果不错，提升明显，优化了一下，大概是这样的。"

    a = rubric.rule_scores(plain)["accuracy"]
    b = rubric.rule_scores(empty)["accuracy"]
    assert a > b, f"实数据({a})应高于套话({b})"


def test_rule_floor_protects_honest_answers():
    """规则分有下限：讲得实在但没数字（保密场景）不会被判死。

    这是双轨设计的关键——规则负责"别被空话骗"，
    但不能反过来把"真话但没数据"判成 1 分。
    """
    honest = (
        "这个项目的数据涉及公司内部指标，我不能说具体数字。"
        "但我可以讲方法：我们先做了离线评测，用 200 条人工标注的问答对，"
        "双人交叉校验，一致性 kappa 0.81。"
    )
    r = rubric.rule_scores(honest)
    assert r["accuracy"] >= rubric.RULE_FLOOR, (
        f"诚实但无数字的回答不该低于下限 {rubric.RULE_FLOOR}"
    )


def test_boundary_words_raise_boundary():
    """说清"什么情况下不行" → boundary 加分。"""
    no_bound = "这个方案用了向量检索。"
    with_bound = "这个方案只在数据量小于 100 万时有效，数据量再大就会因为索引膨胀而失效。"

    a = rubric.rule_scores(no_bound)["boundary"]
    b = rubric.rule_scores(with_bound)["boundary"]
    assert b > a, f"有边界({b})应高于没边界({a})"


def test_structure_words_raise_clarity():
    """有结构词 → clarity 加分。"""
    flat = "做了切块然后做检索然后做重排效果还行"
    structured = (
        "首先是切块：500 字是实测选的。"
        "其次是检索：用 bge-m3。"
        "然后是重排：只在 top20 上做。"
        "结果是召回提升 11 个点。"
    )

    a = rubric.rule_scores(flat)["clarity"]
    b = rubric.rule_scores(structured)["clarity"]
    assert b > a, f"有结构({b})应高于无结构({a})"


def test_empty_answer_returns_floor():
    """空回答 → 全维度下限，不抛异常。"""
    r = rubric.rule_scores("")
    assert r["accuracy"] == rubric.RULE_FLOOR
    assert r["signals"].get("empty") is True


# ============================================================================
# 融合：权重按维度不同
# ============================================================================

def test_merge_accuracy_favors_rule():
    """accuracy 上规则权重 0.6，模型权重 0.4。

    场景：模型说 4 分（讲得流畅），规则说 2 分（一个数字都没有）。
    平均会是 3，等于认可了"没数据也准确"。期望结果更接近 2.8。
    """
    llm = {"accuracy": 4.0, "depth": 4.0, "tradeoff": 3.0,
           "boundary": 3.0, "clarity": 4.0}
    rule = {"accuracy": 2.0, "boundary": 3.0, "clarity": 4.0,
            "signals": {"metrics": 0}}

    m = rubric.merge(llm, rule)
    # 4*0.4 + 2*0.6 = 2.8
    assert m["accuracy"] == 2.8, f"期望 2.8，实际 {m['accuracy']}"
    assert "rule" in m["score_source"]["accuracy"]


def test_merge_depth_is_pure_model():
    """depth 规则算不了 → 完全采用模型分。"""
    llm = {"accuracy": 3.0, "depth": 4.5, "tradeoff": 3.0,
           "boundary": 3.0, "clarity": 3.0}
    rule = {"accuracy": 3.0, "boundary": 3.0, "clarity": 3.0, "signals": {}}

    m = rubric.merge(llm, rule)
    assert m["depth"] == 4.5, f"depth 应该完全来自模型，实际 {m['depth']}"
    assert m["score_source"]["depth"] == "model"


def test_merge_tradeoff_is_pure_model():
    llm = {"accuracy": 3.0, "depth": 3.0, "tradeoff": 2.5,
           "boundary": 3.0, "clarity": 3.0}
    rule = {"accuracy": 3.0, "boundary": 3.0, "clarity": 3.0, "signals": {}}

    m = rubric.merge(llm, rule)
    assert m["tradeoff"] == 2.5
    assert m["score_source"]["tradeoff"] == "model"


def test_merge_clarity_favors_rule():
    """clarity 规则权重 0.7（结构词、句长可量化）。"""
    llm = {"accuracy": 3.0, "depth": 3.0, "tradeoff": 3.0,
           "boundary": 3.0, "clarity": 5.0}
    rule = {"accuracy": 3.0, "boundary": 3.0, "clarity": 2.0, "signals": {}}

    m = rubric.merge(llm, rule)
    # 5*0.3 + 2*0.7 = 2.9
    assert m["clarity"] == 2.9, f"期望 2.9，实际 {m['clarity']}"


def test_merge_falls_back_when_rule_missing():
    """规则分缺失 → 用模型分，不崩。"""
    llm = {"accuracy": 4.0, "depth": 3.0, "tradeoff": 3.0,
           "boundary": 3.0, "clarity": 4.0}
    m = rubric.merge(llm, {})
    assert m["accuracy"] == 4.0
    assert m["score_source"]["accuracy"] == "model"


def test_merge_falls_back_when_model_missing():
    """模型分缺失（降级）→ 用规则分，不崩。"""
    rule = {"accuracy": 3.5, "boundary": 2.5, "clarity": 3.0, "signals": {}}
    m = rubric.merge({}, rule)
    assert m["accuracy"] == 3.5
    assert m["score_source"]["accuracy"] == "rule"


def test_merge_clamps_to_valid_range():
    """分数必须落在 [1, 5]，不能越界。"""
    llm = {"accuracy": 9.0, "depth": -3.0, "tradeoff": 3.0,
           "boundary": 3.0, "clarity": 3.0}
    m = rubric.merge(llm, {})
    for dim in ("accuracy", "depth", "tradeoff", "boundary", "clarity"):
        assert 1.0 <= m[dim] <= 5.0, f"{dim}={m[dim]} 越界"


# ============================================================================
# apply：端到端
# ============================================================================

def test_apply_recomputes_avg():
    """avg 必须是融合后五维的平均，不是模型原来的 avg。"""
    llm = {
        "accuracy": 4.0, "depth": 4.0, "tradeoff": 3.0,
        "boundary": 3.0, "clarity": 4.0,
        "avg": 3.6,  # 模型自己算的，应该被覆盖
    }
    answer = "我们做了一些优化。"  # 无数字 → 规则 accuracy 低

    out = rubric.apply(llm, answer)
    dims = [out["accuracy"], out["depth"], out["tradeoff"],
            out["boundary"], out["clarity"]]
    expected = round(sum(dims) / 5, 2)

    assert out["avg"] == expected, f"avg 应重算为 {expected}，实际 {out['avg']}"
    assert out["avg"] != 3.6, "不该沿用模型的旧 avg"


def test_apply_preserves_verdict():
    """verdict 由阈值决定，双轨评分不能改它。"""
    llm = {"accuracy": 4.0, "depth": 4.0, "tradeoff": 4.0,
           "boundary": 4.0, "clarity": 4.0,
           "avg": 4.0, "verdict": "pass"}
    out = rubric.apply(llm, "我们做了优化。")
    assert out["verdict"] == "pass", "verdict 不该被评分逻辑改动"


def test_apply_records_score_source():
    """要能追溯每个维度来自谁（报告里要展示）。"""
    llm = {"accuracy": 4.0, "depth": 4.0, "tradeoff": 3.0,
           "boundary": 3.0, "clarity": 4.0}
    out = rubric.apply(llm, "召回从 71% 提到 82%。")

    src = out["score_source"]
    assert set(src) == {"accuracy", "depth", "tradeoff", "boundary", "clarity"}
    assert "model" in src["depth"]
    assert "rule" in src["accuracy"]
    assert out["rule_signals"]["metrics"] >= 2

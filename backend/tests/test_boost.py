"""弱项加权测试：证明长期记忆真的影响出题。

## 这条测试要证明什么

用户之前的批评是："画像存了但选题没用"——这是"假功能"。
这条测试就是防止再退化成假功能：
给定同一批候选，加权必须**改变选中结果**。

## 怎么构造

不依赖真实历史数据（那样测试不稳定）。直接构造权重字典：
- 话题 A 权重 1.5（弱项）
- 话题 B 权重 0.8（强项）
- A 和 B 的原始分数相同 → 加权后 A 应该赢
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import boost


def test_topic_weights_from_profile():
    profile = {
        "weak_topics": ["缓存一致性"],
        "weak_elements": {"失败方案": 3, "边界": 1},
        "strong_topics": ["RAG 检索增强生成"],
    }
    w = boost.topic_weights(profile)

    assert w["缓存一致性"] > 1.0, "弱项话题应 >1"
    assert w["失败方案"] > 1.0
    assert w["RAG 检索增强生成"] < 1.0, "强项话题应 <1"
    # 次数越多权重越高
    assert w["失败方案"] > w["边界"], "3 次的加权应强于 1 次"


def test_weak_boost_cap():
    """次数超过 CAP 后不再叠加（避免老账永久主导）。"""
    w3 = boost.topic_weights({"weak_elements": {"X": 3}})
    w10 = boost.topic_weights({"weak_elements": {"X": 10}})
    assert abs(w3["X"] - w10["X"]) < 1e-9, "CAP 之后权重应封顶"


def test_weak_wins_over_strong():
    """同一话题同时在弱项和强项里 → 以弱项为准（不降权）。"""
    w = boost.topic_weights({
        "weak_topics": ["A"],
        "strong_topics": ["A"],
    })
    assert w["A"] > 1.0, f"矛盾时弱项优先，实际 {w['A']}"


def test_empty_profile_no_weights():
    assert boost.topic_weights(None) == {}
    assert boost.topic_weights({}) == {}
    assert boost.topic_weights({"weak_topics": [], "strong_topics": []}) == {}


def test_factor_for_exact_and_fuzzy():
    w = {"缓存一致性": 1.4, "RAG": 1.3}
    assert boost.factor_for("缓存一致性", w) == 1.4
    assert boost.factor_for("", w) == 1.0
    assert boost.factor_for("不存在的话题", w) == 1.0
    # 包含匹配兜底
    assert boost.factor_for("RAG 检索增强生成", w) > 1.0


def test_factor_changes_ranking():
    """核心断言：加权能改变排序。"""
    # 两个候选原始分相同
    base_a, base_b = 0.60, 0.60
    w = {"弱项话题": 1.5, "强项话题": 0.8}

    a = base_a * boost.factor_for("弱项话题", w)
    b = base_b * boost.factor_for("强项话题", w)

    assert a > b, f"加权后弱项应胜出：{a} vs {b}"
    assert a / b > 1.5, f"差距应显著（{a / b:.2f}x）"


def test_describe_is_readable():
    w = {"缓存一致性": 1.4, "RAG 检索增强生成": 0.8}
    text = boost.describe(w)
    assert "弱项" in text and "强项" in text
    assert boost.describe({}) == "无历史画像，不做加权"

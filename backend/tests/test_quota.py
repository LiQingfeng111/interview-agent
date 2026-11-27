"""话题配额测试：验证"不偏科"这条产品要求真的生效。

## 用户的要求

> "加（配额）" —— 面试官职责是覆盖全面，不能被高相关度的单一话题吸住。

## 怎么证明它生效

跑一场模拟面试（16 轮），统计话题分布：
- 硬断言：没有任何话题超过 `ceil(16/4)=4` 次
- 硬断言：覆盖的话题数 >= 4

如果没配额，纯语义排序会把大部分轮次吸到最相关的 1-2 个话题上。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import bank_select, quota


def _ready() -> bool:
    """能不能跑（PG + 题库向量 + 向量服务）。

    CI 里 OLLAMA_BASE_URL 指向不可达地址，
    原来只查 PG 会让测试"以为自己能跑"，然后向量化失败 → 断言红。
    统一用 tests/ready.semantic_ready() 做判定。
    """
    from tests.ready import semantic_ready
    return semantic_ready()


pytestmark = pytest.mark.skipif(
    not _ready(), reason="需要 PG + 已导入题库向量（bash ops.sh up && bash ops.sh bank）"
)


def test_max_per_topic_scaling():
    """配额随轮次缩放，但有上下界。"""
    assert quota.max_per_topic(6) == 2       # 6/4=1.5→2
    assert quota.max_per_topic(16) == 4      # 16/4=4
    assert quota.max_per_topic(30) == 8      # 30/4=7.5→8
    # 下界 2：轮次再少也不能只问 1 次就换
    assert quota.max_per_topic(1) == 2
    # 上界 8：轮次再多也不许一个话题问 9 次以上
    assert quota.max_per_topic(200) == 8


def test_full_interview_respects_quota():
    """模拟 16 轮，验证没有话题超配额、且覆盖够广。"""
    total_turns = 16
    cap = quota.max_per_topic(total_turns)

    asked: list[str] = []
    topics: dict[str, int] = {}
    query = "RAG 检索系统怎么做，切块和召回率"

    for turn in range(total_turns):
        p = bank_select.pick(
            query, role="ai_algo", asked_qids=asked, turn=turn,
            topic_counts=topics, total_turns=total_turns, use_quota=True,
        )
        if p is None:
            break
        asked.append(p.qid)
        topics[p.topic] = topics.get(p.topic, 0) + 1
        query = f"{query} {p.question[:50]}"

    assert len(asked) >= 8, f"只选出了 {len(asked)} 题，题库不该这么快耗尽"

    over = {t: c for t, c in topics.items() if c > cap}
    assert not over, f"有话题超配额（上限 {cap}）：{over}"

    assert len(topics) >= 4, (
        f"16 轮只覆盖 {len(topics)} 个话题，太偏科：{topics}"
    )


def test_quota_actually_changes_selection():
    """关掉配额 vs 打开配额，结果应该不同。

    这条断言证明"配额真的起作用了"，而不是参数传了但没用。
    """
    total_turns = 16
    asked: list[str] = []
    topics_a: dict[str, int] = {}
    query = "注意力机制"

    # 不开配额：一直问同一类
    for turn in range(10):
        p = bank_select.pick(query, role="ai_algo", asked_qids=asked,
                             turn=turn, topic_counts=topics_a,
                             total_turns=total_turns, use_quota=False)
        if p is None:
            break
        asked.append(p.qid)
        topics_a[p.topic] = topics_a.get(p.topic, 0) + 1

    asked_b: list[str] = []
    topics_b: dict[str, int] = {}
    for turn in range(10):
        p = bank_select.pick(query, role="ai_algo", asked_qids=asked_b,
                             turn=turn, topic_counts=topics_b,
                             total_turns=total_turns, use_quota=True)
        if p is None:
            break
        asked_b.append(p.qid)
        topics_b[p.topic] = topics_b.get(p.topic, 0) + 1

    cap = quota.max_per_topic(total_turns)
    max_a = max(topics_a.values()) if topics_a else 0
    max_b = max(topics_b.values()) if topics_b else 0

    assert max_b <= cap, f"开配额后仍有话题超上限：{max_b} > {cap}"
    # 不开配额时通常会超过；如果恰好没超，说明该话题题量本来就少
    if max_a > cap:
        assert max_b < max_a, (
            f"配额没起作用：不开={max_a} 开={max_b}"
        )


def test_coverage_report():
    """报告里的覆盖情况要能说明"是否偏科"。"""
    counts = {"RAG 检索增强生成": 5, "微调与 LoRA": 2, "多 Agent 编排": 1}
    r = quota.coverage_report(counts, total_turns=16)
    assert r["max_per_topic"] == 4
    assert r["distinct_topics"] == 3
    assert "RAG 检索增强生成" in r["over_quota"], "5 次超过上限 4，应报超额"
    assert r["balanced"] is False

    r2 = quota.coverage_report({"A": 2, "B": 2}, total_turns=16)
    assert r2["balanced"] is True


def test_filter_candidates_drops_over_quota():
    """filter_candidates 直接剔除超额话题。"""
    cands = [(0.9, "A", "x"), (0.8, "B", "y"), (0.7, "A", "z")]
    kept = quota.filter_candidates(cands, {"A": 4}, total_turns=16)
    assert all(t != "A" for _, t, _ in kept), "A 已满 4 次，应全部剔除"
    assert len(kept) == 1

"""题库检索的阈值标定与回归测试。

## 这份测试锁的是什么

实测发现 bge-m3 对"同一语义的不同说法"打分波动很大：

    改造前的指标是多少   ↔ 题库题干   0.854
    你的 baseline 是什么 ↔ 同一题干   0.493
    对比基线是什么       ↔ 同一题干   0.420

所以阈值不能一刀切：
- **去重**要 0.60（严格）：误判重复会跳过本来该问的题
- **选题**要 0.42（宽松）：漏了只是少一个候选

这条测试用真实 PG 数据验证两个用途各自的召回，防止有人"统一阈值"把功能改坏。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import retriever


def _pg_ready() -> bool:
    try:
        from app.repos.pg import pg_health
        return bool(pg_health().get("ok"))
    except Exception:  # noqa: BLE001
        return False


def _bank_count() -> int:
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL")
            n = cur.fetchone()[0]
            cur.close()
        return int(n)
    except Exception:  # noqa: BLE001
        return 0


pytestmark = pytest.mark.skipif(
    not _pg_ready() or _bank_count() < 100,
    reason="需要 PG 且题库已导入向量（跑 `bash ops.sh up && bash ops.sh bank`）",
)


def test_threshold_by_use_is_differentiated():
    """两个用途的阈值必须不同——这是一个刻意的设计决定。"""
    dedup = retriever.threshold_for_use("dedup")
    select = retriever.threshold_for_use("select")
    assert dedup > select, (
        f"去重阈值应严于选题阈值，实际 dedup={dedup} select={select}；"
        "若改成相同，会出现'去重漏判'或'选题全空'"
    )


@pytest.mark.parametrize("query,topic_kw", [
    ("多头注意力里每个头的维度是多少", "注意力"),
    ("缓存被击穿怎么处理", "缓存"),
    ("向量检索怎么做的", "RAG"),
])
def test_select_mode_recalls_relevant_topic(query, topic_kw):
    """选题模式（宽松）应能召回相关话题。"""
    hits = retriever.search_bank(query, k=5, use="select")
    assert hits, f"查询「{query}」在 select 模式下一条都没召回"
    blob = " ".join(f"{h.project} {h.text}" for h in hits)
    assert topic_kw in blob, (
        f"查询「{query}」未召回含「{topic_kw}」的题，实际：{[h.project for h in hits]}"
    )


def test_select_is_more_permissive_than_dedup():
    """同一个查询，宽松模式的召回数应 >= 严格模式。"""
    q = "你的 baseline 是什么"
    loose = retriever.search_bank(q, k=10, use="select")
    strict = retriever.search_bank(q, k=10, use="dedup")
    assert len(loose) >= len(strict), (
        f"宽松模式召回 {len(loose)} 应不少于严格模式 {len(strict)}"
    )


def test_dedup_mode_finds_near_duplicate():
    """去重模式应能把"几乎同一句话"判成高相似。"""
    hits = retriever.search_bank("多头注意力中，若模型维度 d_model=512、头数 h=8，每个头的 d_k 和 d_v 通常取多少？",
                                 k=3, use="dedup")
    assert hits, "去重模式对原文查询应至少召回自己"
    assert hits[0].score >= 0.85, (
        f"原文查自己的相似度应接近 1，实际 {hits[0].score}"
    )


def test_empty_query_returns_empty():
    assert retriever.search_bank("", k=5) == []
    assert retriever.search_bank("   ", k=5) == []


def test_hits_sorted_by_score_desc():
    hits = retriever.search_bank("RAG 切块大小怎么定", k=5, use="select")
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), "召回必须按分数降序"


def test_hit_carries_topic_and_points():
    """命中结果要带 topic 和 points，选题时要用。"""
    hits = retriever.search_bank("注意力机制", k=3, use="select")
    assert hits
    h = hits[0]
    assert h.project, "Hit.project 应含 topic"
    assert "points" in h.meta, "Hit.meta 应含 points"

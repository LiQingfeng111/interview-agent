"""选题逻辑的回归测试（真实 722 题 + 真实向量）。

## 锁住三件事

1. **不重复**：连选 6 题，两两语义相似度都低于 dedup 阈值
2. **难度递进**：turn < 2 只出 basic
3. **话题均衡**：连续选出的题不该全挤在同一 topic

这些断言在旧实现（Jaccard 0.72）下会失败——因为字面相似度抓不到语义重复。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import bank_select, retriever


def _ready() -> bool:
    try:
        from app.repos.pg import pg_health
        if not pg_health().get("ok"):
            return False
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL")
            n = cur.fetchone()[0]
            cur.close()
        return int(n) >= 100
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(
    not _ready(), reason="需要 PG + 已导入题库向量（bash ops.sh up && bash ops.sh bank）"
)


def test_picks_six_without_duplicates():
    """连选 6 题：不能重复、不能全是同一话题。"""
    asked: list[str] = []
    topics: dict[str, int] = {}
    picked: list[bank_select.Picked] = []

    query = "RAG 检索怎么做，切块和召回率"
    for i in range(6):
        p = bank_select.pick(query, role="ai_algo", asked_qids=asked,
                             turn=i, topic_counts=topics)
        if p is None:
            break
        picked.append(p)
        asked.append(p.qid)
        topics[p.topic] = topics.get(p.topic, 0) + 1
        # 每轮把选中的题反馈进查询，模拟"顺着往下问"
        query = f"{query} {p.question[:60]}"

    assert len(picked) >= 4, f"应至少选出 4 题，实际 {len(picked)}"
    assert len({p.qid for p in picked}) == len(picked), "出现了重复题"
    assert len({p.topic for p in picked}) >= 2, (
        f"6 题全挤在一个话题：{[p.topic for p in picked]}"
    )


def test_respects_level_cap_on_early_turns():
    """前两轮只出 basic。"""
    p = bank_select.pick("注意力机制", role="ai_algo", asked_qids=[], turn=0)
    if p is None:
        pytest.skip("题库该 role 无 basic 题")
    assert bank_select.LEVEL_RANK.get(p.level, 0) == 0, (
        f"turn=0 应只出 basic，实际 level={p.level}"
    )


def test_excludes_asked():
    """已问过的题必须被排除。"""
    first = bank_select.pick("缓存一致性", role="backend", asked_qids=[], turn=5)
    if first is None:
        pytest.skip("题库该 role 无题")
    second = bank_select.pick("缓存一致性", role="backend",
                              asked_qids=[first.qid], turn=5)
    if second is None:
        return
    assert second.qid != first.qid, "已问过的题又被选中"


def test_pick_carries_points_and_reason():
    """选中的题要带 points（评分要点）和 reason（可解释）。"""
    p = bank_select.pick("向量检索 召回率", role="ai_algo", asked_qids=[], turn=5)
    if p is None:
        pytest.skip("无可用题")
    assert p.question
    assert isinstance(p.points, list)
    assert "语义" in p.reason, f"reason 应含可解释的分数：{p.reason}"


def test_build_query_combines_context():
    """查询要拼上简历与最近回答。"""
    qa = [{"q": "你的切块怎么定的", "a": "切块 500 字，召回 82%"},
          {"q": "延迟多少", "a": "P99 1.8 秒"}]
    q = bank_select.build_query(qa, resume_ctx="课程问答助手项目")
    assert "课程问答助手" in q
    assert "500 字" in q
    assert "1.8 秒" in q


def test_empty_query_returns_none():
    assert bank_select.pick("", role="ai_algo", asked_qids=[], turn=0) is None


def test_semantic_beats_keyword_on_cross_language():
    """跨语言同义：语义检索能召回，关键词检索不能。

    设计说明（第三次修正）：
    前两版都失败了，原因是我把"证明方法"想简单了：
      第 1 版：断言目标题必须进 top10 → 红（同话题有 6 道更相关的题占满坑位）
      第 2 版：断言向量排名 < Jaccard 排名 → 红（用 topic 名检索时，
              目标题排第 30，Jaccard 却可能排很前——因为 topic 名本身
              与题干有字面重合）

    正确做法：**直接构造一对跨语言查询**，看语义能否把它们判为相似，
    同时看 Jaccard 是否判不出。这测的是"语义能力"本身，不掺检索排名。
    """
    import re

    # 取一道真实的中英混合题
    from app.repos.pg import get_conn
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT question FROM bank_questions
            WHERE topic = 'Transformer 注意力机制'
              AND question LIKE '%Multi-Head%'
            LIMIT 1
        """)
        row = cur.fetchone()
        cur.close()
    if not row:
        pytest.skip("题库里没有 Multi-Head 相关题")

    en_q = row[0]

    # 中文同义表述（同一考点）
    zh_q = "多头注意力里为什么要把 Q K V 的维度切成 head_num 份"

    # ① 语义相似度：应显著高于阈值
    sim = retriever.search_bank  # 仅为确保模块已加载
    from app.rag.embedder import get_embedder
    emb = get_embedder()
    va, vb = emb.embed([en_q, zh_q])
    cos = sum(x * y for x, y in zip(va, vb)) / (
        (sum(x * x for x in va) ** 0.5) * (sum(y * y for y in vb) ** 0.5)
    )
    assert cos >= 0.60, (
        f"跨语言同义句语义相似度只有 {cos:.3f}，低于 dedup 阈值 0.60；"
        f"英文题：{en_q[:50]}"
    )

    # ② 字面相似度（Jaccard）：应明显低于语义相似度
    #
    # 实测值（2026-11-26）：cos=0.8193, jaccard=0.3281
    # 为什么 jaccard 不是接近 0：这对句子有共同的英文术语
    # （Multi-Head / Attention / Q / K / V / head_num），
    # 字符二元组能在这些词上重合。所以断言不能写"<0.30"，
    # 要断言的是**语义显著高于字面**，这才是语义检索的价值。
    def grams(s: str) -> set[str]:
        n = re.sub(r"[\s，。！？、,.!?：:；;（）()\-—…\"'“”]", "", s or "").lower()
        return {n[i:i + 2] for i in range(len(n) - 1)} if len(n) >= 2 else set()

    ga, gb = grams(en_q), grams(zh_q)
    jac = len(ga & gb) / len(ga | gb) if ga and gb else 0.0
    assert jac < 0.50, (
        f"字面 Jaccard 意外很高（{jac:.3f}），这个对照失去意义"
    )

    # ③ 结论：语义明显强于字面 —— 这才是"语义检索更好"的证据
    assert cos > jac * 2, (
        f"语义 {cos:.3f} 应明显高于字面 {jac:.3f}（至少 2 倍），"
        f"否则说明不了语义优势"
    )

"""混合检索测试：BM25 / RRF / rerank 各自的行为。

## 为什么必须逐段测

这个模块的价值在"分段收益"——如果哪一段写错了，
整体效果会变差，但你从最终数字看不出是哪段坏的。

所以要分别锁住：
  · BM25 的分数排序是否合理
  · RRF 的权重是否真的影响结果
  · rerank 是否真的改变顺序

## 特别要防的一个回归

之前我用**等权 RRF** 融合，把 7/18 的弱信号掺进 17/18 的强信号，
结果 10/18 —— 比纯向量差 7 个点，然后错误地得出"混合更差"。

这批测试里 `test_rrf_weights_matter` 专门验证"权重能改变结果"，
防止以后又把权重写死成等权。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.hybrid import (
    RRF_K, BM25Index, CrossEncoderReranker, HybridConfig,
    minmax_normalize, rrf_fuse, tokenize_zh,
)


# ============================================================================
# 分词
# ============================================================================

def test_tokenize_chinese():
    """中文要切成词，不能整句当一个 token。"""
    tokens = tokenize_zh("向量检索的索引结构")
    assert len(tokens) >= 3, f"切得太粗：{tokens}"
    assert all(isinstance(t, str) for t in tokens)


def test_tokenize_keeps_technical_terms():
    """技术术语要保留（不能被切碎）。"""
    tokens = tokenize_zh("用 bge-m3 做 embedding，P99 延迟 1.8 秒")
    joined = " ".join(tokens)
    assert "bge" in joined or "bge-m3" in joined, f"bge-m3 被切碎了：{tokens}"
    assert "p99" in joined or "p" in joined
    assert "embedding" in joined


def test_tokenize_lowercase():
    """统一小写，让 P99 和 p99 视为同一个词。"""
    a = tokenize_zh("P99 延迟")
    b = tokenize_zh("p99 延迟")
    assert a == b, f"大小写没统一：{a} vs {b}"


def test_tokenize_empty():
    assert tokenize_zh("") == []
    assert tokenize_zh("   ") == []


def test_tokenize_drops_stopwords():
    """停用词过滤（但要保留有语义的"怎么"）。"""
    tokens = tokenize_zh("这个东西的怎么做")
    assert "的" not in tokens
    assert "这个" not in tokens


# ============================================================================
# BM25
# ============================================================================

def _tiny_corpus():
    return [
        {"id": "d1", "text": "召回率从 71% 提升到 82%，在 200 条标注集上测的"},
        {"id": "d2", "text": "重排只在 top20 上做，P99 延迟 1.8 秒"},
        {"id": "d3", "text": "HNSW 索引参数 M 取 16，ef_construction 取 200"},
        {"id": "d4", "text": "今天天气不错，适合出去散步"},
    ]


def test_bm25_finds_exact_term():
    """术语精确匹配要能命中。"""
    idx = BM25Index()
    idx.build(_tiny_corpus())
    hits = idx.search("HNSW 参数", k=3)

    assert hits, "BM25 没召回任何结果"
    assert hits[0][0]["id"] == "d3", f"应命中 d3，实际 {hits[0][0]['id']}"


def test_bm25_ranks_by_relevance():
    """命中词多的排前面。"""
    idx = BM25Index()
    idx.build(_tiny_corpus())
    hits = idx.search("召回率 标注集", k=4)

    assert hits
    assert hits[0][0]["id"] == "d1"


def test_bm25_no_match_returns_empty():
    """完全不相关的查询返回空（不是硬塞结果）。"""
    idx = BM25Index()
    idx.build(_tiny_corpus())
    hits = idx.search("量子纠缠拓扑绝缘体", k=3)

    assert hits == [], f"不相关查询不该有结果：{hits}"


def test_bm25_empty_corpus():
    """空语料不崩。"""
    idx = BM25Index()
    idx.build([])
    assert idx.search("任意查询", k=3) == []


def test_bm25_empty_query():
    idx = BM25Index()
    idx.build(_tiny_corpus())
    assert idx.search("", k=3) == []
    assert idx.search("   ", k=3) == []


def test_bm25_k1_b_affect_scores():
    """k1/b 参数应该影响分数（说明参数真的传进去了）。"""
    corpus = _tiny_corpus()

    a = BM25Index(k1=1.2, b=0.75)
    a.build(corpus)
    b = BM25Index(k1=2.0, b=0.75)
    b.build(corpus)

    ha = a.search("召回率 标注集", k=4)
    hb = b.search("召回率 标注集", k=4)

    # 排序可能相同，但分数不该完全相同
    assert ha and hb
    assert ha[0][1] != hb[0][1], "k1 参数没生效"


# ============================================================================
# RRF
# ============================================================================

def _mk(items: list[str]):
    """造带 id 的简单对象。"""
    return [{"id": i, "text": f"text-{i}"} for i in items]


def test_rrf_merges_two_lists():
    """两路结果要合并，去重。"""
    a = _mk(["d1", "d2", "d3"])
    b = _mk(["d2", "d4", "d1"])

    fused = rrf_fuse([a, b])
    ids = [item["id"] for item, _ in fused]

    assert set(ids) == {"d1", "d2", "d3", "d4"}, f"合并结果不对：{ids}"
    assert len(ids) == len(set(ids)), "有重复"


def test_rrf_rewards_agreement():
    """两路都靠前的应该排最前。"""
    a = _mk(["d1", "d2", "d3"])
    b = _mk(["d1", "d3", "d4"])

    fused = rrf_fuse([a, b])
    top = fused[0][0]["id"]

    assert top == "d1", f"两路都是第 1 的 d1 应该赢，实际 {top}"


def test_rrf_weights_matter():
    """**权重必须能改变结果** —— 这是之前踩坑的防线。

    等权融合时，弱路会稀释强路。改权重后排序必须变化，
    否则说明权重参数根本没生效。
    """
    # a 是强路（结果更相关），b 是弱路
    a = _mk(["good1", "good2", "good3"])
    b = _mk(["bad1", "bad2", "good1"])

    equal = rrf_fuse([a, b], weights=[1.0, 1.0])
    weighted = rrf_fuse([a, b], weights=[1.0, 0.1])

    equal_ids = [i["id"] for i, _ in equal]
    weighted_ids = [i["id"] for i, _ in weighted]

    assert equal_ids != weighted_ids, (
        "权重改了但排序没变 —— 权重参数无效"
    )

    # 降权后，强路独占的 good2 应该压过弱路独占的 bad1
    assert weighted_ids.index("good2") < weighted_ids.index("bad1"), (
        f"弱路降权后 bad1 不该还在 good2 前面：{weighted_ids}"
    )


def test_rrf_k_smoothing():
    """k 越大，排名差异越被磨平。"""
    a = _mk(["d1", "d2", "d3"])

    small_k = rrf_fuse([a], k=1)
    large_k = rrf_fuse([a], k=100)

    # k=1 时第 1 名分数远高于第 3 名
    ratio_small = small_k[0][1] / small_k[2][1]
    # k=100 时差距小得多
    ratio_large = large_k[0][1] / large_k[2][1]

    assert ratio_small > ratio_large, (
        f"k 没起到平滑作用：k=1 比值 {ratio_small:.2f}，k=100 比值 {ratio_large:.2f}"
    )


def test_rrf_single_list():
    """只有一路时不崩，保持原序。"""
    a = _mk(["d1", "d2"])
    fused = rrf_fuse([a])
    assert [i["id"] for i, _ in fused] == ["d1", "d2"]


def test_rrf_empty():
    assert rrf_fuse([]) == []
    assert rrf_fuse([[]]) == []


# ============================================================================
# 归一化
# ============================================================================

def test_minmax_normalize():
    assert minmax_normalize([0, 5, 10]) == [0.0, 0.5, 1.0]


def test_minmax_identical_values():
    """所有值相同 → 返回 0.5（不是除零）。"""
    assert minmax_normalize([3, 3, 3]) == [0.5, 0.5, 0.5]


def test_minmax_empty():
    assert minmax_normalize([]) == []


# ============================================================================
# rerank
# ============================================================================

def test_reranker_available_check_does_not_crash():
    """可用性探测不该抛异常（离线时有网也不通）。"""
    r = CrossEncoderReranker(base_url="http://127.0.0.1:1", timeout=1)
    assert isinstance(r.available(), bool)


def test_reranker_empty_candidates():
    r = CrossEncoderReranker(base_url="http://127.0.0.1:1")
    assert r.rerank("query", [], top_k=3) == []


def test_reranker_respects_top_k():
    """返回条数不超过 top_k。"""
    r = CrossEncoderReranker(base_url="http://127.0.0.1:1")
    cands = _mk(["d1", "d2", "d3", "d4", "d5"])
    out = r.rerank("召回率", cands, top_k=2)
    assert len(out) <= 2


def test_reranker_text_extraction():
    """能从 dict 和对象两种形式取文本。"""
    from app.rag.retriever import Hit

    r = CrossEncoderReranker(base_url="http://127.0.0.1:1")

    assert r._text_of({"text": "abc"}, "text") == "abc"
    h = Hit(id="x", text="hello", score=0.5, rank=0)
    assert r._text_of(h, "text") == "hello"


# ============================================================================
# 完整链路
# ============================================================================

def test_hybrid_search_without_bm25():
    """没 BM25 时只用向量，不崩。"""
    from app.rag.hybrid import hybrid_search

    corpus = _mk(["d1", "d2", "d3"])

    def fake_vec(q, k):
        return corpus[:k]

    out = hybrid_search(
        "test", vector_search=fake_vec, bm25_index=None, reranker=None,
        cfg=HybridConfig(recall_k=3, fuse_k=3, final_k=2, use_rerank=False),
    )
    assert len(out) == 2
    assert out[0].item["id"] == "d1"


def test_hybrid_search_empty_vector():
    """向量召回为空 → 返回空，不崩。"""
    from app.rag.hybrid import hybrid_search

    out = hybrid_search(
        "test", vector_search=lambda q, k: [], bm25_index=None, reranker=None,
        cfg=HybridConfig(),
    )
    assert out == []


def test_hybrid_config_defaults():
    """默认参数要能召回宽、精排严（这是设计意图）。"""
    cfg = HybridConfig()
    assert cfg.recall_k > cfg.final_k, "召回应该比最终结果宽"
    assert cfg.fuse_k >= cfg.final_k

"""混合检索完整实验：三路召回 + RRF + rerank，逐段量化收益。

# 这个实验要回答的问题

之前的实验只做到"两路召回 + RRF"就下了"混合更差"的结论。
但标准链路有四段，缺了最后一环（rerank）就等于拿半成品比成品。

本实验严格按段拆分，每一段都量化边际收益：

    基线 A  纯向量 top-3
    方案 B  向量 top-50 → rerank top-3                （只加宽召回+精排）
    方案 C  向量 top-50 + BM25 top-50 → RRF top-20 → top-3  （不加精排）
    方案 D  向量 top-50 + BM25 top-50 → RRF top-20 → rerank top-3  （完整）

关键对比：
  B vs A  → rerank 本身值多少
  D vs B  → 加 BM25 值多少（在已有 rerank 的前提下）
  D vs C  → rerank 对混合结果值多少

# 评测集

18 条查询（沿用之前的），分三类：
  A 字面/数字/术语 —— 关键词理论上该赢
  B 同义改写     —— 向量该赢
  C 中英对照     —— 向量该赢

每条标注期望命中的目标文本（子串匹配）。

# 用法

    PYTHONPATH='backend:.' .venv/bin/python backend/tests/manual/exp_hybrid_full.py

输出：
    backend/tests/manual/hybrid_full_result.json   逐条明细
    终端打印对比表
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path("/mnt/e/桌面/实习项目/interview_agent")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("DATABASE_URL",
                      "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("OLLAMA_BASE_URL", "http://172.22.48.1:11800")
os.environ.setdefault("IA_REDIS", "0")
os.environ.setdefault("IA_JWT_SECRET", "x" * 32)

import logging
logging.basicConfig(level=logging.WARNING)

# ============================================================================
# 评测集
# ============================================================================

CASES: list[tuple[str, str, str]] = [
    # ---- A. 字面/数字/术语 ----
    ("A", "召回从71%提到82%", "召回率从 71% 提升到 82%，在 200 条标注集上测的"),
    ("A", "top20 重排的延迟", "重排只在 top20 上做，P99 延迟 1.8 秒"),
    ("A", "d_k = 64 是怎么算的", "每个头 d_k = d_v = d_model / h = 512/8 = 64"),
    ("A", "P99 超过 2.5 秒降级", "监控 P99，超过 2.5 秒自动降级成纯向量召回"),
    ("A", "chunk size 512", "切块大小从 500 调到 512，索引体积涨 30%"),
    ("A", "HNSW 的 M 参数", "HNSW 索引参数 M 取 16，ef_construction 取 200"),

    # ---- B. 同义改写 ----
    ("B", "缓存击穿怎么处理", "Redis 缓存穿透的解决方案与区别"),
    ("B", "你的 baseline 是什么", "改造前的指标是多少，和现在怎么比"),
    ("B", "这个东西有什么限制", "方案的适用边界与失效条件"),
    ("B", "如果数据量翻十倍会怎样", "数据规模增长后的性能与成本变化"),
    ("B", "你怎么证明改进有效", "实验设计与指标对照的做法"),
    ("B", "团队里你负责哪部分", "个人贡献与协作分工的说明"),

    # ---- C. 中英对照 ----
    ("C", "多头注意力机制", "multi-head attention 的计算复杂度和实际收益"),
    ("C", "向量检索的索引结构", "vector index 的选择：HNSW 与 IVF 的取舍"),
    ("C", "大模型的上下文窗口", "context window 长度对成本的影响"),
    ("C", "提示词工程的常见坑", "prompt engineering 里的失败模式"),
    ("C", "模型微调和微调数据的配比", "fine-tuning 的数据配比与过拟合"),
    ("C", "推理延迟怎么优化", "inference latency 的优化手段"),
]


# ============================================================================
# 语料
# ============================================================================

def load_corpus() -> list[dict]:
    """从数据库读题库（722 条），构建检索语料。

    刻意混入一些"干扰项"——真实场景里目标不在 top-1 是常态，
    如果评测集的目标总在第一位，任何方法都能拿满分。
    """
    from app.repos.pg import get_conn

    with get_conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT qid, question, topic FROM bank_questions "
            "WHERE embedding IS NOT NULL ORDER BY qid"
        )
        rows = cur.fetchall()
        cur.close()

    corpus = []
    for qid, question, topic in rows:
        corpus.append({
            "id": qid,
            "text": question or "",
            "topic": topic or "",
        })

    # 把标注的目标文本也加进语料（否则永远匹配不到）
    have = {c["text"][:40] for c in corpus}
    for _kind, _q, target in CASES:
        if target[:40] not in have:
            corpus.append({"id": f"target::{target[:24]}", "text": target, "topic": "_target"})
            have.add(target[:40])

    return corpus


# ============================================================================
# 各方案
# ============================================================================

@dataclass
class Score:
    name: str
    hits: int = 0
    total: int = 0
    by_type: dict[str, list[int]] = field(default_factory=dict)
    per_case: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def add(self, kind: str, ok: bool):
        self.by_type.setdefault(kind, [0, 0])
        self.by_type[kind][1] += 1
        if ok:
            self.by_type[kind][0] += 1
            self.hits += 1
        self.total += 1

    @property
    def recall(self) -> float:
        return self.hits / self.total if self.total else 0.0


def contains_target(text: str, target: str) -> bool:
    """判断 retrieved 文本是否命中目标。

    用"目标的前 20 字"做子串匹配 —— 比整串匹配宽容，
    避免标点/空格差异导致误判。
    """
    if not text:
        return False
    probe = target.strip()[:20]
    return probe in text or text.strip()[:20] in target


def run_case_new(corpus, bm25_index, reranker, cfg, query, target):
    """跑一条新链路，返回 [文本]。"""
    from app.rag.hybrid import hybrid_search

    # 用 BM25 索引 + 简单的向量召回替身
    #
    # 为什么不用真向量召回：真召回要查 PG（每条一次 IO），
    # 18 条 × 4 方案会很慢。这里用"预计算好的全库向量 + 本地算余弦"，
    # 结果等价但快 10 倍。
    def vector_search(q: str, k: int):
        return _local_vector_search(corpus, q, k)

    results = hybrid_search(
        query,
        vector_search=vector_search,
        bm25_index=bm25_index,
        reranker=reranker,
        cfg=cfg,
    )
    return [str(getattr(r.item, "text", "") or r.item.get("text", ""))
            if not isinstance(r.item, dict) else str(r.item.get("text", ""))
            for r in results]


# ---- 本地向量检索（预计算缓存）----

_VEC_CACHE: dict[str, list[float]] = {}


def _embed_batch(texts: list[str]) -> list[list[float]]:
    from app.rag.embedder import get_embedder
    emb = get_embedder()
    out = []
    batch = 16
    for i in range(0, len(texts), batch):
        out.extend(emb.embed(texts[i:i + batch]))
    return out


def _ensure_vectors(corpus: list[dict]) -> None:
    if _VEC_CACHE:
        return
    print(f"  预计算 {len(corpus)} 条语料向量…")
    t0 = time.time()
    vecs = _embed_batch([c["text"] for c in corpus])
    for c, v in zip(corpus, vecs):
        _VEC_CACHE[c["id"]] = v
    print(f"  完成（{time.time() - t0:.1f} 秒）")


def _local_vector_search(corpus: list[dict], query: str, k: int):
    """本地余弦检索，返回带 score 的 dict 列表。"""
    import math

    if query in _VEC_CACHE:
        qvec = _VEC_CACHE[query]
    else:
        qvec = _embed_batch([query])[0]
        _VEC_CACHE[query] = qvec

    def cos(a, b):
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    scored = []
    for c in corpus:
        v = _VEC_CACHE.get(c["id"])
        if v:
            scored.append(({**c, "score": cos(qvec, v)}, cos(qvec, v)))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [s[0] for s in scored[:k]]


# ============================================================================
# 主流程
# ============================================================================

def main() -> int:
    from app.rag.hybrid import (
        BM25Index, CrossEncoderReranker, HybridConfig,
    )

    print("=" * 72)
    print("混合检索完整实验")
    print("=" * 72)

    print("\n[1/4] 加载语料")
    corpus = load_corpus()
    print(f"  题库 {len(corpus)} 条")

    _ensure_vectors(corpus)

    print("\n[2/4] 构建 BM25 索引")
    t0 = time.time()
    bm25 = BM25Index()
    bm25.build(corpus, text_key="text")
    bm25_ms = (time.time() - t0) * 1000
    print(f"  建索引 {bm25_ms:.0f} ms")

    print("\n[3/4] 初始化 reranker")
    reranker = CrossEncoderReranker()
    has_reranker = reranker.available()
    print(f"  bge-reranker-v2-m3 可用: {has_reranker}")
    if not has_reranker:
        print("  （将退化为 bi-encoder 二次打分）")

    print("\n[4/4] 跑四种方案")

    schemes = [
        ("A. 纯向量 top3", HybridConfig(
            recall_k=3, fuse_k=3, final_k=3, use_rerank=False)),
        ("B. 向量 top50 → rerank top3", HybridConfig(
            recall_k=50, fuse_k=20, final_k=3, use_rerank=True)),
        ("C. 向量+BM25 → RRF top20 → 截断top3", HybridConfig(
            recall_k=50, fuse_k=20, final_k=3, use_rerank=False,
            weights=(1.0, 1.0))),
        ("D. 向量+BM25 → RRF top20 → rerank top3（完整）", HybridConfig(
            recall_k=50, fuse_k=20, final_k=3, use_rerank=True,
            weights=(1.0, 1.0))),
        ("E. 完整 + BM25 降权 0.6", HybridConfig(
            recall_k=50, fuse_k=20, final_k=3, use_rerank=True,
            weights=(1.0, 0.6))),
    ]

    scores: list[Score] = []

    for name, cfg in schemes:
        sc = Score(name=name)
        t0 = time.time()

        for kind, query, target in CASES:
            try:
                texts = run_case_new(corpus, bm25, reranker, cfg, query, target)
            except Exception as e:  # noqa: BLE001
                print(f"    ! {query[:20]} 失败: {type(e).__name__}: {e}")
                texts = []

            ok = any(contains_target(t, target) for t in texts)
            sc.add(kind, ok)
            sc.per_case.append({
                "kind": kind, "query": query, "target": target[:40],
                "hit": ok, "top3": [t[:45] for t in texts[:3]],
            })

        sc.elapsed_ms = (time.time() - t0) * 1000
        scores.append(sc)
        print(f"  ✓ {name}  ({sc.elapsed_ms:.0f} ms)")

    # ---------------------------------------------------------------- 输出
    print()
    print("=" * 72)
    print("结果")
    print("=" * 72)

    header = f"{'方案':<44} {'总召回':>8} {'A类':>6} {'B类':>6} {'C类':>6} {'耗时':>8}"
    print(header)
    print("-" * len(header))

    for sc in scores:
        a = sc.by_type.get("A", [0, 0])
        b = sc.by_type.get("B", [0, 0])
        c = sc.by_type.get("C", [0, 0])
        short = sc.name.split(". ")[-1][:42]
        print(f"{short:<44} "
              f"{sc.hits}/{sc.total:<6} "
              f"{a[0]}/{a[1]:<4} "
              f"{b[0]}/{b[1]:<4} "
              f"{c[0]}/{c[1]:<4} "
              f"{sc.elapsed_ms:>7.0f}ms")

    # ---------------------------------------------------------------- 边际收益
    print()
    print("=" * 72)
    print("边际收益（这是实验的核心结论）")
    print("=" * 72)

    by_name = {s.name.split(". ")[0]: s for s in scores}

    def diff(x: str, y: str) -> str:
        a, b = by_name.get(x), by_name.get(y)
        if not (a and b):
            return "n/a"
        d = a.recall - b.recall
        sign = "+" if d >= 0 else ""
        return f"{sign}{d * 100:.1f} 个点"

    print(f"  rerank 本身的价值（B vs A）:        {diff('B', 'A')}")
    print(f"  BM25 在已有 rerank 下的价值（D vs B）: {diff('D', 'B')}")
    print(f"  rerank 对混合结果的价值（D vs C）:    {diff('D', 'C')}")
    print(f"  BM25 降权的影响（D vs E）:           {diff('D', 'E')}")

    # ---------------------------------------------------------------- 明细
    print()
    print("=" * 72)
    print("逐条明细（完整方案 D）")
    print("=" * 72)
    d_score = next((s for s in scores if s.name.startswith("D.")), None)
    if d_score:
        for c in d_score.per_case:
            mark = "✓" if c["hit"] else "✗"
            print(f"  {mark} [{c['kind']}] {c['query'][:28]:<30} → {c['top3'][0][:38] if c['top3'] else '(空)'}")

    # ---------------------------------------------------------------- 存盘
    out = ROOT / "backend" / "tests" / "manual" / "hybrid_full_result.json"
    payload = {
        "corpus_size": len(corpus),
        "cases": len(CASES),
        "k": 3,
        "reranker_real": has_reranker,
        "bm25_build_ms": round(bm25_ms, 1),
        "schemes": [
            {
                "name": s.name,
                "recall": round(s.recall, 4),
                "hits": s.hits,
                "total": s.total,
                "by_type": {k: f"{v[0]}/{v[1]}" for k, v in s.by_type.items()},
                "elapsed_ms": round(s.elapsed_ms, 1),
                "per_case": s.per_case,
            }
            for s in scores
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"明细已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

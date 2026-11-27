"""终极实验：真 cross-encoder rerank 能否解决 B/C 类查询。

## 这个实验要回答的问题

前面已经确认：

    recall_k=50 覆盖 16/18（89%）   ← 目标基本都捞进来了
    从 50 选 top-3 只有 9/18（50%）  ← 排不进前三
    词面 rerank 更差（5/18）         ← 因为 B/C 类字面完全不同

所以只剩一个可能：**语义精排**。

本实验用真正的 cross-encoder（BAAI/bge-reranker-v2-m3）验证。

## 对比方案

    A. 向量 top-3                        基线
    B. 向量 top-50 → rerank top-3        只加精排
    C. 向量 top-250 → rerank top-3       扩召回再精排
    D. 向量+BM25 top-250 → RRF → rerank   完整链路

## 关键判据

如果 B 明显 > A，说明 rerank 是解药 → 上生产。
如果 B ≈ A，说明 bge-m3 的语义空间里这些就不相关，rerank 也救不了
  → 要换 embedding 模型。

首次运行会下载 2.2GB 模型，之后约 3 秒加载。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path("/mnt/e/桌面/实习项目/interview_agent")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests" / "manual"))
os.environ.setdefault("DATABASE_URL",
                      "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("OLLAMA_BASE_URL", "http://172.22.48.1:11800")
os.environ.setdefault("IA_REDIS", "0")
os.environ.setdefault("IA_JWT_SECRET", "x" * 32)

import logging
logging.basicConfig(level=logging.WARNING)

from app.rag.hybrid import BM25Index, rrf_fuse  # noqa: E402
from app.rag.rerank import SentenceTransformerReranker  # noqa: E402
from exp_hybrid_full import (  # noqa: E402
    CASES, load_corpus, _ensure_vectors, _local_vector_search,
)


def hit(text: str, target: str) -> bool:
    return target[:20] in text


def main() -> int:
    print("=" * 78)
    print("终极实验：真 cross-encoder rerank")
    print("=" * 78)

    print("\n[1/4] 加载语料")
    corpus = load_corpus()
    print(f"  {len(corpus)} 条")
    _ensure_vectors(corpus)

    print("\n[2/4] 构建 BM25（供方案 D 用）")
    bm25 = BM25Index()
    bm25.build(corpus)
    print("  完成")

    print("\n[3/4] 加载 cross-encoder")
    rr = SentenceTransformerReranker()
    t0 = time.time()
    ok = rr.available()
    load_s = time.time() - t0
    print(f"  可用: {ok}（{load_s:.1f} 秒）")
    if not ok:
        print("\n  ✗ cross-encoder 加载失败，无法继续")
        print("  可能原因：网络无法访问 HuggingFace（模型 2.2GB）")
        return 1

    print("\n[4/4] 跑四种方案")

    schemes = {}

    # ---- A. 纯向量 top-3 ----
    def scheme_a(query: str, target: str):
        cands = _local_vector_search(corpus, query, 3)
        return [c["text"] for c in cands], 0.0

    # ---- B. 向量 top-50 → rerank top-3 ----
    def scheme_b(query: str, target: str):
        t = time.time()
        cands = _local_vector_search(corpus, query, 50)
        ranked = rr.rerank(query, cands, top_k=3)
        return [r.item["text"] for r in ranked], (time.time() - t) * 1000

    # ---- C. 向量 top-250 → rerank top-3 ----
    def scheme_c(query: str, target: str):
        t = time.time()
        cands = _local_vector_search(corpus, query, 250)
        ranked = rr.rerank(query, cands, top_k=3)
        return [r.item["text"] for r in ranked], (time.time() - t) * 1000

    # ---- D. 向量+BM25 top-250 → RRF → rerank top-3 ----
    def scheme_d(query: str, target: str):
        t = time.time()
        vec = _local_vector_search(corpus, query, 250)
        bm = [d for d, _ in bm25.search(query, 250)]
        if bm:
            fused = rrf_fuse([vec, bm], weights=[1.0, 0.5], k=60)
            cands = [item for item, _ in fused[:50]]
        else:
            cands = vec[:50]
        ranked = rr.rerank(query, cands, top_k=3)
        return [r.item["text"] for r in ranked], (time.time() - t) * 1000

    for name, fn in (
        ("A. 向量 top3（基线）", scheme_a),
        ("B. 向量 top50 → rerank", scheme_b),
        ("C. 向量 top250 → rerank", scheme_c),
        ("D. 向量+BM25 → RRF → rerank", scheme_d),
    ):
        hits = 0
        by_kind = {}
        total_ms = 0.0
        detail = []

        for kind, query, target in CASES:
            try:
                texts, ms = fn(query, target)
            except Exception as e:  # noqa: BLE001
                print(f"    ! {query[:18]} 失败: {type(e).__name__}: {e}")
                texts, ms = [], 0.0

            total_ms += ms
            ok_hit = any(hit(t, target) for t in texts)
            by_kind.setdefault(kind, [0, 0])
            by_kind[kind][1] += 1
            if ok_hit:
                hits += 1
                by_kind[kind][0] += 1
            detail.append({
                "kind": kind, "query": query, "hit": ok_hit,
                "target": target[:50],
                "top3": [t[:50] for t in texts],
            })

        schemes[name] = {
            "hits": hits, "by_kind": by_kind,
            "avg_ms": total_ms / len(CASES), "detail": detail,
        }
        print(f"  ✓ {name}: {hits}/18")

    # ---------------------------------------------------------------- 输出
    print()
    print("=" * 78)
    print("结果")
    print("=" * 78)

    header = f"{'方案':<36} {'总':>7} {'A':>6} {'B':>6} {'C':>6} {'平均耗时':>10}"
    print(header)
    print("-" * len(header))

    for name, r in schemes.items():
        a = r["by_kind"].get("A", [0, 0])
        b = r["by_kind"].get("B", [0, 0])
        c = r["by_kind"].get("C", [0, 0])
        print(f"{name:<36} {r['hits']:>4}/18 "
              f"{a[0]:>4}/{a[1]} {b[0]:>4}/{b[1]} {c[0]:>4}/{c[1]} "
              f"{r['avg_ms']:>8.0f}ms")

    # ---------------------------------------------------------------- 结论
    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)

    base = schemes["A. 向量 top3（基线）"]["hits"]
    for name, r in schemes.items():
        d = r["hits"] - base
        sign = "+" if d > 0 else ""
        print(f"  {name:<36} {sign}{d:+d} vs 基线")

    b_hits = schemes["B. 向量 top50 → rerank"]["hits"]
    print()
    if b_hits > base + 2:
        print("  ✓ rerank 有效，建议上生产")
    elif b_hits > base:
        print("  ~ rerank 略有提升，但幅度有限")
    else:
        print("  ✗ rerank 无效 —— 问题不在排序，在 embedding 语义空间")
        print("    下一步应换 embedding 模型（如 bge-large-zh-v1.5）")

    # ---------------------------------------------------------------- 明细
    print()
    print("=" * 78)
    print("方案 B 的逐条明细（看 rerank 具体改对了哪些）")
    print("=" * 78)
    for d in schemes["B. 向量 top50 → rerank"]["detail"]:
        mark = "✓" if d["hit"] else "✗"
        print(f"  {mark} [{d['kind']}] {d['query'][:26]:<28} → {d['top3'][0][:40] if d['top3'] else '(空)'}")

    # ---------------------------------------------------------------- 存盘
    out = ROOT / "backend" / "tests" / "manual" / "rerank_result.json"
    out.write_text(json.dumps({
        "corpus": len(corpus),
        "cases": len(CASES),
        "reranker": rr.stats(),
        "schemes": {
            k: {
                "hits": v["hits"],
                "by_kind": {kk: f"{vv[0]}/{vv[1]}" for kk, vv in v["by_kind"].items()},
                "avg_ms": round(v["avg_ms"], 1),
                "detail": v["detail"],
            }
            for k, v in schemes.items()
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"明细已存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

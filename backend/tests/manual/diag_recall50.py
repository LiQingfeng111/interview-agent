"""诊断 2：recall_k=50 能覆盖多少目标？rerank 能捞回多少？

## 核心假设

上一轮数据显示：B/C 类目标大多在 4-36 名。
如果 recall_k=50 能覆盖它们，那 rerank 就有活干。

本脚本验证：
  1. recall_k=50 的目标覆盖率（能捞进来多少）
  2. 用不同 rerank 策略能从 50 条里捞回多少到 top-3

## 三种 rerank 策略（都不需要新模型）

  ① 原始分数（基线，不排）
  ② bi-encoder 重算（query 与 doc 重新编码算余弦）
  ③ 混合分数：0.7×向量 + 0.3×BM25 归一化

如果 ③ 明显好于 ①，说明"混合 + 精排"这条链路值得上。
"""

import os
import sys
from pathlib import Path

ROOT = Path("/mnt/e/桌面/实习项目/interview_agent")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "tests" / "manual"))
os.environ.setdefault("DATABASE_URL", "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("OLLAMA_BASE_URL", "http://172.22.48.1:11800")
os.environ.setdefault("IA_REDIS", "0")
os.environ.setdefault("IA_JWT_SECRET", "x" * 32)

import logging
logging.basicConfig(level=logging.ERROR)

from app.rag.hybrid import BM25Index, minmax_normalize  # noqa: E402
from exp_hybrid_full import (  # noqa: E402
    CASES, load_corpus, _ensure_vectors, _local_vector_search,
)


def hit(text: str, target: str) -> bool:
    return target[:20] in text


def main():
    corpus = load_corpus()
    print(f"语料 {len(corpus)} 条")
    _ensure_vectors(corpus)

    bm25 = BM25Index()
    bm25.build(corpus)

    print()
    print("=" * 76)
    print("阶段 1：recall_k=50 的目标覆盖率（能捞进来多少）")
    print("=" * 76)

    covered = 0
    for kind, query, target in CASES:
        vec50 = _local_vector_search(corpus, query, 50)
        bm50 = [d for d, _ in bm25.search(query, 50)]

        in_vec = any(hit(c["text"], target) for c in vec50)
        in_bm = any(hit(c["text"], target) for c in bm50)
        in_union = in_vec or in_bm

        if in_union:
            covered += 1

        mark = "✓" if in_union else "✗"
        print(f"  {mark} [{kind}] {query[:24]:<26} vec={in_vec} bm25={in_bm}")

    print()
    print(f"  覆盖率: {covered}/18 = {covered / 18 * 100:.0f}%")

    print()
    print("=" * 76)
    print("阶段 2：从 50 条里选 top-3，三种策略对比")
    print("=" * 76)

    strategies = {
        "① 原始向量分": lambda q, cands: [c.get("score", 0) for c in cands],
    }

    # ② 混合分数：向量 + BM25 归一化
    def mix_score(q, cands):
        """0.7×向量 + 0.3×BM25。

        注意：BM25 分数要归一化 —— 它是无界的，不能直接和余弦相加。
        """
        qv = [c.get("score", 0) for c in cands]

        # 对每条候选单独算 BM25 分
        bm_scores = []
        for c in cands:
            from app.rag.hybrid import tokenize_zh
            qtok = set(tokenize_zh(q))
            dtok = set(tokenize_zh(c["text"]))
            # 简化的重叠度（真 BM25 需要全库统计，这里用局部重叠近似）
            inter = len(qtok & dtok)
            bm_scores.append(inter / max(1, len(qtok)))

        qv_n = minmax_normalize(qv)
        bm_n = minmax_normalize(bm_scores)
        return [0.7 * a + 0.3 * b for a, b in zip(qv_n, bm_n)]

    strategies["② 混合分数 0.7vec+0.3bm"] = mix_score

    results = {}
    for name, scorer in strategies.items():
        hits = 0
        detail = []
        for kind, query, target in CASES:
            cands = _local_vector_search(corpus, query, 50)
            if not cands:
                detail.append((kind, query, False))
                continue

            scores = scorer(query, cands)
            ranked = sorted(zip(cands, scores), key=lambda x: x[1], reverse=True)
            top3 = [c for c, _ in ranked[:3]]

            ok = any(hit(c["text"], target) for c in top3)
            if ok:
                hits += 1
            detail.append((kind, query, ok))

        results[name] = (hits, detail)
        print(f"\n{name}: {hits}/18 = {hits / 18 * 100:.0f}%")
        for kind, query, ok in detail:
            if not ok:
                print(f"    ✗ [{kind}] {query[:34]}")

    print()
    print("=" * 76)
    print("结论")
    print("=" * 76)
    base = results["① 原始向量分"][0]
    for name, (h, _) in results.items():
        d = h - base
        sign = "+" if d >= 0 else ""
        print(f"  {name:<32} {h}/18  ({sign}{d} vs 基线)")


if __name__ == "__main__":
    main()

"""最终实验：真 cross-encoder rerank 能否解决 B/C 类查询。

## 优化点（相比 exp_rerank_final.py）

1. **候选只取 top-30**（不是 250）—— CPU 上 rerank 30 条约 2-4 秒/查询，
   18 条就是 40-70 秒。250 条要 5 分钟以上，不现实。
   而实测 recall_k=30 已覆盖 83%，够验证方向。

2. **缓存向量**，避免重复计算。

3. **分段输出**，每跑完一条就打印，不会因为超时看不到任何结果。

## 对比

    A. 向量 top-3（基线）
    B. 向量 top-30 → rerank top-3

如果 B > A，说明 rerank 是解药。
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
logging.basicConfig(level=logging.ERROR)

from exp_hybrid_full import (  # noqa: E402
    CASES, load_corpus, _ensure_vectors, _local_vector_search,
)


def hit(text: str, target: str) -> bool:
    return target[:20] in text


def main() -> int:
    print("=" * 72, flush=True)
    print("最终实验：真 cross-encoder rerank", flush=True)
    print("=" * 72, flush=True)

    print("\n[1/4] 语料", flush=True)
    corpus = load_corpus()
    print(f"  {len(corpus)} 条", flush=True)

    print("  ���计算向量…", flush=True)
    t0 = time.time()
    _ensure_vectors(corpus)
    print(f"  完成（{time.time() - t0:.0f} 秒）", flush=True)

    print("\n[2/4] 加载 cross-encoder", flush=True)
    print("  （568M 模型，CPU 加载约 1-3 分钟）", flush=True)
    t0 = time.time()

    try:
        from sentence_transformers import CrossEncoder

        model = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
        print(f"  ✓ 加载完成（{time.time() - t0:.0f} 秒）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ 失败: {type(e).__name__}: {e}", flush=True)
        return 1

    # ---- 快速验证：能区分吗 ----
    print("\n[3/4] 区分度自检", flush=True)
    probe = [
        ("你的 baseline 是什么", "改造前的指标是多少，和现在怎么比"),
        ("你的 baseline 是什么", "今天天气不错适合散步"),
    ]
    ps = model.predict(probe, show_progress_bar=False)
    print(f"  语义相关: {float(ps[0]):.4f}", flush=True)
    print(f"  不相关:   {float(ps[1]):.4f}", flush=True)
    if float(ps[0]) <= float(ps[1]):
        print("  ! 模型区分度异常，结果可能不可信", flush=True)
    else:
        print("  ✓ 区分度正常", flush=True)

    # ---- 跑对比 ----
    print("\n[4/4] 对比 A（基线）vs B（rerank）", flush=True)
    print(flush=True)

    base_hits = 0
    rerank_hits = 0
    detail = []

    for i, (kind, query, target) in enumerate(CASES, 1):
        t0 = time.time()

        # A. 向量 top-3
        a_cands = _local_vector_search(corpus, query, 3)
        a_ok = any(hit(c["text"], target) for c in a_cands)

        # B. 向量 top-30 → rerank top-3
        b_cands = _local_vector_search(corpus, query, 30)
        b_top3 = []
        if b_cands:
            scores = model.predict(
                [(query, c["text"]) for c in b_cands],
                show_progress_bar=False,
            )
            order = sorted(range(len(b_cands)),
                           key=lambda j: float(scores[j]), reverse=True)
            b_top3 = [b_cands[j] for j in order[:3]]
        b_ok = any(hit(c["text"], target) for c in b_top3)

        if a_ok:
            base_hits += 1
        if b_ok:
            rerank_hits += 1

        ms = (time.time() - t0) * 1000
        changed = "→" if a_ok != b_ok else " "
        mark_a = "✓" if a_ok else "✗"
        mark_b = "✓" if b_ok else "✗"
        print(f"  {i:>2}. [{kind}] {query[:22]:<24} A{mark_a} {changed} B{mark_b}  ({ms:.0f}ms)",
              flush=True)

        detail.append({
            "kind": kind, "query": query, "target": target[:50],
            "a_hit": a_ok, "b_hit": b_ok,
            "b_top1": b_top3[0]["text"][:50] if b_top3 else "",
        })

    # ---- 汇总 ----
    print()
    print("=" * 72)
    print("结果")
    print("=" * 72)
    print(f"  A. 向量 top-3（基线）      {base_hits}/18 = {base_hits / 18 * 100:.0f}%")
    print(f"  B. 向量 top-30 → rerank    {rerank_hits}/18 = {rerank_hits / 18 * 100:.0f}%")

    d = rerank_hits - base_hits
    sign = "+" if d > 0 else ""
    print(f"\n  边际收益: {sign}{d} 个点")

    # 分类看
    print()
    print("  按类型：")
    for kind in ("A", "B", "C"):
        sub = [x for x in detail if x["kind"] == kind]
        a_n = sum(1 for x in sub if x["a_hit"])
        b_n = sum(1 for x in sub if x["b_hit"])
        print(f"    {kind} 类:  A {a_n}/{len(sub)}  →  B {b_n}/{len(sub)}")

    # 结论
    print()
    print("=" * 72)
    print("结论")
    print("=" * 72)
    if d >= 3:
        print("  ✓ rerank 显著有效，建议上生产")
        print("    下一步：把 rerank 接进 bank_select，评估端到端延迟")
    elif d > 0:
        print("  ~ rerank 有效但幅度有限")
        print("    建议再试更小的模型（bge-reranker-base，快 2 倍）看性价比")
    else:
        print("  ✗ rerank 无效")
        print("    说明 bge-m3 的语义空间里这些就是不相关，精排救不了")
        print("    下一步：换 embedding 模型（bge-large-zh-v1.5）")

    # 存盘
    out = ROOT / "backend" / "tests" / "manual" / "rerank_final_result.json"
    out.write_text(json.dumps({
        "corpus": len(corpus),
        "cases": len(CASES),
        "model": "BAAI/bge-reranker-v2-m3",
        "baseline_hits": base_hits,
        "rerank_hits": rerank_hits,
        "delta": d,
        "detail": detail,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  明细已存: {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

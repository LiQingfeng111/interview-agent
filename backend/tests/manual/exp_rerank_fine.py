"""用小模型验证 rerank 是否有效。

## 为什么换小模型

网络实测 183 KB/s，bge-reranker-v2-m3（2.2GB）要 3.4 小时，不现实。

`BAAI/bge-reranker-base`：
  · 参数 278M（vs 568M），体积约 1.1GB
  · 下载约 1.7 小时 —— 仍然久，但只测方向够用
  · 精度略低，但如果连它都能提升，"精排有效"这个结论就成立

## 更快的替代：交叉编码器换成向量相似度的细粒度版本

如果模型实在下不来，还有一个零下载的方案：
用**已有的 bge-m3** 做"分块精排"——把候选文本切成句子，
算 query 与每个句子的最大相似度。这能捕捉"query 只匹配某一句"的情况，
比整段平均更细。

本脚本两种都试，取能跑通的那个。
"""

from __future__ import annotations

import json
import math
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


def cos(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def fine_grained_rerank(query: str, cands: list, embedder, top_k: int = 3):
    """细粒度精排：把候选切成句子，算 query 与最相似句子的分数。

    ## 为什么这可能有效

    召回时是"整段 vs 整段"算余弦。如果候选很长（题库里有的题目 100+ 字），
    query 只匹配其中一句，相似度会被其他句子"平均掉"，导致排名靠后。

    细粒度版本：max(sim(query, sentence_i)) —— 不被无关句子稀释。

    这和 cross-encoder 的差别：没有词级交互，但比整段平均细。
    零下载成本，用现成的 bge-m3。
    """
    # 切句：按中文标点
    def split_sents(t: str) -> list[str]:
        import re
        parts = re.split(r"[。！？；\n]", t)
        return [p.strip() for p in parts if len(p.strip()) >= 4]

    # 收集所有句子
    all_sents = []
    owner = []
    for i, c in enumerate(cands):
        for s in split_sents(c["text"]):
            all_sents.append(s)
            owner.append(i)

    if not all_sents:
        return cands[:top_k]

    # 一次批量编码
    vecs = embedder.embed([query] + all_sents)
    qv = vecs[0]
    svs = vecs[1:]

    # 每个候选取最大句相似度
    best = {}
    for i, sv in zip(owner, svs):
        s = cos(qv, sv)
        if s > best.get(i, -1):
            best[i] = s

    # 没句子的候选沿用原分
    scored = []
    for i, c in enumerate(cands):
        scored.append((c, best.get(i, c.get("score", 0))))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [c for c, _ in scored[:top_k]]


def main() -> int:
    print("=" * 72, flush=True)
    print("精排验证（零下载方案：细粒度向量精排）", flush=True)
    print("=" * 72, flush=True)

    corpus = load_corpus()
    print(f"\n语料 {len(corpus)} 条", flush=True)
    _ensure_vectors(corpus)

    from app.rag.embedder import get_embedder
    emb = get_embedder()

    print("\n对比：", flush=True)
    print("  A. 向量 top-3（基线）", flush=True)
    print("  B. 向量 top-30 → 细粒度精排 top-3", flush=True)
    print()

    base_hits = 0
    fine_hits = 0
    detail = []

    for i, (kind, query, target) in enumerate(CASES, 1):
        t0 = time.time()

        # A. 基线
        a = _local_vector_search(corpus, query, 3)
        a_ok = any(hit(c["text"], target) for c in a)

        # B. 宽召回 + 细粒度精排
        wide = _local_vector_search(corpus, query, 30)
        b = fine_grained_rerank(query, wide, emb, top_k=3)
        b_ok = any(hit(c["text"], target) for c in b)

        if a_ok:
            base_hits += 1
        if b_ok:
            fine_hits += 1

        ms = (time.time() - t0) * 1000
        changed = "⬆" if (b_ok and not a_ok) else ("⬇" if (a_ok and not b_ok) else " ")
        print(f"  {i:>2}. [{kind}] {query[:20]:<22} A{'✓' if a_ok else '✗'} {changed} "
              f"B{'✓' if b_ok else '✗'}  ({ms:.0f}ms)", flush=True)

        detail.append({
            "kind": kind, "query": query, "target": target[:50],
            "a_hit": a_ok, "b_hit": b_ok,
            "b_top1": b[0]["text"][:50] if b else "",
        })

    print()
    print("=" * 72)
    print("结果")
    print("=" * 72)
    print(f"  A. 向量 top-3（基线）              {base_hits}/18 = {base_hits / 18 * 100:.0f}%")
    print(f"  B. 向量 top-30 → 细粒度精排         {fine_hits}/18 = {fine_hits / 18 * 100:.0f}%")

    d = fine_hits - base_hits
    print(f"\n  边际收益: {'+' if d > 0 else ''}{d} 个点")

    print()
    print("  按类型：", flush=True)
    for kind in ("A", "B", "C"):
        sub = [x for x in detail if x["kind"] == kind]
        a_n = sum(1 for x in sub if x["a_hit"])
        b_n = sum(1 for x in sub if x["b_hit"])
        print(f"    {kind}: A {a_n}/{len(sub)} → B {b_n}/{len(sub)}")

    # 看提升和退步的具体案例
    up = [x for x in detail if x["b_hit"] and not x["a_hit"]]
    down = [x for x in detail if x["a_hit"] and not x["b_hit"]]

    if up:
        print()
        print(f"  提升的 {len(up)} 条：")
        for x in up:
            print(f"    ⬆ [{x['kind']}] {x['query'][:24]}")
            print(f"       新 top1: {x['b_top1'][:44]}")
    if down:
        print()
        print(f"  退步的 {len(down)} 条：")
        for x in down:
            print(f"    ⬇ [{x['kind']}] {x['query'][:24]}")

    print()
    print("=" * 72)
    print("结论")
    print("=" * 72)
    if d >= 2:
        print("  ✓ 精排有效（即使是零下载的近似方案）")
        print("    真 cross-encoder 应该更好，值得等模型下完再测")
    elif d > 0:
        print("  ~ 略有提升，幅度有限")
    else:
        print("  ✗ 精排无效")
        print("    说明问题在 embedding 语义空间，要换模型而不是加重排")

    out = ROOT / "backend" / "tests" / "manual" / "rerank_fine_result.json"
    out.write_text(json.dumps({
        "corpus": len(corpus),
        "method": "fine-grained embedding rerank (zero download)",
        "baseline_hits": base_hits,
        "rerank_hits": fine_hits,
        "delta": d,
        "detail": detail,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  明细: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

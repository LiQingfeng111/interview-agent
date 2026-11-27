"""验证 rerank 能否把"已召回但排名靠后"的目标提到 top-3。

## 为什么这个实验最关键

上一轮数据：
    recall_k=50 覆盖 16/18（89%）   ← 目标基本都捞进来了
    从 50 选 top-3 只有 9/18（50%）  ← 但排不进前三

**瓶颈不在召回，在精排。** 所以 rerank 是这条链路的核心价值。

## 为什么不用真 cross-encoder

torch 还在装（200MB+）。先用三种**纯 Python 的近似 rerank** 验证方向，
如果近似方案能提升，真模型只会更好。

## 三种近似 rerank

  ① IDF 加权词重叠（模拟 BM25 的"稀有词更重要"）
  ② 位置加权（query 的词出现在文档开头权重大）
  ③ 短语匹配奖励（query 的连续词在文档里也连续）

这三种都是 cross-encoder 想要捕捉的信号。如果它们有用，
说明"精排"这个方向对；如果全都没用，说明问题在别处。
"""

import math
import os
import sys
from collections import Counter
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

from app.rag.hybrid import tokenize_zh  # noqa: E402
from exp_hybrid_full import (  # noqa: E402
    CASES, load_corpus, _ensure_vectors, _local_vector_search,
)


def hit(text: str, target: str) -> bool:
    return target[:20] in text


class RerankScores:
    """三种近似 rerank 打分器。"""

    def __init__(self, corpus: list[dict]):
        # 预计算 IDF：越少见的词越有区分度
        self.idf: dict[str, float] = {}
        n = len(corpus)
        df: Counter = Counter()
        for c in corpus:
            for t in set(tokenize_zh(c["text"])):
                df[t] += 1
        for term, cnt in df.items():
            self.idf[term] = math.log((n + 1) / (cnt + 1)) + 1.0

    # ---- ① IDF 加权重叠 ----
    def idf_overlap(self, query: str, text: str) -> float:
        """query 的词在文档里出现，按 IDF 加权。

        这是 cross-encoder 最基础的能力：判断"关键词是否命中"。
        比纯向量强的地方：向量会把"baseline"和"改造前指标"平均成中等相似度，
        而 IDF 能明确知道"这两个词没有共同 token"。
        """
        qtok = set(tokenize_zh(query))
        dtok = set(tokenize_zh(text))
        if not qtok:
            return 0.0

        total = sum(self.idf.get(t, 1.0) for t in qtok)
        if total <= 0:
            return 0.0
        matched = sum(self.idf.get(t, 1.0) for t in qtok if t in dtok)
        return matched / total

    # ---- ② 位置加权 ----
    def position_weighted(self, query: str, text: str) -> float:
        """query 的词出现在文档越靠前，分越高。

        面试题的问句结构通常是"主题词 + 展开"，主题词在前面。
        """
        qtok = [t for t in tokenize_zh(query) if t]
        if not qtok:
            return 0.0

        dtoks = tokenize_zh(text)
        if not dtoks:
            return 0.0

        score = 0.0
        for t in qtok:
            for pos, dt in enumerate(dtoks):
                if dt == t:
                    # 越靠前权重越高（1.0 到 0.3 线性衰减）
                    w = max(0.3, 1.0 - pos / max(1, len(dtoks)))
                    score += w
                    break
        return score / len(qtok)

    # ---- ③ 短语匹配 ----
    def phrase_bonus(self, query: str, text: str) -> float:
        """query 的连续 2-3 词在文档里也连续出现 → 加分。

        这是 cross-encoder 的核心优势：它能看到词序。
        双塔模型把整句压成一个向量，词序信息会丢。
        """
        qtoks = tokenize_zh(query)
        dtoks = tokenize_zh(text)
        if len(qtoks) < 2 or len(dtoks) < 2:
            return 0.0

        dtext = " ".join(dtoks)
        hits = 0
        total = 0
        for n in (3, 2):
            for i in range(len(qtoks) - n + 1):
                gram = " ".join(qtoks[i:i + n])
                total += 1
                if gram in dtext:
                    hits += 1
        return hits / max(1, total)


def main():
    corpus = load_corpus()
    print(f"语料 {len(corpus)} 条")
    _ensure_vectors(corpus)

    rs = RerankScores(corpus)

    print()
    print("=" * 78)
    print("从 50 条候选里选 top-3：各种 rerank 策略对比")
    print("=" * 78)

    # 策略：(名字, 打分函数)  打分函数接收 (query, text, 原始向量分)
    strategies = [
        ("① 原始向量分（基线）", lambda q, t, v: v),
        ("② IDF 加权重叠", lambda q, t, v: rs.idf_overlap(q, t)),
        ("③ IDF + 位置", lambda q, t, v: 0.7 * rs.idf_overlap(q, t)
                                           + 0.3 * rs.position_weighted(q, t)),
        ("④ IDF + 位置 + 短语", lambda q, t, v: 0.5 * rs.idf_overlap(q, t)
                                                + 0.25 * rs.position_weighted(q, t)
                                                + 0.25 * rs.phrase_bonus(q, t)),
        ("⑤ 向量 0.3 + IDF 0.7", lambda q, t, v: 0.3 * v + 0.7 * rs.idf_overlap(q, t)),
        ("⑥ 向量 0.4 + IDF 0.35 + 短语 0.25",
         lambda q, t, v: 0.4 * v + 0.35 * rs.idf_overlap(q, t)
                         + 0.25 * rs.phrase_bonus(q, t)),
    ]

    all_results = {}

    for name, scorer in strategies:
        hits = 0
        by_kind = {}
        fails = []

        for kind, query, target in CASES:
            cands = _local_vector_search(corpus, query, 50)
            if not cands:
                continue

            scored = [
                (c, scorer(query, c["text"], c.get("score", 0)))
                for c in cands
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            top3 = [c for c, _ in scored[:3]]

            ok = any(hit(c["text"], target) for c in top3)
            by_kind.setdefault(kind, [0, 0])
            by_kind[kind][1] += 1
            if ok:
                hits += 1
                by_kind[kind][0] += 1
            else:
                fails.append((kind, query))

        all_results[name] = (hits, by_kind, fails)

    # 输出表
    header = f"{'策略':<38} {'总':>6} {'A':>5} {'B':>5} {'C':>5}"
    print(header)
    print("-" * len(header))
    for name, (h, bk, _) in all_results.items():
        a = bk.get("A", [0, 0])
        b = bk.get("B", [0, 0])
        c = bk.get("C", [0, 0])
        print(f"{name:<38} {h:>3}/18 {a[0]:>3}/{a[1]} {b[0]:>3}/{b[1]} {c[0]:>3}/{c[1]}")

    # 边际收益
    print()
    print("=" * 78)
    print("边际收益")
    print("=" * 78)
    base = all_results["① 原始向量分（基线）"][0]
    for name, (h, _, _) in all_results.items():
        d = h - base
        sign = "+" if d > 0 else ""
        flag = " ← 最优" if h == max(v[0] for v in all_results.values()) else ""
        print(f"  {name:<38} {sign}{d:+d} vs 基线 ({h}/18){flag}")

    # 最优策略还差哪些
    best_name = max(all_results, key=lambda k: all_results[k][0])
    best_hits, _, best_fails = all_results[best_name]
    if best_fails:
        print()
        print(f"  最优策略（{best_name}）仍失败的:")
        for kind, q in best_fails:
            print(f"    ✗ [{kind}] {q}")


if __name__ == "__main__":
    main()

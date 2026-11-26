"""混合检索第二轮：分查询类型测，找出"什么时候该用关键词"。

## 第一轮为什么得出"混合更差"

上一轮 RRF 等权融合，结果：

    vector    正样本 3/5 = 0.60   负样本误召 0/4
    keyword   正样本 1/5 = 0.20   负样本误召 1/4
    rrf       正样本 1/5 = 0.20   负样本误召 0/4

keyword 本身召回只有 0.20，把它和 vector 等权融合，等于用弱信号污染强信号。
**这是我的融合方式错了，不是"混合检索"错了。**

RRF 的前提是两个检索器"各有擅长"；如果一方普遍很差，等权融合必然变差。

## 第二轮：按查询类型分开测

真正该分场景：

| 查询类型 | 例子 | 谁应该赢 |
|---|---|---|
| **字面/数字/术语** | "71%"、"top20"、"P99"、"d_k=64" | **关键词**（向量对数字不敏感） |
| **同义改写** | "缓存击穿" vs "缓存穿透" | **向量** |
| **中英对照** | "多头注意力" vs "multi-head" | **向量** |

如果这个假设成立，正确做法是**按查询特征动态加权**，而不是等权 RRF。

## 这一轮要回答的三个问题

1. 数字/术语类查询，关键词真的赢吗？
2. 同义改写类查询，向量真的赢吗？
3. 动态加权（有数字/术语时提高关键词权重）是否优于任何单一方法？
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

OLLAMA = "http://172.22.48.1:11800"
ROOT = Path(__file__).resolve().parents[1]
RRF_K = 60

# ---------------------------------------------------------------- 测试集
# 每类 6 条，共 18 条。query -> 期望命中的目标文本
CASES = [
    # ---- A. 字面/数字/术语：关键词应赢 ----
    ("A", "召回从71%提到82%", "召回率从 71% 提升到 82%，在 200 条标注集上测的"),
    ("A", "top20 重排的延迟", "重排只在 top20 上做，P99 延迟 1.8 秒"),
    ("A", "d_k = 64 是怎么算的", "每个头 d_k = d_v = d_model / h = 512/8 = 64"),
    ("A", "P99 超过 2.5 秒降级", "监控 P99，超过 2.5 秒自动降级成纯向量召回"),
    ("A", "chunk size 512", "切块大小从 500 调到 512，索引体积涨 30%"),
    ("A", "HNSW 的 M 参数", "HNSW 索引参数 M 取 16，ef_construction 取 200"),

    # ---- B. 同义改写：向量应赢 ----
    ("B", "缓存击穿怎么处理", "Redis 缓存穿透的解决方案与区别"),
    ("B", "你的 baseline 是什么", "改造前的指标是多少，和现在怎么比"),
    ("B", "失败方案有哪些", "试过但没走通的路线及其原因"),
    ("B", "如何证明效果变好了", "用什么指标判断系统改进是否真实"),
    ("B", "数据量翻十倍会先崩在哪", "规模扩大十倍的瓶颈定位"),
    ("B", "这套设计的代价是什么", "为此付出的复杂度与维护成本的取舍"),

    # ---- C. 中英对照：向量应赢 ----
    ("C", "多头注意力每个头维度", "multi-head attention with d_k = 64 per head"),
    ("C", "向量检索的召回率", "recall of the vector retrieval stage"),
    ("C", "重排模型选型", "cross-encoder reranker selection"),
    ("C", "分块策略", "chunking strategy for long documents"),
    ("C", "评估集怎么构建", "how to build the evaluation set"),
    ("C", "提示词版本管理", "prompt versioning and A/B testing"),
]

# 干扰项：真实题库里抽的，与测试目标无关
DISTRACTORS = [
    "进程和线程的区别是什么，什么时候用多进程",
    "TCP 三次握手为什么不能是两次",
    "数据库事务的 ACID 分别指什么",
    "什么情况下索引会失效",
    "Redis 持久化 RDB 和 AOF 的区别",
    "消息队列如何保证不丢消息",
    "微服务拆分的边界怎么定",
    "如何设计一个短链接系统",
    "限流算法有哪几种，各自适用场景",
    "分布式锁的实现方式有哪些",
    "MySQL 的 MVCC 是怎么实现的",
    "为什么 Redis 单线程还这么快",
    "容器和虚拟机的区别",
    "如何排查 CPU 100% 的问题",
    "HTTPS 的握手过程",
    "什么是 B+ 树，为什么数据库用它",
]


def embed(texts: list[str]) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), 16):
        batch = texts[i:i + 16]
        req = urllib.request.Request(
            OLLAMA + "/api/embed",
            data=json.dumps({"model": "bge-m3", "input": batch}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            out.extend(json.loads(r.read())["embeddings"])
    return out


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def norm(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;（）()\-—…\"'“”]", "", s or "")


def grams(s: str) -> set[str]:
    n = norm(s)
    return {n[i:i + 2] for i in range(len(n) - 1)} if len(n) >= 2 else ({n} if n else set())


def jaccard(a: str, b: str) -> float:
    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


HAS_LITERAL = re.compile(r"\d|[A-Za-z]{2,}|[%％]|[A-Z]{2,}")


def has_literal_token(q: str) -> bool:
    """查询里有没有数字/英文/符号这类"字面信号"。"""
    return bool(HAS_LITERAL.search(q))


def main() -> int:
    targets = [t for _, _, t in CASES]
    corpus = targets + DISTRACTORS
    print(f"语料 {len(corpus)} 条（目标 {len(targets)} + 干扰 {len(DISTRACTORS)}）")
    print(f"查询 {len(CASES)} 条（A 字面 {(sum(1 for c in CASES if c[0]=='A'))} / "
          f"B 同义 {(sum(1 for c in CASES if c[0]=='B'))} / "
          f"C 中英 {(sum(1 for c in CASES if c[0]=='C'))}）")

    cvecs = embed(corpus)
    qvecs = embed([q for _, q, _ in CASES])

    K = 3
    stats = {t: {"vector": 0, "keyword": 0, "rrf": 0, "adaptive": 0, "total": 0}
             for t in ("A", "B", "C")}

    print()
    print(f"{'类':<3}{'查询':<26}{'向量':<6}{'关键词':<7}{'RRF':<6}{'自适应':<7}{'字面?'}")
    print("-" * 72)

    for i, (typ, query, target) in enumerate(CASES):
        tgt_idx = corpus.index(target)
        vs = [(j, cosine(qvecs[i], cvecs[j])) for j in range(len(corpus))]
        vs.sort(key=lambda x: -x[1])
        vrank = {j: r for r, (j, _) in enumerate(vs)}

        ks = [(j, jaccard(query, corpus[j])) for j in range(len(corpus))]
        ks.sort(key=lambda x: -x[1])
        krank = {j: r for r, (j, _) in enumerate(ks)}

        # 标准 RRF（等权）
        fused = {j: 1 / (RRF_K + vrank[j]) + 1 / (RRF_K + krank[j]) for j in range(len(corpus))}
        frank = sorted(fused, key=lambda j: -fused[j])

        # 自适应加权：查询含字面信号 → 关键词权重 0.6；否则 0.2
        lit = has_literal_token(query)
        kw_w = 0.6 if lit else 0.2
        vec_w = 1.0 - kw_w
        adaptive = {j: vec_w / (RRF_K + vrank[j]) + kw_w / (RRF_K + krank[j])
                    for j in range(len(corpus))}
        arank = sorted(adaptive, key=lambda j: -adaptive[j])

        res = {
            "vector": vrank[tgt_idx] < K,
            "keyword": krank[tgt_idx] < K,
            "rrf": frank.index(tgt_idx) < K,
            "adaptive": arank.index(tgt_idx) < K,
        }
        for m, ok in res.items():
            if ok:
                stats[typ][m] += 1
        stats[typ]["total"] += 1

        mark = lambda b: "✓" if b else "·"  # noqa: E731
        print(f"{typ:<3}{query[:24]:<26}{mark(res['vector']):<6}{mark(res['keyword']):<7}"
              f"{mark(res['rrf']):<6}{mark(res['adaptive']):<7}{'是' if lit else '否'}")

    print()
    print(f"=== recall@{K} 分类统计 ===")
    print(f"{'类型':<22}{'向量':<8}{'关键词':<9}{'RRF':<8}{'自适应'}")
    for typ, label in (("A", "A 字面/数字/术语"), ("B", "B 同义改写"), ("C", "C 中英对照")):
        s = stats[typ]
        n = s["total"]
        print(f"{label:<22}"
              f"{s['vector']}/{n}    {s['keyword']}/{n}     {s['rrf']}/{n}    {s['adaptive']}/{n}")

    total = sum(s["total"] for s in stats.values())
    print(f"{'合计':<22}"
          f"{sum(s['vector'] for s in stats.values())}/{total}    "
          f"{sum(s['keyword'] for s in stats.values())}/{total}     "
          f"{sum(s['rrf'] for s in stats.values())}/{total}    "
          f"{sum(s['adaptive'] for s in stats.values())}/{total}")

    print()
    print("=== 结论 ===")
    a, b = stats["A"], stats["B"]
    if a["keyword"] > a["vector"]:
        print(f"1. 字面/数字类：关键词 {a['keyword']}/{a['total']} > 向量 {a['vector']}/{a['total']} → 证实假设")
    else:
        print(f"1. 字面/数字类：关键词 {a['keyword']}/{a['total']} vs 向量 {a['vector']}/{a['total']} → 未证实")
    if b["vector"] >= b["keyword"]:
        print(f"2. 同义改写类：向量 {b['vector']}/{b['total']} ≥ 关键词 {b['keyword']}/{b['total']} → 证实假设")
    else:
        print(f"2. 同义改写类：向量 {b['vector']}/{b['total']} vs 关键词 {b['keyword']}/{b['total']} → 未证实")

    best = max(("vector", "keyword", "rrf", "adaptive"),
               key=lambda m: sum(s[m] for s in stats.values()))
    print(f"3. 综合最优：{best}"
          f"（{sum(s[best] for s in stats.values())}/{total}）")

    out = ROOT / "tools" / "retrieval_round2.json"
    out.write_text(json.dumps({"stats": stats, "cases": len(CASES), "k": K},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

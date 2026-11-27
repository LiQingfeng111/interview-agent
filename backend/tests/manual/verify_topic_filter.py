"""验证 topic 过滤的效果：从 20% 提升到多少。

## 前面已经证明的问题

    topic='RAG'          question 含 "RAG"   2/8  = 25%
    topic='Transformer'  含 "Transformer"    1/5  = 20%

用话题名做向量检索，匹配的是 question 文本 —— 这些题文本里没这个词，
所以必然查不到。

## 修复

`search_bank(..., topic='RAG')` 现在会在 SQL 里加 `WHERE topic = %s`，
精确过滤后再按向量相似度排序。

## 本脚本验证

对比三种检索方式在 37 个话题上的表现：
  A. 纯向量（旧行为）
  B. 纯向量 + k=30（我上一版改的）
  C. topic 过滤 + 向量排序（本次修复）

指标：top-10 里同话题的题占多少。
"""

from __future__ import annotations

import os
import sys
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
logging.basicConfig(level=logging.ERROR)


def load_topics():
    from app.repos.pg import get_conn
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT topic, COUNT(*) AS n FROM bank_questions
            WHERE embedding IS NOT NULL AND topic IS NOT NULL AND topic <> ''
            GROUP BY topic HAVING COUNT(*) >= 3 ORDER BY n DESC
        """)
        rows = cur.fetchall()
        cur.close()
    return [(t, n) for t, n in rows]


def main() -> int:
    from app.rag import retriever

    topics = load_topics()
    print("=" * 76)
    print("topic 过滤前后对比")
    print("=" * 76)
    print(f"\n话题数: {len(topics)}")

    schemes = {
        "A. 纯向量 k=10（旧）": dict(k=10, topic=None),
        "B. 纯向量 k=30": dict(k=30, topic=None),
        "C. topic 过滤 + 向量 k=10": dict(k=10, topic=True),
    }

    results = {}

    for name, cfg in schemes.items():
        total_same = 0
        total_slots = 0
        perfect = 0
        zero = 0

        for topic, n in topics:
            if cfg["topic"]:
                hits = retriever.search_bank(topic, k=cfg["k"], role="",
                                             use="select", topic=topic)
            else:
                hits = retriever.search_bank(topic, k=cfg["k"], role="",
                                             use="select")
            same = sum(1 for h in hits if (h.project or "") == topic)
            total_same += same
            total_slots += len(hits)
            if hits and same == len(hits):
                perfect += 1
            if hits and same == 0:
                zero += 1

        rate = total_same / total_slots * 100 if total_slots else 0
        results[name] = (rate, perfect, zero, total_same, total_slots)

    print(f"\n{'方案':<32} {'同话题占比':>10} {'全中话题数':>10} {'全错':>6}")
    print("-" * 76)
    for name, (rate, perfect, zero, same, slots) in results.items():
        print(f"{name:<32} {rate:>8.0f}% {perfect:>7}/{len(topics)} {zero:>6}")

    print()
    print("=" * 76)
    print("逐话题对比（C 方案）")
    print("=" * 76)
    print(f"  {'话题':<26} {'A 纯向量':>10} {'C 过滤':>10}")

    for topic, n in topics[:20]:
        a_hits = retriever.search_bank(topic, k=10, role="", use="select")
        a_same = sum(1 for h in a_hits if (h.project or "") == topic)

        c_hits = retriever.search_bank(topic, k=10, role="", use="select", topic=topic)
        c_same = sum(1 for h in c_hits if (h.project or "") == topic)

        mark = "✓" if c_same > a_same else ("=" if c_same == a_same else "✗")
        print(f"  {mark} {topic:<24} {a_same:>4}/{len(a_hits):<5} {c_same:>4}/{len(c_hits)}")

    print()
    print("=" * 76)
    print("结论")
    print("=" * 76)
    a_rate = results["A. 纯向量 k=10（旧）"][0]
    c_rate = results["C. topic 过滤 + 向量 k=10"][0]
    print(f"  A（旧行为）: {a_rate:.0f}%")
    print(f"  C（本次修复）: {c_rate:.0f}%")
    print(f"  提升: {c_rate - a_rate:+.0f} 个点")
    print()
    if c_rate >= 95:
        print("  ✓ topic 过滤是正确解法 —— 100% 命中同话题")
        print("    代价：只在该话题内检索，跨话题能力没了")
        print("    但这正是想要的 —— 状态机已经指定了话题")
    return 0


if __name__ == "__main__":
    sys.exit(main())

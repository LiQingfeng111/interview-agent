"""用正确指标重测：话题命中率，而不是"找到某一道特定题"。

## 上一版为什么错了

我用"话题名"检索，然后检查"该话题的第一道题"是否在结果里。
但一个话题有 12~33 道题（如 Agent 工具调用 20 题、上下文工程 33 题），
检索返回该话题的**另一道题**是完全正确的 —— 面试官问哪道都行。

用"能否找到特定一道题"当指标，等于要求向量检索做精确定位，
这在语义检索里本来就不该期待。

## 正确指标：话题命中率

    query = "Agent 工具调用"
    hits  = top-k 题
    命中  = hits 里有多少道题的 topic 是 "Agent 工具调用"

这才是生产关心的：**给定话题，召回该话题的题**。

## 同时测 topic 过滤的效果

`search_bank` 支持 role 过滤，但没支持 topic 过滤。
如果话题命中率低，说明应该加 topic 过滤（精确解），
而不是指望向量检索"猜对"话题。
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
            SELECT topic, COUNT(*) AS n
            FROM bank_questions
            WHERE embedding IS NOT NULL AND topic IS NOT NULL AND topic <> ''
            GROUP BY topic HAVING COUNT(*) >= 3
            ORDER BY n DESC
        """)
        rows = cur.fetchall()
        cur.close()
    return [(t, n) for t, n in rows]


def main() -> int:
    from app.rag import retriever

    topics = load_topics()
    print("=" * 74)
    print("正确指标：给定话题名，top-k 里该话题的题占多少")
    print("=" * 74)
    print(f"\n话题（题数 ≥3）: {len(topics)} 个")

    for k in (10, 20, 30):
        print(f"\n{'='*74}")
        print(f"k = {k}")
        print(f"{'='*74}")
        print(f"  {'话题':<24} {'同话题命中':>10} {'命中率':>8}")

        total_hits = 0
        total_slots = 0
        perfect = 0

        for topic, n in topics:
            hits = retriever.search_bank(topic, k=k, role="", use="select")
            same = sum(1 for h in hits if (h.project or "") == topic)
            rate = same / len(hits) * 100 if hits else 0

            total_hits += same
            total_slots += len(hits)
            if len(hits) and same == len(hits):
                perfect += 1

            flag = "✓" if rate >= 60 else ("~" if rate >= 30 else "✗")
            print(f"  {flag} {topic:<22} {same:>3}/{len(hits):<3} {rate:>6.0f}%")

        overall = total_hits / total_slots * 100 if total_slots else 0
        print(f"\n  合计: {total_hits}/{total_slots} = {overall:.0f}%")
        print(f"  全部命中同话题的话题数: {perfect}/{len(topics)}")

    print()
    print("=" * 74)
    print("结论")
    print("=" * 74)
    print("  如果同话题命中率低，说明向量检索没有充分利用 topic 信息。")
    print("  解法不是加重排，而是 **直接用 topic 过滤**（题库里有这个字段，")
    print("  却只用来做加权，没用来过滤）—— 这是精确解，零成本。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""最终诊断：topic 标签和题目文本到底对不对得上。

## 前一个假设被推翻了

我以为"查询太短"是原因，但实测扩展查询后**更差**：

    RAG              2/10
    RAG 检索增强生成    2/10   （没改善）
    RAG 系统的召回与生成质量  4/10   （略有改善但仍低）

所以不是短查询的问题。

## 新假设：topic 字段和题目文本不一致

如果 `topic = 'RAG'` 的题目，问的是"如何评估召回率"、"切块大小怎么定"，
那么它们的**文本里根本没有 'RAG' 这个词**。

向量检索匹配的是 `question` 文本，不是 `topic` 标签。
所以用 "RAG" 去查，查不到那些题 —— 这是必然的，不是模型的问题。

## 验证方法

直接看 topic='RAG' 的题，它们的 question 里有多少含 "RAG" 或 "检索"。
如果很少，那结论就是：**必须按 topic 字段过滤，不能靠向量猜**。
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
os.environ.setdefault("IA_REDIS", "0")
os.environ.setdefault("IA_JWT_SECRET", "x" * 32)

import logging
logging.basicConfig(level=logging.ERROR)


def main() -> int:
    from app.repos.pg import get_conn

    print("=" * 78)
    print("诊断：topic 标签 vs 题目文本")
    print("=" * 78)

    for topic in ("RAG", "Agent", "Transformer", "评测", "状态管理"):
        print(f"\n{'='*78}")
        print(f"topic = {topic!r}")
        print(f"{'='*78}")

        with get_conn() as c:
            cur = c.cursor()
            cur.execute("""
                SELECT question,
                       (question ILIKE %s) AS has_topic_word
                FROM bank_questions
                WHERE topic = %s AND embedding IS NOT NULL
                ORDER BY qid LIMIT 8
            """, (f"%{topic}%", topic))
            rows = cur.fetchall()
            cur.close()

        if not rows:
            print("  （无此题）")
            continue

        for q, has_word in rows:
            mark = "含" if has_word else "无"
            print(f"  [{mark}] {q[:64]}")

        n_with = sum(1 for _, h in rows if h)
        print(f"\n  前 {len(rows)} 题里，question 含 {topic!r} 的: {n_with}/{len(rows)}")

    # 全库统计
    print()
    print("=" * 78)
    print("全库统计：topic 词出现在 question 里的比例")
    print("=" * 78)

    with get_conn() as c:
        cur = c.cursor()
        for topic in ("RAG", "Agent", "Transformer", "评测", "状态管理",
                      "数据库索引", "缓存一致性", "微调与 LoRA"):
            cur.execute("""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN question ILIKE %s THEN 1 ELSE 0 END) AS with_word
                FROM bank_questions
                WHERE topic = %s AND embedding IS NOT NULL
            """, (f"%{topic}%", topic))
            total, with_word = cur.fetchone()
            if total:
                pct = (with_word or 0) / total * 100
                flag = "✓" if pct > 60 else ("~" if pct > 30 else "✗")
                print(f"  {flag} {topic:<16} {with_word or 0:>3}/{total:<3} = {pct:>5.0f}%")
        cur.close()

    print()
    print("=" * 78)
    print("结论")
    print("=" * 78)
    print("  ✗ 标记的话题：题目文本里几乎不含话题词")
    print("    → 向量检索天然查不到，这不是模型问题")
    print("    → 正确做法：按 topic 字段过滤（精确匹配），不要靠向量猜")
    print()
    print("  ✓ 标记的话题：题目文本里含话题词")
    print("    → 向量检索能正常工作，命中率 90%+")
    return 0


if __name__ == "__main__":
    sys.exit(main())

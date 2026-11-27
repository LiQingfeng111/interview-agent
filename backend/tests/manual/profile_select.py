"""定位选题耗时：向量化 vs 向量查询 vs 打分，各占多少。

## 为什么要拆开测

现象：16 轮面试的选题耗时从 ~40 秒（recall_k=15）涨到 ~90 秒（recall_k=30）。
但 pgvector 查 30 条 vs 15 条应该只差几毫秒。

如果耗时和 k 不成比例，说明瓶颈不在数据库，而在别处 —— 大概率是
**每次选题都要调 Ollama 向量化查询串**（约 270ms/次）。

16 轮 × 270ms ≈ 4.3 秒，也对不上 50 秒的增量。
所以必须逐段计时才能定位。
"""

import os
import sys
import time
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

from app.rag import bank_select, retriever  # noqa: E402


def timeit(label, fn, n=3):
    """跑 n 次取平均。"""
    times = []
    for _ in range(n):
        t0 = time.time()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ! {label} 失败: {e}")
            return 0
        times.append((time.time() - t0) * 1000)
    avg = sum(times) / len(times)
    print(f"  {label:<38} {avg:>7.0f} ms  (n={n})")
    return avg


def main():
    from app.rag.embedder import get_embedder

    print("=" * 64)
    print("选题各环节耗时拆解")
    print("=" * 64)

    query = "召回从71%提到82%，切块500字的取舍"

    print("\n[1] 向量化（走 Ollama）")
    emb = get_embedder()
    t_embed = timeit("embed 单条查询", lambda: emb.embed([query]))

    print("\n[2] pgvector 查询（不同 k）")
    for k in (15, 30, 50):
        timeit(f"search_bank k={k}",
               lambda k=k: retriever.search_bank(query, k=k, role="ai_algo",
                                                 use="select"), n=2)

    print("\n[3] 完整选题（含打分/配额/加权）")
    for k in (15, 30):
        timeit(f"bank_select.pick k={k}",
               lambda k=k: bank_select.pick(
                   query=query, role="ai_algo", asked_qids=[],
                   turn=3, topic_counts={}, k=k, total_turns=16),
               n=2)

    print()
    print("=" * 64)
    print("结论")
    print("=" * 64)
    print(f"  单次向量化: {t_embed:.0f} ms")
    print("  → 16 轮面试光向量化就约 {:.1f} 秒".format(t_embed * 16 / 1000))
    print()
    print("  如果 pgvector 查询只占几 ms，那瓶颈就是向量化 ——")
    print("  优化方向应该是**缓存查询向量**或**减少重复向量化**，")


if __name__ == "__main__":
    main()

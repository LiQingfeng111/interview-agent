"""验证 cross-encoder 能否加载并打分。

首次运行会下载模型（bge-reranker-v2-m3 约 2.2GB）。
如果网络不通会明确报错，不静默失败。
"""

import sys
import time

print("=== 版本检查 ===")
for mod in ("torch", "transformers", "sentence_transformers"):
    try:
        m = __import__(mod)
        print(f"  {mod}: {getattr(m, '__version__', 'ok')}")
    except ImportError as e:
        print(f"  ✗ {mod}: {e}")
        sys.exit(1)

print()
print("=== 加载 CrossEncoder（首次会下载 2.2GB）===")
t0 = time.time()

try:
    from sentence_transformers import CrossEncoder

    model = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
    print(f"  ✓ 加载完成（{time.time() - t0:.1f} 秒）")
except Exception as e:
    print(f"  ✗ 失败: {type(e).__name__}: {e}")
    sys.exit(1)

print()
print("=== 打分测试（这两对是本项目的关键案例）===")
pairs = [
    # 语义相关、字面不重合 —— 这正是我们需要 rerank 解决的
    ("你的 baseline 是什么", "改造前的指标是多少，和现在怎么比"),
    ("向量检索的索引结构", "vector index 的选择：HNSW 与 IVF 的取舍"),
    # 不相关对照
    ("你的 baseline 是什么", "今天天气不错适合散步"),
    # 字面重合
    ("HNSW 的 M 参数", "HNSW 索引参数 M 取 16"),
]

try:
    scores = model.predict(pairs, show_progress_bar=False)
    for (q, d), s in zip(pairs, scores):
        print(f"  {s:>8.4f}  {q[:18]:<20} vs {d[:32]}")
except Exception as e:
    print(f"  ✗ 打分失败: {type(e).__name__}: {e}")
    sys.exit(1)

print()
print("=== 判断 ===")
scores = list(scores)
if scores[0] > scores[2]:
    print("  ✓ rerank 能区分'语义相关但字面不同'和'完全不相关'")
    print("    （前者分数高于后者，说明模型有用）")
else:
    print("  ! rerank 没能区分，需要换模型或调参")

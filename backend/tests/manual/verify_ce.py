import traceback

print("=== CrossEncoder 导入测试 ===")
try:
    from sentence_transformers import CrossEncoder
    print("  ✓ CrossEncoder 导入成功")
except Exception:
    print("  ✗ 失败：")
    for line in traceback.format_exc().split("\n")[-8:]:
        if line.strip():
            print("    " + line)
    raise SystemExit(1)

print()
print("=== 加载模型（首次下载 2.2GB）===")
import time
t0 = time.time()
try:
    model = CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
    print(f"  ✓ 加载成功（{time.time() - t0:.1f} 秒）")
except Exception:
    print("  ✗ 加载失败：")
    for line in traceback.format_exc().split("\n")[-10:]:
        if line.strip():
            print("    " + line)
    raise SystemExit(1)

print()
print("=== 打分（关键案例）===")
pairs = [
    ("你的 baseline 是什么", "改造前的指标是多少，和现在怎么比"),
    ("向量检索的索引结构", "vector index 的选择：HNSW 与 IVF 的取舍"),
    ("你的 baseline 是什么", "今天天气不错适合散步"),
    ("HNSW 的 M 参数", "HNSW 索引参数 M 取 16"),
]
scores = model.predict(pairs, show_progress_bar=False)

for (q, d), s in zip(pairs, scores):
    print(f"  {float(s):>9.4f}  {q[:20]:<22} vs {d[:30]}")

print()
if float(scores[0]) > float(scores[2]):
    print("  ✓ rerank 能区分语义相关与不相关")
    print(f"    （{float(scores[0]):.3f} > {float(scores[2]):.3f}）")
else:
    print("  ! 区分度不够")

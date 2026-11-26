"""用真实 bge-m3 打分，标定 RAG 阈值。

为什么要标定而不是拍一个 0.8：
bge-m3 的相似度分布比直觉保守——实测「召回71%→82%」与
「recall 0.71→0.82」只有 0.70，而不是 0.9。
如果阈值照 0.8 写，正样本全部会被丢掉，RAG 等于没做。

这个脚本输出：正/负样本的分数分布 + 在多个阈值下的精确率/召回率，
最终给出一组"正样本尽量不丢、负样本尽量挡住"的建议阈值。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE = "http://172.22.48.1:11800"
MODEL = "bge-m3"


def embed(texts: list[str]) -> list[list[float]]:
    req = urllib.request.Request(
        BASE + "/api/embed",
        data=json.dumps({"model": MODEL, "input": texts}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["embeddings"]


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="tools/embedding_calib.json")
    ap.add_argument("--out", default="tools/embedding_calib_result.json")
    args = ap.parse_args()

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    rows = []
    for group in ("positive", "negative"):
        for a, b, label in data[group]:
            rows.append({"group": group, "a": a, "b": b, "label": label})

    # 一次批量嵌入，减少往返
    texts = []
    for r in rows:
        texts.append(r["a"])
        texts.append(r["b"])
    vecs = embed(texts)

    print("=== 逐条相似度 ===")
    pos, neg = [], []
    for i, r in enumerate(rows):
        sim = cosine(vecs[2 * i], vecs[2 * i + 1])
        r["sim"] = round(sim, 4)
        (pos if r["group"] == "positive" else neg).append(sim)
        mark = "正" if r["group"] == "positive" else "负"
        print(f"  [{mark}] {sim:.3f}  {r['label']:<18} {r['a'][:18]} ↔ {r['b'][:18]}")

    print()
    print("=== 分布 ===")
    print(f"  正样本 n={len(pos)}  min={min(pos):.3f}  中位={sorted(pos)[len(pos)//2]:.3f}  max={max(pos):.3f}")
    print(f"  负样本 n={len(neg)}  min={min(neg):.3f}  中位={sorted(neg)[len(neg)//2]:.3f}  max={max(neg):.3f}")
    print(f"  正样本最低 {min(pos):.3f} vs 负样本最高 {max(neg):.3f}"
          + ("  ← 可分" if min(pos) > max(neg) else "  ← 有重叠，需取舍"))

    print()
    print("=== 阈值扫描（正样本=该召回，负样本=该挡住）===")
    print("   阈值  正样本保留  负样本误放   F1")
    best = None
    for t in [x / 100 for x in range(40, 96, 5)]:
        tp = sum(1 for s in pos if s >= t)
        fp = sum(1 for s in neg if s >= t)
        recall = tp / len(pos)
        precision = tp / (tp + fp) if tp + fp else 1.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
        print(f"   {t:.2f}   {tp}/{len(pos)}        {fp}/{len(neg)}       {f1:.3f}")
        if best is None or f1 > best[1]:
            best = (t, f1, recall, precision)

    print()
    print(f"=== 建议阈值 {best[0]:.2f}（F1={best[1]:.3f}，正样本召回 {best[2]*100:.0f}%，精确率 {best[3]*100:.0f}%）===")
    print("注：题库去重与简历定位可以用不同阈值——前者宁可严（误杀代价高），后者宁可宽（漏了会问错项目）。")

    Path(args.out).write_text(
        json.dumps({"rows": rows, "suggested_threshold": best[0],
                    "pos_min": min(pos), "neg_max": max(neg)},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n明细已写入 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Agent 的可靠性能不能"逐步相乘"预测？—— 检验错误是独立还是传染

背景（为什么这件事重要）：
    业界算 Agent 成功率一律用连乘：单步 95%，10 步 = 0.95^10 = 60%。
    这个公式的隐含假设是**每步的错误互相独立**。
    如果错误会传染（一步错了，下一步更可能错），连乘就是**系统性乐观**，
    所有"我的 Agent 能跑 20 步"的承诺都是虚的。

本脚本只做一个检验：
    在一场轨迹内部，把每轮判成 好/差（相对本场均分），
    然后看"连续差"的游程长度分布，和"独立抛硬币"的几何分布比。
    实际游程显著更长 → 错误传染；接近几何 → 独立，连乘成立。

用到的统计：游程检验（Wald–Wolfowitz 思想的离散版本）+ 几何分布期望对比。
纯标准库。
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "runs")
DIMS = ("accuracy", "depth", "tradeoff", "boundary", "clarity")


def load_runs():
    runs = {}
    for path in sorted(glob.glob(os.path.join(RUNS, "run_*.json"))):
        try:
            j = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        seq = []
        for t in j.get("turns") or []:
            ev = t.get("evaluation") or {}
            sc = [ev[d] for d in DIMS if isinstance(ev.get(d), (int, float))]
            if sc:
                seq.append(sum(sc) / len(sc))
        if len(seq) >= 6:
            runs[os.path.basename(path)] = seq
    return runs


def runs_of(items, pred):
    """返回所有 True 游程的长度列表。"""
    out, cur = [], 0
    for v in items:
        if pred(v):
            cur += 1
        else:
            if cur:
                out.append(cur)
            cur = 0
    if cur:
        out.append(cur)
    return out


def main():
    runs = load_runs()
    if not runs:
        print("场次不足（需 >=6 轮）")
        return 1

    all_len = sum(len(v) for v in runs.values())
    print(f"场次 {len(runs)}   轮次 {all_len}   平均 {all_len / len(runs):.1f} 轮/场")
    print()

    # 判据：低于本场均分 = 一格差
    bad_runs, good_runs = [], []
    p_list = []
    for seq in runs.values():
        m = statistics.mean(seq)
        flags = [v < m for v in seq]
        p_list.append(sum(flags) / len(flags))
        bad_runs += runs_of(flags, lambda x: x)
        good_runs += runs_of(flags, lambda x: not x)

    p = statistics.mean(p_list)
    print(f"单轮'低于本场均分'的基础概率 p = {p:.3f}")
    print()

    # ---- 实测游程分布 vs 几何分布 ----
    print("连续'差'的游程长度分布（游程 = 连续几轮都低于本场均分）")
    print(f"  {'长度':>4}{'实测条数':>9}{'实测占比':>9}{'独立假设期望':>13}{'比值':>8}")
    print("  " + "-" * 48)
    cnt = Counter(bad_runs)
    n_runs = len(bad_runs)
    exp_total = n_runs
    cum = 0.0
    for k in range(1, 9):
        observed = cnt.get(k, 0)
        share = observed / n_runs if n_runs else 0
        geom = (1 - p) * (p ** (k - 1))
        print(f"  {k:>4}{observed:>9}{share:>8.1%}{geom:>12.1%}{('%.1fx' % (share / geom)) if geom else '--':>8}")

    # ---- 关键数字：平均游程长度 ----
    mean_obs = statistics.mean(bad_runs) if bad_runs else 0
    mean_indep = 1 / (1 - p) if p < 1 else float("inf")
    print()
    print(f"实测平均连续差轮数 = {mean_obs:.2f}")
    print(f"独立假设下的期望   = 1/(1-p) = {mean_indep:.2f}")
    ratio = mean_obs / mean_indep
    print(f"传染系数 = {ratio:.2f}x   {'> 1.3 → 错误在传染，连乘公式乐观' if ratio > 1.3 else ('≈1 → 错误近独立，连乘可用' if ratio > 0.8 else '< 0.8 → 反相关，存在自我纠正')}")

    # ---- 换成业界口径：可靠度衰减 ----
    print()
    print("换算成业界口径（假设单轮成功率 = 1 - p）")
    single = 1 - p
    print(f"  单轮成功率 {single:.1%}")
    print(f"  {'步数':>5}{'连乘预测':>12}{'按实测传染系数修正':>20}")
    print("  " + "-" * 40)
    for steps in (5, 10, 15, 20):
        naive = single ** steps
        # 传染修正：有效失败率按游程聚集放大。用 E[游程]/独立E[游程] 作为聚集度
        adj = single ** (steps * min(ratio, 3.0))
        print(f"  {steps:>5}{naive:>11.1%}{adj:>19.1%}")
    print()
    print("注意：上面的修正是个一阶近似，只用来展示量级差异，不是严格模型。")
    print()
    print("这个检验的价值：")
    print("  若 ratio > 1.3，说明'单步成功率连乘'在 Agent 上是错的，")
    print("  任何'20 步端到端可靠度'的承诺都必须用轨迹级实测，而不是把单步数字乘起来。")
    print("  这是一个只有 Agent 才有的统计问题——单轮 QA 根本不存在它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

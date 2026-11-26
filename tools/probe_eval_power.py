"""评测数据的"独立观测数"到底有几个？—— ICC / 设计效应 / 有效样本量

问题：
    你的 eval 报"1131 条评分、均值 2.98"。但评分是嵌套在**场次**里的，
    同一场的十几轮共享同一个候选人、同一套项目、同一份简历。
    这在统计上叫伪重复（pseudoreplication）：1131 条不是 1131 个独立证据。

本脚本只算四个数，不建任何框架：
    ICC(1)   场次间方差 / 总方差      —— "同一场里的评分有多相关"
    deff     设计效应 1+(m-1)*ICC     —— "等效于损失多少样本"
    n_eff    有效样本量 N/deff
    95% CI   报告一处差异时的不确定度

用途：任何"v2 比 v1 好 0.2 分"的结论，都要先过这个数。
"""
from __future__ import annotations

import glob
import json
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "runs")
DIMS = ("accuracy", "depth", "tradeoff", "boundary", "clarity")


def load_sessions():
    out = {}
    for path in sorted(glob.glob(os.path.join(RUNS, "run_*.json"))):
        try:
            j = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        vals = []
        for t in j.get("turns") or []:
            ev = t.get("evaluation") or {}
            sc = [ev[d] for d in DIMS if isinstance(ev.get(d), (int, float))]
            if sc:
                vals.append(sum(sc) / len(sc))
        if len(vals) >= 2:
            out[os.path.basename(path)] = vals
    return out


def icc1(groups):
    """单因素随机效应 ICC(1)：不需要平衡设计，按 n0 校正。"""
    k = len(groups)
    ns = [len(g) for g in groups]
    N = sum(ns)
    grand = sum(sum(g) for g in groups) / N
    ssb = sum(len(g) * (statistics.mean(g) - grand) ** 2 for g in groups)
    ssw = sum(sum((v - statistics.mean(g)) ** 2 for v in g) for g in groups)
    msb = ssb / (k - 1)
    msw = ssw / (N - k)
    n0 = (N - sum(n * n for n in ns) / N) / (k - 1)
    icc = (msb - msw) / (msb + (n0 - 1) * msw)
    return dict(k=k, N=N, ns=ns, m=statistics.mean(ns), msb=msb, msw=msw,
                icc=icc, msw_sd=msw ** 0.5)


def main():
    g = load_sessions()
    if len(g) < 3:
        print("场次不足，无法估计 ICC")
        return 1
    r = icc1(list(g.values()))
    deff = 1 + (r["m"] - 1) * max(r["icc"], 0.0)
    n_eff = r["N"] / deff

    print(f"簇(场次) k={r['k']}   观测 N={r['N']}   平均簇大小 m={r['m']:.1f}")
    print(f"组间 MS={r['msb']:.4f}   组内 MS={r['msw']:.4f}")
    print()
    print(f"ICC(1)      = {r['icc']:+.4f}")
    print(f"设计效应 deff = {deff:.2f}")
    print(f"有效样本 n_eff = {r['N']} / {deff:.2f} = {n_eff:.1f}")
    print()
    print(f"→ 你说的'1131 条评分'，统计上等价于约 {round(n_eff)} 条独立观测。")
    print(f"  损失了 {1 - n_eff / r['N']:.1%} 的样本量，全部来自'同一场内高度相关'。")
    print()
    sd = r["msw_sd"]
    print(f"单轮评分标准差 ≈ {sd:.3f}（满分 5）")
    print(f"{'独立观测数':>10}{'两组均值差的95%半宽':>24}")
    print("-" * 36)
    for n in (r["N"] / deff, r["N"], r["k"], 60, 120):
        se = sd * (deff / n) ** 0.5 if n <= r["N"] else sd * (2 / n) ** 0.5
        half = 1.96 * se * (2 ** 0.5)
        print(f"{n:>10.0f}{'±' + format(half, '.2f'):>24}")
    print()
    print("读法：")
    print("  deff > 2 说明你的 eval 把同一份证据数了多遍，")
    print("  任何 A/B 结论若没有做簇级聚合（先按场取均值再比较），显著性会被系统性高估。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

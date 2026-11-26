"""Agent 评测体检仪 —— 4 项检查，每个都给一个数字和一个判决。

为什么需要它：
    所有 agent 团队都在报"v2 比 v1 提升 0.2 分"，但几乎没人问过：
        (1) 这些"条"评分真的是独立的吗？
        (2) 分数变化是 prompt 的功劳，还是换了批数据 / 换了评判口径？
        (3) 提升 0.2 分，超出噪声了吗？
        (4) 要证明 d=0.3 的效应，我一共要跑多少场？
    这四个问题不回答，eval 就是自证清白的仪式。

四项检查：
    C1 独立观测数   ICC(1) / 设计效应 deff / 有效样本 n_eff
    C2 结构泄漏     重复答案率、位置共线、评判官是否启发式
    C3 效应显著性   簇级聚合 + bootstrap 置信区间（不用正态近似）
    C4 所需样本量   power analysis：检测 d 需要多少场

用法：
    python tools/eval_health.py                    # 体检本仓库 runs/
    python tools/eval_health.py --two runs_a runs_b  # 比较两组（A/B）

纯标准库，不装包。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import statistics
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIMS = ("accuracy", "depth", "tradeoff", "boundary", "clarity")


# ---------------------------------------------------------------- 读数据

def load_dir(path: str) -> dict[str, list[dict]]:
    """{场次名: [turn, ...]}，turn 里带分数、答案、阶段、位置。"""
    out = {}
    for fp in sorted(glob.glob(os.path.join(path, "run_*.json"))):
        try:
            j = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        turns, pos = [], 0
        for t in j.get("turns") or []:
            ev = t.get("evaluation") or {}
            sc = [ev[d] for d in DIMS if isinstance(ev.get(d), (int, float))]
            if not sc:
                continue
            turns.append({
                "score": sum(sc) / len(sc),
                "answer": str(t.get("answer") or ""),
                "phase": t.get("phase"),
                "evidence": str(ev.get("evidence") or ""),
                "pos": pos,
            })
            pos += 1
        if turns:
            out[os.path.basename(fp)] = turns
    return out


# ---------------------------------------------------------------- C1

def check_independence(runs: dict) -> dict:
    groups = [[t["score"] for t in ts] for ts in runs.values() if len(ts) >= 2]
    if len(groups) < 3:
        return {"ok": False, "why": "场次不足 3 个"}
    k, N = len(groups), sum(len(g) for g in groups)
    ns = [len(g) for g in groups]
    grand = sum(sum(g) for g in groups) / N
    ssb = sum(len(g) * (statistics.mean(g) - grand) ** 2 for g in groups)
    ssw = sum(sum((v - statistics.mean(g)) ** 2 for v in g) for g in groups)
    msb, msw = ssb / (k - 1), ssw / (N - k)
    n0 = (N - sum(n * n for n in ns) / N) / (k - 1)
    icc = max((msb - msw) / (msb + (n0 - 1) * msw), 0.0)
    m = N / k
    deff = 1 + (m - 1) * icc
    return {"ok": True, "k": k, "N": N, "m": m, "icc": icc,
            "deff": deff, "n_eff": N / deff, "sd": msw ** 0.5}


# ---------------------------------------------------------------- C2

def check_leakage(runs: dict) -> dict:
    all_turns = [t for ts in runs.values() for t in ts]
    ans = Counter(t["answer"][:80] for t in all_turns)
    dup_share = sum(v for v in ans.values() if v > 1) / len(all_turns) if all_turns else 0

    # 位置共线：把"第几轮"当自变量，看它对分数的解释力（eta² 的粗略版）
    by_pos: dict[int, list[float]] = {}
    for ts in runs.values():
        for t in ts:
            by_pos.setdefault(t["pos"], []).append(t["score"])
    grand = statistics.mean(t["score"] for t in all_turns)
    ss_between = sum(len(v) * (statistics.mean(v) - grand) ** 2 for v in by_pos.values())
    ss_total = sum((t["score"] - grand) ** 2 for t in all_turns) or 1e-9
    eta2_pos = ss_between / ss_total

    # 评判官是否启发式：看分数与答案长度/数字的相关（启发式评分的典型指纹）
    lens = [len(t["answer"]) for t in all_turns]
    scores = [t["score"] for t in all_turns]
    r_len = _pearson(lens, scores)

    return {"dup_share": dup_share, "unique_answers": len(ans),
            "n_answers": len(all_turns), "eta2_pos": eta2_pos,
            "n_positions": len(by_pos), "r_len": r_len}


def _pearson(xs, ys):
    if len(xs) < 3:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


# ---------------------------------------------------------------- C3

def check_effect(a: dict, b: dict, iters: int = 2000, seed: int = 7) -> dict:
    """簇级聚合 + bootstrap：按场取均值后重采样场次，不用正态近似。"""
    ma = [statistics.mean(t["score"] for t in ts) for ts in a.values()]
    mb = [statistics.mean(t["score"] for t in ts) for ts in b.values()]
    if len(ma) < 3 or len(mb) < 3:
        return {"ok": False, "why": "每组至少 3 场"}
    obs = statistics.mean(mb) - statistics.mean(ma)
    rng = random.Random(seed)
    diffs = []
    for _ in range(iters):
        sa = [rng.choice(ma) for _ in ma]
        sb = [rng.choice(mb) for _ in mb]
        diffs.append(statistics.mean(sb) - statistics.mean(sa))
    diffs.sort()
    lo, hi = diffs[int(iters * 0.025)], diffs[int(iters * 0.975)]
    # 双侧 p：bootstrap 分布跨过 0 的比例
    p = 2 * min(sum(1 for d in diffs if d <= 0), sum(1 for d in diffs if d >= 0)) / iters
    # 合并标准差 → Cohen's d（按场为单位）
    sd = statistics.stdev(ma + mb)
    return {"ok": True, "n_a": len(ma), "n_b": len(mb), "obs": obs,
            "lo": lo, "hi": hi, "p": min(p, 1.0), "d": obs / sd if sd else 0.0,
            "raw_naive_p": None}


# ---------------------------------------------------------------- C4

def check_power(d: float, sd: float, per_session: int = 15) -> dict:
    """要检测效应 d（以场次为单位），每组需要多少场？近似式 n ≈ 16/d²。"""
    d = abs(d) or 0.01
    need = max(int(2 * (2.8 / d) ** 2), 3)
    return {"d": d, "need_per_group": need,
            "need_total": need * 2,
            "equivalent_turns": need * 2 * per_session}


# ---------------------------------------------------------------- 报告

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "runs"))
    ap.add_argument("--two", nargs=2, metavar=("A", "B"), help="两组 run 目录，做 A/B")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    args = ap.parse_args()

    runs = load_dir(args.data)
    if not runs:
        print(f"{args.data} 下没有可解析的 run_*.json")
        return 1

    line = "=" * 68
    print(line)
    print("Agent 评测体检报告")
    print(line)
    print(f"数据：{args.data}   场次 {len(runs)}   轮次 {sum(len(v) for v in runs.values())}")
    print()

    # ---- C1 ----
    c1 = check_independence(runs)
    print("[C1] 独立观测数")
    if not c1["ok"]:
        print(f"     无法计算：{c1['why']}")
    else:
        print(f"     场次 k={c1['k']}  观测 N={c1['N']}  平均 {c1['m']:.1f} 轮/场")
        print(f"     ICC(1) = {c1['icc']:.3f}   设计效应 deff = {c1['deff']:.2f}")
        print(f"     有效样本 n_eff = {c1['n_eff']:.0f}  （名义 {c1['N']}，损失 {1 - c1['n_eff'] / c1['N']:.0%}）")
        if c1["deff"] > 2:
            print(f"     ✗ 判决：同一场内的评分高度相关（ICC={c1['icc']:.2f}）。")
            print(f"       任何以'轮次'为单位的显著性检验都被高估 {c1['deff']:.1f} 倍。")
            print(f"       必须改成：先按场取均值，再比较。")
        else:
            print(f"     ✓ 判决：聚类效应可接受。")
    print()

    # ---- C2 ----
    c2 = check_leakage(runs)
    print("[C2] 结构泄漏")
    print(f"     唯一答案 {c2['unique_answers']} / 总答案 {c2['n_answers']}"
          f"   重复答案占比 {c2['dup_share']:.1%}")
    print(f"     轮次位置解释的分数方差 eta² = {c2['eta2_pos']:.1%}（位置种类 {c2['n_positions']}）")
    print(f"     分数 vs 答案长度的相关 r = {c2['r_len']:+.3f}")
    flags = []
    if c2["dup_share"] > 0.3:
        flags.append("答案高度重复 → 同一份输入被反复评分，样本量是假的")
    if c2["eta2_pos"] > 0.4:
        flags.append("位置效应过大 → '第几问'本身就决定分数，A/B 若轮次数不同则不可比")
    if abs(c2["r_len"]) > 0.5:
        flags.append("分数与长度强相关 → 评判官疑似启发式（长度/关键词），不是真模型判断")
    if flags:
        print("     ✗ 判决：")
        for f in flags:
            print(f"       - {f}")
    else:
        print("     ✓ 判决：未发现明显泄漏。")
    print()

    # ---- C3 / C4 ----
    if args.two:
        a, b = load_dir(args.two[0]), load_dir(args.two[1])
        c3 = check_effect(a, b)
        print("[C3] 效应显著性（簇级聚合 + bootstrap）")
        if not c3["ok"]:
            print(f"     无法计算：{c3['why']}")
        else:
            print(f"     {args.label_a}: {c3['n_a']} 场    {args.label_b}: {c3['n_b']} 场")
            print(f"     均值差 = {c3['obs']:+.3f}   95% CI [{c3['lo']:+.3f}, {c3['hi']:+.3f}]   p≈{c3['p']:.3f}")
            print(f"     Cohen's d = {c3['d']:+.2f}")
            if c3["lo"] <= 0 <= c3["hi"]:
                print(f"     ✗ 判决：置信区间跨 0 —— 这个差异在统计上不可区分于噪声。")
            elif c3["p"] < 0.05:
                print(f"     ✓ 判决：差异显著。可以报，但要同时报 CI 宽度。")
            else:
                print(f"     ? 判决：区间未跨 0 但 p≥0.05，样本量偏小，结论不稳。")
        print()
        print("[C4] 所需样本量")
        d = c3.get("d", 0.0) if c3["ok"] else 0.3
        pw = check_power(d, 1.0, int(c1.get("m", 15)) if c1["ok"] else 15)
        print(f"     检测 |d|={abs(d):.2f} 的效应，每组需 {pw['need_per_group']} 场")
        print(f"     合计 {pw['need_total']} 场 ≈ {pw['equivalent_turns']} 轮")
        for dd in (0.2, 0.5, 0.8):
            p2 = check_power(dd, 1.0, int(c1.get("m", 15)) if c1["ok"] else 15)
            print(f"       若效应只有 d={dd}: 每组需 {p2['need_per_group']} 场")
        print()

    # ---- 一页结论 ----
    print(line)
    print("一页结论")
    print(line)
    n_eff = c1.get("n_eff", 0) if c1["ok"] else 0
    print(f"  · 你现在能声称的独立证据量：约 {n_eff:.0f} 条，不是 {c1.get('N', 0)} 条。")
    print(f"  · 在这批数据上，小于 ±{1.96 * (c1.get('sd', 0.4) * c1.get('deff', 1) / max(c1.get('k', 1), 1)) ** 0.5 * 1.41:.2f} 分的均值差不可信。")
    print(f"  · 先修结构（按场聚合 + 真模型评判 + 多样候选人），再谈 prompt 优化。")
    print(f"  · 否则你优化的是一个跟自己重复的指标——这正是 agent eval 现在最普遍的病。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

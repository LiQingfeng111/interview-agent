"""W1 加强版：控制混淆变量后，"误差累积"还成立吗？

上一版跑出 lift +134%，但有个致命混淆没排掉：
    低分不是"模型这轮搞砸了"，也可能是"这个候选人在这一项上就是弱"。
    一个整体很弱的候选人，会连续几轮都低分——这看起来像累积，其实是人的属性在自相关。

本脚本做两件事：
  Q3 场次内中心化：把每轮分数减去**本场均值**，消掉"候选人整体水平"，再看自相关。
      若中心化后 lift 归零 → 累积是人的属性造成的假象，A 的核心假设不成立。
  Q4 阶段控制：分 OPENING / PROJECT_PROBE / TECH_QA 看累积是否只在某一阶段出现。
      若只出现在后期 → 那是"追深了本来就难"，也不是模型误差。
  Q5 把"信息量"信号换成不可循环的量：答案长度是评分官看得见的，用它预测分是循环论证。
      改用"本场前 k 轮平均长度"预测"本轮是否低于本场均值"——这才有决策价值。
"""
from __future__ import annotations

import glob
import json
import os
import re
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "runs")
DIMS = ("accuracy", "depth", "tradeoff", "boundary", "clarity")


def norm(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()\-—…]+", "", s or "")


def load():
    runs = {}
    for path in sorted(glob.glob(os.path.join(RUNS, "run_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        turns = []
        for t in j.get("turns") or []:
            ev = t.get("evaluation") or {}
            scores = [ev[d] for d in DIMS if isinstance(ev.get(d), (int, float))]
            if not scores:
                continue
            ans = str(t.get("answer") or "")
            turns.append({
                "idx": t.get("idx"),
                "phase": t.get("phase"),
                "avg": sum(scores) / len(scores),
                "min": min(scores),
                "len": len(ans),
                "short": len(ans) < 40,
            })
        if len(turns) >= 4:
            runs[os.path.basename(path)] = turns
    return runs


def auc_bad(sig_bad, sig_good):
    if not sig_bad or not sig_good:
        return None
    w = 0.0
    for b in sig_bad:
        for g in sig_good:
            w += 1 if b > g else (0.5 if b == g else 0)
    return w / (len(sig_bad) * len(sig_good))


def main():
    runs = load()
    if not runs:
        print("可用场次不足（每场需 >=4 轮）")
        return 1

    all_turns = [t for ts in runs.values() for t in ts]
    n = len(all_turns)
    print(f"场次 {len(runs)}   轮次 {n}")
    print()

    # ---------- Q3 场次内中心化 ----------
    print("Q3 消掉'候选人整体水平'后再看累积")
    print("   做法：每轮分数减本场均分 → 残差；残差 < 0 记作'本轮低于他自己的水平'")
    resid_pairs, raw_pairs = [], []
    for name, ts in runs.items():
        m = statistics.mean(t["avg"] for t in ts)
        for i, t in enumerate(ts):
            t["resid"] = t["avg"] - m
            if i:
                raw_pairs.append((ts[i - 1]["avg"], t["avg"]))
                resid_pairs.append((ts[i - 1]["resid"], t["resid"]))

    def lift_of(pairs, label):
        base = sum(1 for p, c in pairs if c < 0) / len(pairs) if label == "resid" else None
        if label == "resid":
            prev_bad = [c for p, c in pairs if p < 0]
            prev_good = [c for p, c in pairs if p >= 0]
            p_after_bad = sum(1 for c in prev_bad if c < 0) / len(prev_bad) if prev_bad else 0
            p_after_good = sum(1 for c in prev_good if c < 0) / len(prev_good) if prev_good else 0
            print(f"   基准（本轮低于自身均分）: {base:.1%}   相邻对 {len(pairs)}")
            print(f"   前一轮低于自身均分 (n={len(prev_bad)}) → 本轮也低: {p_after_bad:.1%}")
            print(f"   前一轮不低于均分     (n={len(prev_good)}) → 本轮低:   {p_after_good:.1%}")
            print(f"   lift = {(p_after_bad / base - 1) if base else 0:+.1%}")
            return p_after_bad, p_after_good, base

    lift_of(raw_pairs, "raw")
    print()
    lift_of(resid_pairs, "resid")

    # ---------- Q4 阶段控制 ----------
    print()
    print("Q4 分阶段看（只统计相邻对）")
    print(f"   {'阶段':<16}{'轮次':>6}{'均分':>8}{'低于本场均分占比':>18}")
    print("   " + "-" * 50)
    by_phase = {}
    for ts in runs.values():
        m = statistics.mean(t["avg"] for t in ts)
        for t in ts:
            by_phase.setdefault(t["phase"] or "?", []).append(t["avg"] - m)
    for ph, vals in sorted(by_phase.items(), key=lambda kv: -len(kv[1])):
        below = sum(1 for v in vals if v < 0) / len(vals)
        print(f"   {ph:<16}{len(vals):>6}{statistics.mean(vals):>+8.2f}{below:>17.1%}")

    # ---------- Q5 非循环的信息量信号 ----------
    print()
    print("Q5 用'可观测的历史'预测'本轮会不会掉队'（避免循环论证）")
    print("   目标：本轮分数 < 本场均分（掉队）")
    print("   信号：本轮之前的平均答案长度（低 = 候选人表达贫乏 / 模型抽取失败）")
    print(f"   {'历史长度区间':<18}{'轮次':>6}{'掉队率':>9}")
    print("   " + "-" * 36)
    bins = [(0, 40), (40, 80), (80, 150), (150, 10 ** 9)]
    labels = ["<40 字", "40-80 字", "80-150 字", ">150 字"]
    hist = {lab: [] for lab in labels}
    for ts in runs.values():
        m = statistics.mean(t["avg"] for t in ts)
        for i, t in enumerate(ts):
            if i == 0:
                continue
            prev_len = statistics.mean(x["len"] for x in ts[:i])
            for (lo, hi), lab in zip(bins, labels):
                if lo <= prev_len < hi:
                    hist[lab].append(1 if t["avg"] < m else 0)
                    break
    for lab in labels:
        v = hist[lab]
        if v:
            print(f"   {lab:<18}{len(v):>6}{sum(v) / len(v):>8.1%}")
        else:
            print(f"   {lab:<18}{0:>6}{'--':>9}")

    # ---------- 判读 ----------
    print()
    print("怎么读这三段：")
    print("  Q3 中心化后 lift 若从 +134% 掉到 <30% → 原来的'累积'主要是候选人整体水平，")
    print("     那 A 的卖点不能是'误差累积'，必须换成'同一个人身上的状态漂移'。")
    print("  Q4 若掉队率在后期阶段显著更高 → 那是难度曲线，不是模型退化。")
    print("  Q5 若历史长度越低掉队率越高，且这是**决策时就能看到**的量 → 这才是可用的在线信号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""W1 决定性检验：把"位置效应"和"候选人水平"都扣掉，状态漂移还剩多少？

前面的分析有三个混淆，必须逐个扣：
  C1 位置效应：每场只有 1 个 OPENING / TECH_QA / CANDIDATE_ASK，位置和阶段完全共线。
  C2 候选人水平：弱的人连续低分，看起来像累积。
  C3 循环论证：用"答案长度"预测"评分"，而评分官本来就看得见长度。

本脚本的做法：
  1) 只保留 PROJECT_PROBE 阶段（占 704/1127 轮，且同一阶段内位置可比）
  2) 双重去均值：分数 - 本场均分 - 该位置均值 → 残差
  3) 在残差上看相邻轮自相关（lag-1）
  4) 再检验"在线可得"的历史信号：前 k 轮残差均值 / 前 k 轮假证据率
输出一个数字：控制 C1+C2 后，lag-1 自相关还剩多少。
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
    return re.sub(r"[\s，。！？、,.!?：;:“”\"'（）()\-—…]+", "", s or "")


def load():
    runs = {}
    for path in sorted(glob.glob(os.path.join(RUNS, "run_*.json"))):
        try:
            j = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        ts = []
        for t in j.get("turns") or []:
            ev = t.get("evaluation") or {}
            sc = [ev[d] for d in DIMS if isinstance(ev.get(d), (int, float))]
            if not sc:
                continue
            ans = str(t.get("answer") or "")
            core = norm(str(ev.get("evidence") or "")).strip("…")
            ts.append({
                "phase": t.get("phase"),
                "avg": sum(sc) / len(sc),
                "len": len(ans),
                "fake_ev": bool(core) and core not in norm(ans),
            })
        if len(ts) >= 5:
            runs[os.path.basename(path)] = ts
    return runs


def pearson(xs, ys):
    if len(xs) < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else None


def main():
    runs = load()
    if not runs:
        print("场次不足")
        return 1

    # ---------- 只用 PROJECT_PROBE ----------
    probe = {}
    for name, ts in runs.items():
        seq = [t for t in ts if t["phase"] == "PROJECT_PROBE"]
        if len(seq) >= 4:
            probe[name] = seq
    n_turns = sum(len(v) for v in probe.values())
    print(f"只取 PROJECT_PROBE：场次 {len(probe)}  轮次 {n_turns}")
    if n_turns < 100:
        print("样本不足，结论不成立；先补跑数据")
        return 1

    # ---------- 双重去均值 ----------
    pos_means = {}
    for ts in probe.values():
        for i, t in enumerate(ts):
            pos_means.setdefault(i, []).append(t["avg"])
    pos_mean = {i: statistics.mean(v) for i, v in pos_means.items()}

    xs, ys = [], []
    for ts in probe.values():
        m = statistics.mean(t["avg"] for t in ts)
        for t in ts:
            t["resid"] = t["avg"] - m - pos_mean.get(ts.index(t), 0) + statistics.mean(
                [x["avg"] for x in ts])
        seq = ts
        for i in range(1, len(seq)):
            xs.append(seq[i - 1]["resid"])
            ys.append(seq[i]["resid"])
    r = pearson(xs, ys)
    print(f"控制位置+候选人水平后，lag-1 自相关 r = {r:+.3f}  (n={len(xs)})")
    print(f"  解读: r² = {r * r:.3f} → 上一轮残差只能解释本轮 {r * r:.1%} 的波动")
    print("  判据: r ≥ 0.35 才算'状态漂移可建模'；0.15–0.35 只能做弱提示；<0.15 等于噪声")

    # ---------- 在线可得的历史信号 ----------
    print()
    print("在线信号（只用本轮之前的信息，无循环）")
    print(f"  {'历史信号':<28}{'目标掉队率':>12}{'信号组':>18}{'lift':>9}")
    print("  " + "-" * 68)
    for key, label, thresh in (
        ("fake_ev", "前 k 轮假证据率 > 0", None),
        ("len", "前 k 轮平均长度 < 60 字", 60),
        ("resid", "前 k 轮残差均值 < 0", 0),
    ):
        hit, miss = [], []
        for ts in probe.values():
            m = statistics.mean(t["avg"] for t in ts)
            for i in range(1, len(ts)):
                hist = ts[:i]
                if key == "fake_ev":
                    sig = any(h["fake_ev"] for h in hist)
                elif key == "len":
                    sig = statistics.mean(h["len"] for h in hist) < thresh
                else:
                    sig = statistics.mean(h.get("resid", 0) for h in hist) < thresh
                low = ts[i]["avg"] < m
                (hit if sig else miss).append(low)
        if not hit or not miss:
            print(f"  {label:<28}{'样本不足':>12}")
            continue
        base = (sum(hit) + sum(miss)) / (len(hit) + len(miss))
        ph = sum(hit) / len(hit)
        pm = sum(miss) / len(miss)
        print(f"  {label:<28}{base:>11.1%}{ph:>11.1%}/{pm:<6.1%}{(ph / base - 1):>+9.1%}")

    print()
    print("最终判据（写进课题立项报告的第一行）：")
    print("  r ≥ 0.35 且某在线信号 lift ≥ +40% → 方向 A 成立，卖点是'轨迹级状态漂移的在线检测'")
    print("  r < 0.35 但在线信号 lift ≥ +40% → 卖点必须改成'历史行为统计驱动的诚实物弃权'，")
    print("                                        不要声称'误差累积模型'，会被一问就穿")
    print("  两者都不满足                     → 切方向 B，A 的前提在你这批数据上不成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())

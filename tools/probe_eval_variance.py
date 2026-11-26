"""Agent 评测的方差从哪来？—— 三方分解：场次 / 位置 / 残余

为什么这件事只有 Agent 才需要问：
    单轮 QA 的评测，观测是 iid 的，方差只有一个来源（模型）。
    Agent 不一样：一场 = 一个候选人 × 一条轨迹，第 k 步的选择约束第 k+1 步。
    所以"分数变了"至少有三种可能：
        换了候选人 / 追深了位置变了 / 模型这次抽签抽差了
    分不清这三者，A/B 就只能看均值，均值一抖就误判。

本脚本用嵌套方差分解给出口径：
    σ²_session   场次间（候选人 + 简历 + 项目的差异）
    σ²_position  同一场内、轮次位置带来的系统差异
    σ²_resid     剩下的（模型随机性 + 评判随机性 + 交互）

输出一个可操作结论：做 A/B 时应该先按什么聚合，才能把信噪比拉到可用。
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


def load():
    """返回 [(session, position, phase, score)]，只取 PROJECT_PROBE 之外的完整序列。"""
    obs = []
    for path in sorted(glob.glob(os.path.join(RUNS, "run_*.json"))):
        try:
            j = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        name = os.path.basename(path)
        i = 0
        for t in j.get("turns") or []:
            ev = t.get("evaluation") or {}
            sc = [ev[d] for d in DIMS if isinstance(ev.get(d), (int, float))]
            if not sc:
                continue
            obs.append((name, i, t.get("phase"), sum(sc) / len(sc)))
            i += 1
    return obs


def decompose(obs):
    sessions = sorted({o[0] for o in obs})
    grand = statistics.mean(o[3] for o in obs)
    N = len(obs)

    # 场次效应
    by_s = {}
    for o in obs:
        by_s.setdefault(o[0], []).append(o[3])
    ss_s = sum(len(v) * (statistics.mean(v) - grand) ** 2 for v in by_s.values())
    df_s = len(by_s) - 1

    # 位置效应（场次内去均值后，按位置聚合）
    by_p = {}
    for o in obs:
        m = statistics.mean(by_s[o[0]])
        by_p.setdefault(o[1], []).append(o[3] - m)
    # 位置效应量 = 各位置去均值后均值的方差 × 平均出现次数
    pos_means = {k: statistics.mean(v) for k, v in by_p.items()}
    within = [o[3] - statistics.mean(by_s[o[0]]) for o in obs]
    ss_p = sum(len(v) * pos_means[k] ** 2 for k, v in by_p.items())
    df_p = len(by_p) - 1

    ss_res = sum((w - pos_means[o[1]]) ** 2 for w, o in zip(within, obs))
    df_r = N - df_s - df_p - 1
    if df_r <= 0:
        return None

    ms_s, ms_p, ms_r = ss_s / df_s, ss_p / max(df_p, 1), ss_res / df_r
    m_bar = N / len(by_s)
    # 近似方差分量（不平衡设计，取平均簇大小）
    var_s = max((ms_s - ms_r) / m_bar, 0.0)
    var_p = max((ms_p - ms_r) / m_bar, 0.0)
    var_r = ms_r
    tot = var_s + var_p + var_r
    return dict(N=N, ks=len(by_s), kp=len(by_p), m=m_bar,
                var_s=var_s, var_p=var_p, var_r=var_r, tot=tot,
                ms_s=ms_s, ms_p=ms_p, ms_r=ms_r,
                pos_means=pos_means, grand=grand)


def main():
    obs = load()
    if len(obs) < 100:
        print("样本不足")
        return 1
    r = decompose(obs)
    if not r:
        print("自由度不足，无法分解")
        return 1

    print(f"观测 {r['N']}   场次 {r['ks']}   位置种类 {r['kp']}   平均每场 {r['m']:.1f} 轮")
    print(f"总均值 {r['grand']:.3f}")
    print()
    print("方差分解（嵌套：场次 ⊃ 位置）")
    print(f"  {'来源':<24}{'方差分量':>11}{'占比':>9}")
    print("  " + "-" * 46)
    for label, v in (("场次（候选人/简历/项目）", r["var_s"]),
                     ("轮次位置（追问深度）", r["var_p"]),
                     ("残余（模型/评判随机性）", r["var_r"])):
        print(f"  {label:<24}{v:>11.4f}{v / r['tot']:>8.1%}")
    print(f"  {'合计':<24}{r['tot']:>11.4f}{1.0:>8.0%}")

    print()
    print("信噪比（你真正要改的那部分，除以你必须忍的那部分）")
    snr = r["var_s"] / r["var_r"] if r["var_r"] else float("inf")
    print(f"  场次效应 / 残余 = {snr:.1f}")
    print(f"  → 同一份数据里，'换了个人'带来的差异是'模型抖动'的 {snr:.0f} 倍。")
    print()
    print("可操作结论：")
    print(f"  1) A/B 必须先按**场次**取均值再比较，否则场次方差会淹没 prompt 的真实效果。")
    print(f"  2) 位置效应占 {r['var_p'] / r['tot']:.1%}，说明'第几问'本身有系统偏差——")
    print(f"     两版 prompt 若轮次数不同，均值不可直接比。")
    print(f"  3) 残余只占 {r['var_r'] / r['tot']:.1%}，天花板不高：")
    print(f"     在这批数据上，任何小于 {2 * (r['var_r'] / max(r['ks'], 1)) ** 0.5:.2f} 分的差异都不该当成信号。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

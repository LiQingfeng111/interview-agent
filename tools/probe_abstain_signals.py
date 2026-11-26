"""W1 可行性探针：模型侧的不确定信号，能不能预测"这轮评分低"。

只回答两个问题，不建框架：
  Q1 日志里现成的廉价信号，对"这轮确实差"有没有判别力（AUC）？
  Q2 误差是否沿轨迹累积（前一轮差 → 这一轮更可能差）？
      Q2 是方向 A 的核心假设，Q2 不成立整个课题就该换方向。

纯标准库；AUC 手算（Mann-Whitney U），方向统一为"预测差轮"。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "runs")
DIMS = ("accuracy", "depth", "tradeoff", "boundary", "clarity")


def norm(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()\-—…]+", "", s or "")


def load_rows():
    """一行 = 一个 (场次, 轮次)，带可算的信号与结果。"""
    rows = []
    for path in sorted(glob.glob(os.path.join(RUNS, "run_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue
        run = os.path.basename(path)
        prev_low = None
        for t in j.get("turns") or []:
            ev = t.get("evaluation") or {}
            if not ev:
                continue
            scores = [ev[d] for d in DIMS if isinstance(ev.get(d), (int, float))]
            if not scores:
                continue
            ans = str(t.get("answer") or "")
            evidence = str(ev.get("evidence") or "")
            core = norm(evidence).strip("…")
            avg = sum(scores) / len(scores)
            rows.append({
                "run": run,
                "idx": t.get("idx"),
                "avg": avg,
                "min": min(scores),
                "has_evidence": bool(core),
                "evidence_ok": bool(core) and core in norm(ans),
                "conflict": bool(ev.get("verdict_conflict")),
                "fact_bad": str(ev.get("fact_status") or "") in ("unverifiable", "false"),
                "short": len(ans) < 40,
                "prev_low": prev_low,        # 上一轮是否落在低分位（用全局分位回填）
                "prev_avg": None,
            })
            prev_low = avg
        # 回填 prev_avg
        by_run = {}
        for r in rows:
            by_run.setdefault(r["run"], []).append(r)
        for lst in by_run.values():
            for i in range(1, len(lst)):
                lst[i]["prev_avg"] = lst[i - 1]["avg"]
            if lst:
                lst[0]["prev_avg"] = None
    return rows


def auc_bad(sig_bad, sig_good):
    """P(bad 组信号 > good 组信号)：>0.5 表示信号越大越可能是差轮。"""
    if not sig_bad or not sig_good:
        return None
    wins = 0.0
    for b in sig_bad:
        for g in sig_good:
            if b > g:
                wins += 1
            elif b == g:
                wins += 0.5
    return wins / (len(sig_bad) * len(sig_good))


def main():
    rows = load_rows()
    n = len(rows)
    if not n:
        print("没有可解析的轮次，先补跑数据")
        return 1

    scores = sorted(r["avg"] for r in rows)
    cut = scores[max(0, int(n * 0.2) - 1)]          # 20 分位：相对基线，不是绝对阈值
    for r in rows:
        r["low"] = r["avg"] <= cut
    bad = [r for r in rows if r["low"]]
    good = [r for r in rows if not r["low"]]

    print(f"轮次样本 {n}   场次 {len({r['run'] for r in rows})}")
    print(f"低分判据 avg <= {cut:.2f}（20 分位）→ 差轮 {len(bad)} / 正常 {len(good)}")
    print()

    def show(label, fn):
        sb = [1.0 if fn(r) else 0.0 for r in bad]
        sg = [1.0 if fn(r) else 0.0 for r in good]
        a = auc_bad(sb, sg)
        pb = sum(sb) / len(sb)
        pg = sum(sg) / len(sg)
        verdict = "有判别力" if a and abs(a - 0.5) >= 0.10 else "接近随机"
        print(f"  {label:<26}{pb:>7.1%}{pg:>9.1%}{a:>9.3f}   {verdict}")

    print(f"  {'信号':<24}{'差轮':>8}{'正常':>9}{'AUC':>9}")
    print("  " + "-" * 58)
    show("答案 < 40 字（信息量）", lambda r: r["short"])
    show("evidence 缺失", lambda r: not r["has_evidence"])
    show("evidence 不可核验（假证据）", lambda r: not r["evidence_ok"])
    show("verdict 冲突", lambda r: r["conflict"])
    show("事实不可核验", lambda r: r["fact_bad"])

    # ---- Q2：误差是否累积 ----
    print()
    print("Q2 轨迹风险：上一轮差，这一轮还差的概率")
    pairs = [r for r in rows if r["prev_avg"] is not None]
    if pairs:
        base = sum(1 for r in pairs if r["low"]) / len(pairs)
        after_bad = [r for r in pairs if r["prev_avg"] <= cut]
        after_good = [r for r in pairs if r["prev_avg"] > cut]
        p_bad = sum(1 for r in after_bad if r["low"]) / len(after_bad) if after_bad else 0
        p_good = sum(1 for r in after_good if r["low"]) / len(after_good) if after_good else 0
        print(f"  相邻轮对 {len(pairs)}   基准低分率 {base:.1%}")
        print(f"  前一轮差 (n={len(after_bad):>4}) → 本轮差 {p_bad:>6.1%}")
        print(f"  前一轮好 (n={len(after_good):>4}) → 本轮差 {p_good:>6.1%}")
        lift = (p_bad / base - 1) if base else 0
        print(f"  lift = {lift:+.1%}   {'→ 误差在累积，A 的核心假设成立' if lift > 0.3 else '→ 误差不累积，A 的前提不成立，考虑转方向 B'}")
    else:
        print("  场次内轮次不足，无法估计转移概率")

    # ---- Q3：同一条样本上"信号"和"排名"的关系是否单调 ----
    print()
    print("结论怎么用：")
    print("  只有 AUC ≥ 0.65 且 lift > 30% 时，才值得把 6 周投进方向 A；")
    print("  否则这一周就止损，切到方向 B（轨迹回归），成本损失 <= 1 周。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

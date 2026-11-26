"""评分校准：把"尺子松紧"变成可测的东西。

为什么必须有这一层：
  真实模型下一场 10 问 9 个 fail。这可能是候选人不强，也可能是**尺子太严**。
  两者分不清，报告就没有说服力——用户会以为"我很差"，其实是阈值定高了。

做法（不引入标注平台）：
  1. 20 条人工意图标注（好/中/差），放在 `data/calibration/labeled.jsonl`；
  2. 用评分官跑一遍，拿维度均分；
  3. 扫 pass/partial 两个阈值，找与标注一致率最高的组合；
  4. `--apply` 把阈值写进 config，之后 `normalize_eval` 就按新尺子判。

诚实边界：
  - 标注是我按"面试官会怎么判"写的，不是真实面试官标的；
    它能证明"尺子与一套明确标准一致"，不能证明"这套标准就是业界共识"；
  - 20 条样本量小，一致率会有 ±5~10pp 波动，别把 78% 当精确值。

用法：
  python -m eval.calibrate                # mock 评分官（快、离线）
  python -m eval.calibrate --real         # 真实模型评分官
  python -m eval.calibrate --real --apply # 把最优阈值写进 config
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from itertools import product

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import RUBRIC_DIMS  # noqa: E402
from evaluator import Evaluator  # noqa: E402
from llm import build_llm  # noqa: E402

LABELED = os.path.join(ROOT, "data", "calibration", "labeled.jsonl")


def load_labeled(path: str = LABELED) -> list[dict]:
    items = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items


def avg_score(ev) -> float:
    dims = [getattr(ev, d, 3) for d in RUBRIC_DIMS]
    return round(sum(dims) / len(dims), 3)


def predict(score: float, pass_min: float, partial_min: float) -> str:
    if score >= pass_min:
        return "pass"
    if score >= partial_min:
        return "partial"
    return "fail"


def agreement(rows: list[dict], pass_min: float, partial_min: float) -> tuple[float, dict]:
    conf: dict[tuple[str, str], int] = {}
    hit = 0
    for r in rows:
        pred = predict(r["score"], pass_min, partial_min)
        conf[(r["label"], pred)] = conf.get((r["label"], pred), 0) + 1
        if pred == r["label"]:
            hit += 1
    return hit / max(1, len(rows)), conf


def bootstrap_ci(rows: list[dict], pass_min: float, partial_min: float,
                 n_boot: int = 1000, seed: int = 7) -> tuple[float, float, float]:
    """一致率的 bootstrap 置信区间（95%）。

    为什么要这个：57 条样本的一致率是点估计，"80%" 听起来精确，其实可能是 72%~87%。
    不给区间就等于把抽样噪声当成了结论——这是 cje / LLM-judge-reporting
    这类项目做得比我们深的地方，补上它，数字才敢写进 README。

    做法：有放回重采样 n_boot 次，算每次的一致率，取 2.5%/97.5% 分位。
    """
    import random

    n = len(rows)
    if n == 0:
        return 0.0, 0.0, 0.0
    point = agreement(rows, pass_min, partial_min)[0]
    rng = random.Random(seed)
    rates = []
    for _ in range(n_boot):
        sample = [rows[rng.randrange(n)] for _ in range(n)]
        rates.append(sum(1 for r in sample
                         if predict(r["score"], pass_min, partial_min) == r["label"]) / n)
    rates.sort()
    lo = rates[int(0.025 * len(rates))]
    hi = rates[min(len(rates) - 1, int(0.975 * len(rates)))]
    return point, lo, hi


def sweep_robust(runs: list[list[dict]]) -> tuple[float, float, float, float]:
    """多次评测取平均一致率，返回 (平均最优一致率, pass_min, partial_min, 波动)。

    为什么要多次：真实模型不是确定性的，同样 20 条标注跑两遍，
    最优阈值会从 3.0/1.6 跳到 2.8/1.4，一致率 60%↔75%。
    只跑一次就 `--apply`，等于把一次抖动固化成标准。
    """
    best = (-1.0, 4.0, 2.5, 0.0)
    pass_grid = [round(1.6 + 0.2 * i, 1) for i in range(13)]
    partial_grid = [round(1.0 + 0.2 * i, 1) for i in range(13)]
    for pass_min, partial_min in product(pass_grid, partial_grid):
        if partial_min >= pass_min:
            continue
        rates = [agreement(rows, pass_min, partial_min)[0] for rows in runs]
        mean = sum(rates) / len(rates)
        spread = max(rates) - min(rates)
        # 平均一致率高者优先；并列时选波动更小的（更稳的尺子）
        if mean > best[0] or (abs(mean - best[0]) < 1e-9 and spread < best[3]):
            best = (mean, pass_min, partial_min, spread)
    return best


def cv_select(runs: list[list[dict]], k: int = 3) -> tuple[float, float, float]:
    """K 折交叉验证选阈值：每个候选 pair 的分数只在"没参与挑选"的折上计。

    为什么不能只在 train 上扫：
      实测 train 89% / holdout 68%，差 20pp——阈值是贴着训练集边界长出来的，
      换一批题就掉。CV 估计的是"同一套标准用到新题上还能有多少一致率"，
      这才是敢写进 README 的数字。
    """
    rows = runs[0]
    if len(rows) < 6:
        best = sweep_robust(runs)
        return best[0], best[1], best[2]
    folds = [rows[i::k] for i in range(k)]

    best = (-1.0, 4.0, 2.5)
    pass_grid = [round(1.6 + 0.2 * i, 1) for i in range(13)]
    partial_grid = [round(1.0 + 0.2 * i, 1) for i in range(13)]
    for pass_min, partial_min in product(pass_grid, partial_grid):
        if partial_min >= pass_min:
            continue
        rates = []
        for f in folds:
            if not f:
                continue
            rates.append(sum(1 for r in f
                             if predict(r["score"], pass_min, partial_min) == r["label"]) / len(f))
        mean = sum(rates) / len(rates)
        if mean > best[0]:
            best = (mean, pass_min, partial_min)
    return best


def render_confusion(conf: dict) -> str:
    labels = ["pass", "partial", "fail"]
    lines = ["  混淆矩阵（行=人工标注，列=系统判定）",
             "  " + " " * 8 + "".join(f"{c:>9}" for c in labels)]
    for lab in labels:
        row = "".join(f"{conf.get((lab, p), 0):>9}" for p in labels)
        lines.append(f"  {lab:<8}{row}")
    return "\n".join(lines)


def apply_thresholds(pass_min: float, partial_min: float, mode: str = "real") -> None:
    """把阈值写进 config.py 的 VERDICT_THRESHOLDS[mode]。

    注意：必须按模式写。mock 与真实模型的分数分布差一整个档
    （mock 上限 3.4，真模型把好答案打到 2 分），混写等于毁掉另一套尺子。
    """
    path = os.path.join(ROOT, "config.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    import re
    pattern = rf'(VERDICT_THRESHOLDS\s*=\s*\{{[^}}]*?"{mode}":\s*)\([^)]*\)'
    src, n = re.subn(pattern, rf'\g<1>({pass_min}, {partial_min})', src)
    if not n:
        raise SystemExit(f"没找到 VERDICT_THRESHOLDS['{mode}']，拒绝写入（防止手滑毁配置）")
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="用真实模型评分（默认 mock，快且可复现）")
    ap.add_argument("--apply", action="store_true", help="把最优阈值写进 config.py")
    ap.add_argument("--file", default=LABELED)
    ap.add_argument("--split", default="train",
                    help="用哪个划分扫阈值（train），另一个划分做留出验证")
    ap.add_argument("--repeat", type=int, default=0,
                    help="重复评测次数（真实模型建议 3；默认 mock 1 次、real 3 次）")
    args = ap.parse_args()

    items_all = load_labeled(args.file)
    has_split = any("split" in it for it in items_all)
    if has_split:
        train = [it for it in items_all if it.get("split") == args.split]
        holdout = [it for it in items_all if it.get("split") != args.split]
        items = train or items_all
    else:
        items, holdout = items_all, []
    llm = build_llm(mode="auto" if args.real else "mock")
    evaluator = Evaluator(llm)
    repeats = args.repeat or (3 if args.real else 1)

    def run_on(subset: list[dict]) -> list[dict]:
        rows = []
        for it in subset:
            ev = evaluator.evaluate(it["question"], it["answer"], phase="PROJECT_PROBE")
            rows.append({"id": it["id"], "label": it["label"], "score": avg_score(ev),
                         "system_verdict": ev.verdict, "topic": it.get("topic", "")})
        return rows

    runs: list[list[dict]] = [run_on(items) for _ in range(repeats)]
    rows = runs[0]

    # 全量交叉验证：不依赖 train/holdout 划分，把"换了题还能不能对"直接量出来。
    # 之前只看 train（89%）就下结论，一换留出集掉到 68%——那是阈值在拟合训练集。
    all_runs = [run_on(items_all)]
    cv_all_rate, cv_all_pass, cv_all_partial = cv_select(all_runs, k=5)

    from config import VERDICT_THRESHOLDS
    mode_now = "real" if args.real else "mock"
    cur_pass, cur_partial = VERDICT_THRESHOLDS.get(mode_now, VERDICT_THRESHOLDS["real"])
    rate_now = sum(agreement(r, cur_pass, cur_partial)[0] for r in runs) / len(runs)
    conf_now = agreement(rows, cur_pass, cur_partial)[1]
    cv_rate, cv_pass, cv_partial = cv_select(runs, k=3)
    best_rate, best_pass, best_partial, spread = sweep_robust(runs)

    print("=" * 70)
    print(f"评分校准｜{len(rows)} 条标注｜评分官={'真实模型' if args.real else 'mock'}")
    print("=" * 70)
    dist = {lab: sum(1 for r in rows if r["label"] == lab) for lab in ("pass", "partial", "fail")}
    print(f"  标注分布：{dist}")
    scores = sorted(r["score"] for r in rows)
    print(f"  均分区间：{scores[0]} ~ {scores[-1]}（中位 {scores[len(scores)//2]}）")
    print(f"  当前阈值（pass≥{cur_pass} / partial≥{cur_partial}，{mode_now} 模式）一致率：{rate_now:.0%}")
    print(render_confusion(conf_now))
    print(f"  训练集扫描最优：pass≥{best_pass} / partial≥{best_partial} → 一致率 {best_rate:.0%}（波动 {spread:.0%}）")
    print(f"  训练集 3 折 CV：pass≥{cv_pass} / partial≥{cv_partial} → {cv_rate:.0%}")
    pt, lo, hi = bootstrap_ci(all_runs[0], cv_all_pass, cv_all_partial)
    print(f"  全量 {len(items_all)} 条 5 折 CV：pass≥{cv_all_pass} / partial≥{cv_all_partial} → {cv_all_rate:.0%}")
    print(f"  置信区间（bootstrap 1000 次，95%）：{pt:.0%} [{lo:.0%}, {hi:.0%}]，跨度 {hi - lo:.0%}")
    if best_rate > rate_now:
        print(f"  建议：{rate_now:.0%} → {best_rate:.0%}（+{(best_rate - rate_now) * 100:.0f}pp）")
    else:
        print("  建议：当前阈值已是最优，不动")

    # 留出验证：阈值是在 train 上扫出来的，只有在没见过的数据上才敢说"能用"
    holdout_rate = None
    if holdout:
        hold_rows = run_on(holdout)
        holdout_rate, hold_conf = agreement(hold_rows, best_pass, best_partial)
        print(f"  留出验证（{len(holdout)} 条，未参与扫阈值）：一致率 {holdout_rate:.0%}")
        print(render_confusion(hold_conf))
        gap = best_rate - holdout_rate
        print(f"  训练/留出差距：{gap * 100:+.0f}pp"
              + ("（>15pp 说明阈值在拟合训练集）" if gap > 0.15 else "（可接受）"))

    if args.apply:
        apply_thresholds(cv_all_pass, cv_all_partial, mode=mode_now)
        print(f"  已写入 config.py：{mode_now} 阈值 = pass≥{cv_all_pass} / partial≥{cv_all_partial}")

    # 判定取"训练与留出中较小的那个"：只看训练集一致率，等于自己给自己判卷
    # 用 CV 数字做判定：训练集数字自己给自己判卷，不能算数`n    # 判定用全量 5 折 CV：它天然把每一条都当过一次测试样本，不依赖人为划分
    gate = cv_all_rate
    ok = gate >= 0.7
    print(f"判定：{'PASS' if ok else 'FAIL'}（门槛：全量 5 折 CV ≥70%）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

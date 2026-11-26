"""题库健康检查：题量、字段完整性、话题覆盖、时效。

为什么单独一个检查：
  "题库会随时更新吗"这类问题，只靠人记是不行的。
  把判据写成脚本，才知道现在到底是"54 道题、5 个话题全覆盖、今天刚更新"
  还是"12 道题、3 个话题、半年前的老题"。

用法：
  python eval/check_bank.py            # 人类可读报告
  python eval/check_bank.py --json     # 机器可读
退出码：0 健康，1 不健康（缺题/字段错/过期）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bank import LEVELS, bank_stats, load_bank, render_bank_report, validate_bank
from config import ROLE_KB
from knowledge import role_topics

MIN_PER_ROLE = 40         # 每个岗位至少 40 题（三档难度各十几题才够抽）
MIN_POINTS_RATIO = 0.8    # 带期望要点的题占比：没有 points 评分只能靠感觉


def check() -> dict:
    items = load_bank()
    errors, warnings = validate_bank(items)
    stats = bank_stats(items)

    problems: list[str] = list(errors)
    for role in ROLE_KB:
        n = stats["by_role"].get(role, 0)
        if n < MIN_PER_ROLE:
            problems.append(f"{role} 只有 {n} 题（下限 {MIN_PER_ROLE}）")
        covered = stats["topics"].get(role, 0)
        total_topics = len(role_topics(role))
        if covered < total_topics:
            problems.append(f"{role} 话题覆盖 {covered}/{total_topics}")
    for lv in LEVELS:
        if stats["by_level"].get(lv, 0) == 0:
            problems.append(f"缺少难度层级：{lv}")
    if stats["stale"]:
        problems.append(f"题库已过期：最旧一条 {stats['oldest_days']} 天（阈值 {stats['stale_days']}）")

    with_points = sum(1 for q in items if q.get("points"))
    ratio = with_points / max(1, len(items))
    if ratio < MIN_POINTS_RATIO:
        problems.append(
            f"只有 {ratio:.0%} 的题带期望要点（下限 {MIN_POINTS_RATIO:.0%}），"
            f"其余题评分会退化成靠感觉"
        )

    by_source: dict[str, int] = {}
    for q in items:
        by_source[q.get("source", "?")] = by_source.get(q.get("source", "?"), 0) + 1

    unreviewed = sum(1 for q in items if not q.get("reviewed"))
    reviewed_ratio = (len(items) - unreviewed) / max(1, len(items))
    if unreviewed:
        # AI 审核是主力（tools/ai_review.py），这里只提示，不判定失败——
        # 因为"审核没跑完"和"题库坏了"是两件事，不该用同一个红灯。
        warnings.append(
            f"{unreviewed} 条未审核（已审核 {reviewed_ratio:.0%}）："
            f"跑 python tools/ai_review.py --all --apply"
        )

    return {
        "ok": not problems,
        "stats": stats,
        "problems": problems,
        "warnings": warnings,
        "unreviewed": unreviewed,
        "points_ratio": round(ratio, 3),
        "reviewed_ratio": round(reviewed_ratio, 3),
        "by_source": by_source,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = check()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    print("=" * 70)
    print("题库健康检查")
    print("=" * 70)
    print(render_bank_report(load_bank()))
    print(f"  来源分布：{result['by_source']}")
    print(f"  带期望要点的比例：{result['points_ratio']:.0%} · 已审核：{result['reviewed_ratio']:.0%}")
    print()
    if result["problems"]:
        print("不达标：")
        for p in result["problems"]:
            print("  ✗ " + p)
    else:
        print("  ✓ 题量、字段、话题覆盖、时效均达标")
    for w in result["warnings"]:
        print("  ! " + w)
    print()
    print("判定：" + ("PASS" if result["ok"] else "FAIL"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

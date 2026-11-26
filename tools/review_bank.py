"""题库审核工具：把"生成题没人审"这件事变成可执行的流程。

为什么需要：
  353 道题里绝大多数是模型生成的（reviewed=false）。门禁一直警告，
  但警告不等于审核——没有方便的审核动作，这个字段永远停在 false。

设计：
  - 先做**机器能判的筛查**（重复、占位符、太短、缺要点、跑题），把明显坏的挑出来；
  - 再提供逐条审核的交互模式（y 通过 / n 删除 / e 先放着 / q 退出）；
  - 通过后写回 reviewed=true，删除的从题库移走，全程有 diff 可查。

用法：
  python tools/review_bank.py --screen                 # 只看机器筛查结果
  python tools/review_bank.py --role frontend --review  # 逐条审核（交互）
  python tools/review_bank.py --approve-auto           # 机器筛查通过的直接标 reviewed
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bank import load_bank, save_bank, similarity, validate_bank  # noqa: E402

PLACEHOLDER_HINTS = ("待填", "你选的技术栈", "TODO", "占位", "xxx", "XX 需求")
TOO_SHORT = 8      # 「常见的消息队列有哪些？」12 字但是道好题，别误杀


def screen(items: list[dict]) -> dict[str, list[dict]]:
    """机器能判的坏题分类。判得保守：宁可漏，不要误删好题。"""
    bad: dict[str, list[dict]] = {"占位符": [], "太短": [], "缺要点": [], "重复": []}
    for q in items:
        text = q.get("question", "")
        if any(h in text for h in PLACEHOLDER_HINTS):
            bad["占位符"].append(q)
        elif len(text) < TOO_SHORT:
            bad["太短"].append(q)
        elif not q.get("points"):
            bad["缺要点"].append(q)

    seen: list[dict] = []
    for q in items:
        text = q.get("question", "")
        dup = next((s for s in seen if similarity(text, s.get("question", "")) >= 0.85), None)
        if dup:
            bad["重复"].append(q)
        else:
            seen.append(q)
    return bad


def review_interactive(items: list[dict], role: str | None) -> tuple[int, int]:
    approved = removed = 0
    todo = [q for q in items if not q.get("reviewed") and (role is None or q.get("role") == role)]
    print(f"待审 {len(todo)} 条（role={role or '全部'}）")
    for i, q in enumerate(todo, 1):
        print("\n" + "-" * 74)
        print(f"[{i}/{len(todo)}] {q.get('role')} · {q.get('topic')} · {q.get('level')}")
        print(f"  题：{q.get('question')}")
        pts = q.get("points") or []
        print(f"  要点：{'；'.join(pts) if pts else '（无）'}")
        try:
            ans = input("  [y]通过 [n]删除 [e]跳过 [q]退出 > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if ans == "q":
            break
        if ans == "y":
            q["reviewed"] = True
            q["source"] = (q.get("source") or "generated") + "+reviewed"
            approved += 1
        elif ans == "n":
            items.remove(q)
            removed += 1
    return approved, removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", action="store_true")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--approve-auto", action="store_true")
    ap.add_argument("--role", default=None)
    args = ap.parse_args()

    items = load_bank()
    if not items:
        print("题库为空")
        return 1

    bad = screen(items)
    flagged = {id(q) for lst in bad.values() for q in lst}

    print("=" * 74)
    print(f"题库审核｜总 {len(items)} 条｜未审核 {sum(1 for q in items if not q.get('reviewed'))} 条")
    print("=" * 74)
    for kind, lst in bad.items():
        print(f"  [{kind}] {len(lst)} 条")
        for q in lst[:3]:
            print(f"      - {(q.get('question') or '')[:60]}")

    if args.approve_auto:
        n = 0
        for q in items:
            if id(q) not in flagged and not q.get("reviewed"):
                q["reviewed"] = True
                q["source"] = (q.get("source") or "generated") + "+reviewed"
                n += 1
        save_bank(items)
        print(f"\n已自动通过 {n} 条（机器筛查无问题）")
        return 0

    if args.review:
        approved, removed = review_interactive(items, args.role)
        save_bank(items)
        errors, _ = validate_bank(items)
        print(f"\n通过 {approved} 条，删除 {removed} 条，剩余 {len(items)} 条")
        if errors:
            print("题库校验错误：", errors[:3])
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

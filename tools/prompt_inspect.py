"""提示词槽位诊断：把"这次请求到底带了哪些上下文"变成可打印的东西。

为什么需要它：模板化的真正收益不是"代码变短"，而是**每个变量都有名字**。
有了名字，就能回答这些以前只能打断点才知道的问题：

- 第 5 题的 prompt 里，checklist 那段到底在不在？
- 上一问是 partial，retry 指令有没有真的插进去？
- JD 关键词为空时，是不是只是没插那两行，而不是插了两行空字符串？
- 两个阶段的 prompt 长度差多少，谁在吃 token？

用法：
    python tools/prompt_inspect.py                 # 9 条内置样例
    python tools/prompt_inspect.py --json          # 机器可读
    python tools/prompt_inspect.py --case tech_bank
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import prompt_templates  # noqa: E402

SLOT_ORDER = {
    "PROJECT_PROBE": ["ladder", "project", "followup", "retry", "checklist", "seed", "recent", "asked"],
    "TECH_QA": ["topic", "bank", "points", "seed", "jd_keywords", "jd_hint", "ask", "web"],
    "OTHER": ["guide"],
}


def describe(name: str, sm, hint: dict, jd: list[str], web: str) -> dict:
    """返回这条用例的槽位明细：谁有内容、谁为空、占多少字符。"""
    if sm.phase == "PROJECT_PROBE":
        slots = prompt_templates.probe_slots(sm, hint, jd_keywords=jd, web_refs=web)
        order = SLOT_ORDER["PROJECT_PROBE"]
    elif sm.phase == "TECH_QA":
        slots = prompt_templates.tech_slots(sm, hint, jd_keywords=jd, web_refs=web)
        order = SLOT_ORDER["TECH_QA"]
    else:
        slots = prompt_templates.simple_slots(sm, hint)
        order = SLOT_ORDER["OTHER"]

    text = prompt_templates.build_question_text(sm, hint, jd_keywords=jd, web_refs=web)
    rows = []
    for key in order:
        value = slots.get(key, "")
        rows.append({
            "slot": key,
            "filled": bool(value),
            "chars": len(value),
            "preview": (value.strip().replace("\n", " / ")[:60] if value else ""),
        })
    return {
        "case": name,
        "phase": sm.phase,
        "total_chars": len(text),
        "slots": rows,
        "empty_slots": [r["slot"] for r in rows if not r["filled"]],
        "text": text,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default="", help="只看某一条")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    from tests.test_prompt_templates import CASES  # 复用同一批样例，避免两套输入漂移

    names = [args.case] if args.case else list(CASES)
    out = []
    for name in names:
        if name not in CASES:
            print(f"没有这条用例：{name}（可选：{', '.join(CASES)}）", file=sys.stderr)
            return 2
        sm, hint, jd, web = CASES[name]
        out.append(describe(name, sm, hint, jd, web))

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    for row in out:
        print(f"\n=== {row['case']} · {row['phase']} · {row['total_chars']} 字符 ===")
        for s in row["slots"]:
            mark = "✓" if s["filled"] else "·"
            print(f"  {mark} {s['slot']:<12} {s['chars']:>4} 字符  {s['preview']}")
        if row["empty_slots"]:
            print(f"  空槽位：{', '.join(row['empty_slots'])}（这些内容本次不需要）")
    print(f"\n合计 {len(out)} 条，平均 {sum(r['total_chars'] for r in out) // max(1, len(out))} 字符")
    return 0


if __name__ == "__main__":
    sys.exit(main())

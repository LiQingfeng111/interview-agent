"""题库生成器：用真实模型按「话题 × 难度」批量出题，落成可编辑 JSONL。

为什么是"生成 + 人工在环"而不是纯手写：
  - 手写几百道有质量的题，一个人要写几周，还会漏话题；
  - 纯生成没人看，质量不可控（模型会编出"LangGraph 10"这种不存在的版本）。
  所以：生成 → 标 source=generated / reviewed=false → 门禁里警告未审核 →
  人工改过之后把 reviewed 置 true。

为什么按「话题 × 难度」拆调用：
  一次要一个岗位 15 题时，模型 1200 token 预算里 1000 花在 reasoning 上，
  正文被截断成半个 JSON，解析直接失败。拆到一个话题一个层级 4 题后成功率很高。

用法：
  python tools/gen_bank.py --role all                # 18 话题 × 3 层级 × 4 题
  python tools/gen_bank.py --role agent_app --per-level 3
  python tools/gen_bank.py --role all --dry          # 只看现有题库统计
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bank import (  # noqa: E402
    BANK_PATH,
    LEVELS,
    dedupe,
    load_bank,
    render_bank_report,
    save_bank,
    validate_bank,
)
from config import ROLE_KB  # noqa: E402
from knowledge import role_topics, topic_entry  # noqa: E402
from llm import build_llm  # noqa: E402

SYSTEM = """你是 AI 岗位面试题库编写者。给定话题和难度层级，写 N 道同层级的题。
要求：
- 题目必须具体、可验证，禁止"谈谈你对X的理解"这类开放式废话。
- 每道题问不同的侧面，不要换词重复。
- points 写 2-3 条"答到才算过"的要点，要具体（关键词或数字方向）。
- 不要编造不存在的版本号或论文。
- 直接输出 JSON 数组，不要解释、不要 markdown 代码块。元素形如：
  {"question":"...","points":["...","..."]}
"""

LEVEL_DESC = {
    "basic": "基础概念：定义、区别、为什么需要它",
    "advanced": "进阶：工程实现细节、参数选择、线上问题定位",
    "deep": "深入：边界、失效条件、取舍、反事实设计",
}


def _extract_json_array(text: str) -> list[dict]:
    if not text:
        return []
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)]


def gen_level(llm, role: str, topic: str, level: str, n: int = 4) -> list[dict]:
    entry = topic_entry(role, topic)
    angles = entry[1] if entry else []
    user = (
        f"岗位：{ROLE_KB[role].get('name', role)}\n"
        f"话题：{topic}\n"
        f"难度层级：{level}（{LEVEL_DESC[level]}）\n"
        f"可用追问角度：{'、'.join(angles)}\n\n"
        f"写 {n} 道该层级的题，只输出 JSON 数组。"
    )
    try:
        raw = llm.chat(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            task="bank_gen",
        )
    except Exception as e:
        print(f"      [warn] {topic}/{level} 失败：{e}")
        return []

    items = _extract_json_array(raw)
    today = time.strftime("%Y-%m-%d")
    out = []
    for i, it in enumerate(items, 1):
        q = str(it.get("question", "")).strip()
        if not q:
            continue
        out.append({
            "id": f"{role}-{abs(hash(topic)) % 10000:04d}-{level}-{i:02d}",
            "role": role,
            "topic": topic,
            "level": level,
            "question": q,
            "points": [str(p).strip() for p in (it.get("points") or []) if str(p).strip()][:3],
            "tags": [level],
            "source": "generated",
            "reviewed": False,
            "updated_at": today,
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="agent_app", help="岗位名，或 all")
    ap.add_argument("--per-level", type=int, default=4, help="每个 话题×难度 生成几题")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    existing = load_bank()
    if args.dry:
        print(render_bank_report(existing))
        return 0

    roles = list(ROLE_KB) if args.role == "all" else [args.role]
    llm = build_llm(mode="auto")
    if getattr(llm, "cfg", None) is not None:
        llm.cfg.max_tokens = 3000

    added: list[dict] = []
    for role in roles:
        print(f"[gen] {role}")
        for topic in role_topics(role):
            line = []
            for level in LEVELS:
                got = gen_level(llm, role, topic, level, args.per_level)
                line.append(f"{level}:{len(got)}")
                added.extend(got)
            print(f"      {topic:<14} {' '.join(line)}")

    merged = existing + added
    errors, warnings = validate_bank(merged)
    # 先去重再校验：扩题量时最容易出现"换个说法问同一件事"
    merged, dropped = dedupe(merged)
    errors, warnings = validate_bank(merged)
    save_bank(merged)

    print(f"\n[bank] 新增 {len(added)} 条，去重丢弃 {dropped} 条")
    print(f"[bank] 已写入 {BANK_PATH}")
    print(render_bank_report(merged))
    if errors:
        print("[bank] 错误：")
        for e in errors[:5]:
            print("  - " + e)
    if warnings:
        print(f"[bank] 警告 {len(warnings)} 条（示例）：")
        for w in warnings[:3]:
            print("  - " + w)
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

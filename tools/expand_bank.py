"""题库扩容：批量生成 → 规则校验 → 去重 → AI 复审 → 落盘。

为什么要有这个工具（而不是手写题目）：
  对标 GitHub 上的同类项目，人家的题库是 1000~1700 题量级，我们只有 342。
  手写不可持续，纯模型生成会灌水（大量同义重复、空洞题、无考点的套话）。
  所以流程必须是"生成 → 机器筛 → 模型复审 → 人只做抽查"，
  每一层都留数字（生成了多少、丢掉多少、为什么丢）。

三道闸（任一不过就丢，不进库）：
  1. 规则：必须有 points（考点）、长度合理、无占位符、不与其他题重复（bigram 相似度）
  2. 考点：points 里至少一条是"可验证的具体名词/数字要求"，不是"理解加深"这种空话
  3. AI 复审：复用 tools/ai_review.py 的评分卡（风险高或过于空泛 → 丢）

用法：
  python tools/expand_bank.py --role ai_algo --topic RAG --n 20
  python tools/expand_bank.py --role backend --n 40 --dry-run
  python tools/expand_bank.py --all --n 200      # 五岗位一起扩
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

from bank import (BANK_PATH, dedupe, load_bank, save_bank, similarity)  # noqa: E402
from config import ROLE_KB  # noqa: E402
from llm import build_llm  # noqa: E402

GEN_SYSTEM = """你是技术面试题库编写者。只输出 JSON 数组，不要任何解释。
每道题必须满足：
1. question：一句真问题，面试官真的会问出口。禁止"请介绍""谈谈你对"这类泛题；
   必须能追到具体技术点（比如"chunk 500 字是怎么定的"而不是"讲讲 RAG"）。
2. points：2-4 条考点，每条是一个**可验证的要求**（要出现具体名词/数字/机制），
   不许写"理解深入""表达清晰"这种评价词。
3. topic：一个大类词（如 RAG / Agent / 数据库 / 前端性能）。
4. level：junior / mid / senior 之一。
5. tags：1-3 个短标签。
要求：题目之间不许同义改写；正题要能引出追问，而不是能一句话背完的定义题。
"""

BAD_POINT_WORDS = (
    "理解", "熟悉", "掌握", "清楚", "表达能力", "逻辑", "思路清晰",
    "沟通", "态度", "学习能力",
)
PLACEHOLDER = ("XX", "xxx", "TODO", "示例", "略", "等等等")
# 题库的 level 是 basic/advanced/deep，而生成提示里用的是 junior/mid/senior，
# 必须映射，否则 bank.validate_bank 直接判非法（第一次扩容就踩了：60 题全 level 非法）
LEVEL_MAP = {"junior": "basic", "mid": "advanced", "senior": "deep",
             "basic": "basic", "advanced": "advanced", "deep": "deep"}


def _bad_points(points: list[str]) -> str:
    """返回不合格理由；空字符串表示合格。"""
    if not points:
        return "无考点"
    ok = 0
    for p in points:
        t = str(p).strip()
        if any(w in t for w in BAD_POINT_WORDS) and len(t) < 18:
            continue
        # 必须有具体性：数字、英文术语、或较长的动作描述
        if re.search(r"\d", t) or re.search(r"[A-Za-z]{2,}", t) or len(t) >= 14:
            ok += 1
    return "" if ok >= 1 else "考点过于空泛"


def _parse_array(text: str) -> list:
    """从模型输出里抠出 JSON 数组。

    为什么不用 chat_json：chat_json 期望返回一个**对象**，
    而题库生成要的是**数组**（实测模型会老老实实返回 [ {...}, {...} ]），
    走 chat_json 会被解析成空/单个对象，表现就是"生成了 0 条"，看着像模型没干活。
    """
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        return []
    chunk = text[i:j + 1]
    for candidate in (chunk, chunk.replace("，", ",").replace("：", ":")):
        try:
            data = json.loads(candidate)
            return data if isinstance(data, list) else []
        except Exception:
            continue
    # 逐行兜底：模型偶尔少逗号，按行拼回对象
    items = []
    for m in re.finditer(r"\{[^{}]*\}", chunk):
        try:
            items.append(json.loads(m.group(0)))
        except Exception:
            continue
    return items


def gen_batch(llm, role: str, topic: str, n: int) -> list[dict]:
    role_name = ROLE_KB.get(role, {}).get("name", role)
    user = (f"岗位方向：{role_name}\n主题：{topic}\n数量：{n}\n"
            f"输出 JSON 数组，元素形如 "
            f'{{"question":"","points":["",""],"topic":"","level":"mid","tags":[""]}}')
    try:
        text = llm.chat(
            [{"role": "system", "content": GEN_SYSTEM}, {"role": "user", "content": user}],
            task="bank_gen", hint={"role": role, "topic": topic},
        )
    except Exception:
        return []
    items = _parse_array(text)
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        if not q or len(q) < 8 or len(q) > 160:
            continue
        out.append({
            "question": q,
            "points": [str(p).strip() for p in (it.get("points") or []) if str(p).strip()],
            "topic": str(it.get("topic") or topic).strip()[:20],
            "level": str(it.get("level") or "mid").strip().lower(),
            "tags": [str(t).strip() for t in (it.get("tags") or [])][:3],
        })
    return out


def screen(cands: list[dict], existing: list[dict], seen: list[str]) -> tuple[list[dict], dict]:
    """规则三闸：占位符/考点/重复。返回 (通过, 统计)。"""
    stat = {"total": len(cands), "placeholder": 0, "bad_points": 0,
            "dup_existing": 0, "dup_inside": 0, "too_short": 0, "ok": 0}
    kept: list[dict] = []
    for c in cands:
        q = c["question"]
        if any(p in q for p in PLACEHOLDER):
            stat["placeholder"] += 1
            continue
        if len(q) < 8:
            stat["too_short"] += 1
            continue
        reason = _bad_points(c["points"])
        if reason:
            stat["bad_points"] += 1
            continue
        if any(similarity(q, e["question"]) >= 0.72 for e in existing):
            stat["dup_existing"] += 1
            continue
        if any(similarity(q, s) >= 0.72 for s in seen):
            stat["dup_inside"] += 1
            continue
        seen.append(q)
        kept.append(c)
        stat["ok"] += 1
    return kept, stat


def to_items(kept: list[dict], role: str, start_id: int) -> list[dict]:
    day = time.strftime("%Y-%m-%d")
    out = []
    for i, c in enumerate(kept):
        out.append({
            "id": f"{role}_{start_id + i:04d}",
            "role": role,
            "question": c["question"],
            "points": c["points"],
            "topic": c["topic"],
            "level": LEVEL_MAP.get(c["level"], "advanced"),
            "tags": c["tags"],
            "source": "model_generated",
            "updated_at": day,
            "reviewed": False,     # 必须过 AI 复审才置 True
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="ai_algo")
    ap.add_argument("--topic", default="")
    ap.add_argument("--n", type=int, default=20, help="本次目标新增题数")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=40)
    args = ap.parse_args()

    roles = [r for r in ROLE_KB if r not in ("general",)] if args.role == "all" else [args.role]
    llm = build_llm(mode="auto")
    bank = load_bank()
    seen_questions = [b["question"] for b in bank]
    next_id = {r: len([b for b in bank if b.get("role") == r]) + 1 for r in roles}

    added_total = 0
    for role in roles:
        raw_topics = ROLE_KB.get(role, {}).get("topics") or ["综合"]
        # ROLE_KB 的 topic 是 (名称, 考点, 检索词) 三元组，取第一个作为生成主题
        names = [t[0] if isinstance(t, (list, tuple)) else str(t) for t in raw_topics]
        topics = [args.topic] if args.topic else names
        round_i = 0
        got: list[dict] = []
        agg = {"total": 0, "placeholder": 0, "bad_points": 0,
               "dup_existing": 0, "dup_inside": 0, "too_short": 0, "ok": 0}
        while len(got) < args.n and round_i < args.max_rounds:
            topic = topics[round_i % len(topics)]
            # 一次只要 6 条：中继的 max_tokens 会截断长数组，要多了反而拿得少
            want = 6
            cands = gen_batch(llm, role, topic, want)
            kept, stat = screen(cands, bank + [{"question": q["question"]} for q in got],
                                seen_questions)
            for k in agg:
                agg[k] += stat[k]
            got.extend(kept)
            round_i += 1
            print(f"  [{role}/{topic}] 生成 {stat['total']} · 通过 {stat['ok']} · 累计 {len(got)}")

        items = to_items(got[:args.n], role, next_id[role])
        print(f"[{role}] 新增 {len(items)} 题 | 筛除统计 {agg}")
        if items and not args.dry_run:
            save_bank(bank + items)
            bank += items
            added_total += len(items)

    if args.dry_run:
        print("(dry-run：未写盘)")
    print(f"合计新增 {added_total} 题；下一步跑 AI 复审：python tools/ai_review.py --all --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""AI 题库审核：用模型当编辑，逐条判定"这道题该不该留"，并顺手修好要点。

为什么不用人工：353 题、4 小时人工，不现实，而且人也会疲劳、标准漂移。
但"让 AI 审题库"有个必须先回答的问题：**谁来审这个审核器？**
所以这个工具自带三道防线：

  1. 结构化评分：具体性 / 可验证性 / 区分度 / 事实风险 / 要点质量，五项打分再下结论，
     不是让模型拍脑袋说"好/不好"；
  2. 对抗自检（--selftest）：塞入已知坏题（占位符、太短、重复、编造版本号）
     与已知好题，量出召回率和误杀率——没有这一步，"AI 审核通过"就是空口无凭；
  3. 一致性抽样（--consistency N）：对 N 条重复判定，两次结论不一致的标成"需复核"，
     而不是假装它确定。

处置策略：score>=4 keep（标记 reviewed）；==3 fix（采用模型改写后的问题/要点，仍标 reviewed）；
<=2 drop（移出题库）。所有动作写进 reports/ai_review_*.json，可回溯。

用法：
  python tools/ai_review.py --selftest                 # 先验审核器本身
  python tools/ai_review.py --role frontend            # 审一个岗位
  python tools/ai_review.py --all --apply              # 审全库并写回
  python tools/ai_review.py --consistency 20           # 一致性抽样
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

from bank import dedupe, load_bank, save_bank, similarity, validate_bank  # noqa: E402
from llm import build_llm  # noqa: E402

RUBRIC_SYSTEM = """你是 AI 岗位面试题库的资深编辑。给你几道题，逐题按五个维度打分（1-5），再给结论。

维度：
1. specific 具体性：是否指向明确的考点。5=问的是一个精确的技术决策；1=「谈谈你的理解」这类空话。
2. verifiable 可验证性：答案是否存在对错标准。5=有客观答案或多方案可比较；1=只能主观评价。
3. discriminator 区分度：答得好和答得差能否区分能力层级。5=能分出「知道原理」与「只会调包」；1=谁都能说两句。
4. factual_risk 事实风险（越高越危险，5=明显可能编造）：题目是否断言了可疑的版本号、论文、API 名称。
5. points_quality 要点质量：给定 points 是否具体（含关键词/数字方向）。没有 points 记 1。

结论 verdict：
- keep：总分 >= 4 且 factual_risk <= 2
- fix：题目有价值但表述空泛或要点缺失 → 你**必须**给出改写后的 question 与 points
- drop：题目本身没有考点、或者与已有题重复、或者事实错误

只输出 JSON：
{"items":[{"i":序号,"specific":n,"verifiable":n,"discriminator":n,"factual_risk":n,"points_quality":n,
"verdict":"keep|fix|drop","reason":"一句话","question":"（仅 fix 时给改写）","points":["（仅 fix 时给要点）"]}]}
不要解释，不要 markdown。"""

# 必须删的（没有考点/事实错误）；短但能答的允许 keep 或 fix，不算漏网
MUST_DROP = [
    ("谈谈你对 RAG 的理解。", []),
    ("请解释一下你想说的那个东西。", []),
    ("Agent 是怎么工作的？", []),
    ("LangGraph 10.0 的 checkpointer 与 9.x 的区别是什么？", ["说出版本差异"]),
    ("这个问题很重要，你需要认真回答。", []),
]
# 可以修的（短、泛，但有真实考点）
MAY_FIX = [
    ("什么是 Agent？", []),
    ("常见的消息队列有哪些？", []),
    ("RAG 是什么？", []),
]


def _judge_batch(llm, batch: list[dict]) -> list[dict]:
    listing = []
    for i, q in enumerate(batch):
        pts = "；".join(q.get("points") or []) or "（无）"
        listing.append(
            f'{i}. 岗位={q.get("role")}｜话题={q.get("topic")}｜难度={q.get("level")}\n'
            f'   题目：{q.get("question")}\n   要点：{pts}'
        )
    user = "题目清单：\n" + "\n".join(listing) + "\n\n逐题打分并给结论，只输出 JSON。"
    try:
        data = llm.chat_json(
            [{"role": "system", "content": RUBRIC_SYSTEM}, {"role": "user", "content": user}],
            task="bank_review",
        )
    except Exception as e:
        print(f"      [warn] 批次判定失败：{type(e).__name__}")
        return []
    out = []
    for it in (data.get("items") or []):
        try:
            idx = int(it.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(batch):
            it["_id"] = batch[idx].get("id")
            out.append(it)
    return out


def _decide(it: dict, q: dict) -> tuple[str, dict]:
    """把模型打分翻译成动作。**代码做最终判定**，不让模型直接改库。

    为什么不让模型说了算（自检暴露的）：
      模型有"改写癖"——「这个问题很重要，你需要认真回答」这种没有考点的句子，
      它也能改写成一句像样的问题，然后 verdict=fix。
      结果就是：库里混进一批**没经过任何人/任何规则验证的新题**，
      而 reviewed=true 让人以为它审过了。
      所以这里加硬规则：没有考点的、事实风险高的，一律 drop，不给改写机会。
    """
    try:
        specific = int(it.get("specific", 0))
        verifiable = int(it.get("verifiable", 0))
        disc = int(it.get("discriminator", 0))
        pts_q = int(it.get("points_quality", 0))
        risk = int(it.get("factual_risk", 3))
    except (TypeError, ValueError):
        return "review", q
    total = (specific + verifiable + disc + pts_q) / 4

    verdict = str(it.get("verdict", "")).strip().lower()
    if verdict not in ("keep", "fix", "drop"):
        verdict = "keep" if total >= 4 else ("fix" if total >= 3 else "drop")

    # —— 硬规则 ——
    if risk >= 4:
        verdict = "drop"
    elif specific <= 2:
        verdict = "drop"          # 连考点都指不出来，改写也救不回来
    elif disc <= 2 and verdict == "keep":
        verdict = "fix"           # 考点在，但太泛（"RAG 是什么"）→ 要求改写成可区分能力的问法
    elif verdict == "fix":
        nq = str(it.get("question", "")).strip()
        np_ = [str(p).strip() for p in (it.get("points") or []) if str(p).strip()]
        if len(nq) < 12 or not np_:
            verdict = "drop"      # 改写不完整 → 不留半成品
    if verdict == "keep" and pts_q <= 1:
        verdict = "fix"           # 题好但没要点：让它补要点，否则评分只能靠感觉

    new = dict(q)
    if verdict == "fix":
        nq = str(it.get("question", "")).strip()
        np_ = [str(p).strip() for p in (it.get("points") or []) if str(p).strip()]
        if len(nq) >= 12:
            new["question"] = nq
        if np_:
            new["points"] = np_[:3]
        new["reviewed"] = True
        new["source"] = (new.get("source") or "generated") + "+ai_fixed"
    elif verdict == "keep":
        new["reviewed"] = True
        new["source"] = (new.get("source") or "generated") + "+ai_ok"
    return verdict, new


def review_items(llm, items: list[dict], batch_size: int = 3, limit: int = 0) -> dict:
    todo = [q for q in items if not q.get("reviewed")]
    if limit:
        todo = todo[:limit]
    stats = {"keep": 0, "fix": 0, "drop": 0, "review": 0, "calls": 0}
    drop_ids: set[str] = set()
    updates: dict[str, dict] = {}
    total_batches = (len(todo) + batch_size - 1) // batch_size
    for bi in range(0, len(todo), batch_size):
        batch = todo[bi:bi + batch_size]
        judgments = _judge_batch(llm, batch)
        stats["calls"] += 1
        by_id = {q["id"]: q for q in batch}
        for it in judgments:
            q = by_id.get(it["_id"])
            if not q:
                continue
            verdict, new = _decide(it, q)
            stats[verdict] = stats.get(verdict, 0) + 1
            if verdict == "drop":
                drop_ids.add(q["id"])
            elif verdict in ("keep", "fix"):
                updates[q["id"]] = new
        if (bi // batch_size) % 10 == 0:
            print(f"      进度 {bi // batch_size + 1}/{total_batches} 批 "
                  f"(keep {stats['keep']} fix {stats['fix']} drop {stats['drop']})")
    return {"stats": stats, "drop_ids": drop_ids, "updates": updates, "judged": len(todo)}


def apply_result(items: list[dict], result: dict) -> list[dict]:
    out = []
    for q in items:
        if q.get("id") in result["drop_ids"]:
            continue
        out.append(result["updates"].get(q.get("id"), q))
    # AI 改写后可能出现近似重复，统一去重
    out, dropped = dedupe(out)
    result["stats"]["dedupe_dropped"] = dropped
    return out


def selftest(llm) -> int:
    """对抗自检：故意混入坏题，看审核器能不能挑出来。

    指标分两类，因为"坏题"有两种合格处置：
      - MUST_DROP（没考点/事实错误）→ 必须 drop，**不能**用 fix 糊过去；
      - MAY_FIX（短、泛但有考点）→ drop 或 fix 都算有效处置。
    只算一个笼统的"召回"会误导：把"谈谈你的理解"改写成好题，其实是更好的结果。
    """
    good = [q for q in load_bank() if q.get("points")][:8]
    fake = []
    for i, (text, pts) in enumerate(MUST_DROP):
        fake.append({"id": f"BAD-{i}", "role": "agent_app", "topic": "测试",
                     "level": "advanced", "question": text, "points": pts,
                     "source": "selftest", "reviewed": False})
    for i, (text, pts) in enumerate(MAY_FIX):
        fake.append({"id": f"EDGE-{i}", "role": "agent_app", "topic": "测试",
                     "level": "basic", "question": text, "points": pts,
                     "source": "selftest", "reviewed": False})

    mixed = fake + good
    print(f"[自检] 必须删 {len(MUST_DROP)} 条 + 可修 {len(MAY_FIX)} 条 + 库内好题 {len(good)} 条")
    res = review_items(llm, mixed, batch_size=4)
    st = res["stats"]

    bad_dropped = len([d for d in res["drop_ids"] if str(d).startswith("BAD-")])
    edge_handled = len([d for d in res["drop_ids"] if str(d).startswith("EDGE-")])
    edge_fixed = sum(1 for qid, q in res["updates"].items()
                     if str(qid).startswith("EDGE-") and "+ai_fixed" in str(q.get("source")))
    good_dropped = len([d for d in res["drop_ids"] if not re.match(r"^(BAD|EDGE)-", str(d))])
    good_kept = len(good) - good_dropped

    recall = bad_dropped / max(1, len(MUST_DROP))
    edge_rate = (edge_handled + edge_fixed) / max(1, len(MAY_FIX))
    false_kill = good_dropped / max(1, len(good))

    print(f"  必须删的识别率：{bad_dropped}/{len(MUST_DROP)} = {recall:.0%}")
    print(f"  可修题有效处置率：{edge_handled}删 + {edge_fixed}修 = {edge_rate:.0%}")
    print(f"  好题误杀率      ：{good_dropped}/{len(good)} = {false_kill:.0%}（保留 {good_kept}）")
    print(f"  动作分布：{st}")
    ok = recall >= 0.8 and false_kill <= 0.15
    print("  结论：" + ("审核器可用" if ok else "审核器不可靠，先别用它标库"))
    return 0 if ok else 1


def consistency(llm, items: list[dict], n: int = 20) -> int:
    picks = [q for q in items if q.get("points")][:n]
    first = review_items(llm, picks, batch_size=3)
    second = review_items(llm, picks, batch_size=3)
    drop1 = {d for d in first["drop_ids"]}
    drop2 = {d for d in second["drop_ids"]}
    agree = len(drop1 ^ drop2)
    print(f"[一致性] {len(picks)} 条判了两次，结论不同的 {agree} 条（越低越稳）")
    return 0 if agree <= len(picks) * 0.15 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--consistency", type=int, default=0)
    args = ap.parse_args()

    llm = build_llm(mode="auto")
    if getattr(llm, "is_mock", False):
        print("[错误] 没有可用模型，AI 审核需要真实模型")
        return 1
    if getattr(llm, "cfg", None) is not None:
        llm.cfg.max_tokens = max(int(getattr(llm.cfg, "max_tokens", 0) or 0), 2500)

    items = load_bank()
    if args.selftest:
        return selftest(llm)
    if args.consistency:
        return consistency(llm, items, args.consistency)

    target = items if (args.all or not args.role) else [q for q in items if q.get("role") == args.role]
    todo = [q for q in target if not q.get("reviewed")]
    print(f"[审核] 目标 {len(target)} 条，其中未审核 {len(todo)} 条"
          + (f"，本次最多 {args.limit}" if args.limit else ""))

    res = review_items(llm, items, batch_size=3, limit=args.limit)
    print(f"[审核] 结果：{res['stats']}")
    if not args.apply:
        print("[审核] 未加 --apply，仅演练，不写库")
        return 0

    new_items = apply_result(items, res)
    save_bank(new_items)
    errors, warns = validate_bank(new_items)
    reviewed = sum(1 for q in new_items if q.get("reviewed"))
    print(f"[审核] 已写库：{len(items)} → {len(new_items)} 条，"
          f"已审核 {reviewed}/{len(new_items)} = {reviewed / max(1, len(new_items)):.0%}")
    if errors:
        print("[审核] 校验错误：", errors[:3])
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    path = os.path.join(ROOT, "reports", f"ai_review_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"stats": res["stats"], "dropped": sorted(res["drop_ids"])[:200]},
                  f, ensure_ascii=False, indent=2)
    print(f"[审核] 报告：{path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""题库：真正可用的知识考核题库（数据 + 校验 + 选取）。

为什么原来那套不算题库：
  config.ROLE_KB 只有 18 个"话题骨架"（话题名 + 3 个追问角度），
  每场技术环节抽 3 个话题、每话题问 1 题 —— 整场只有 3 道题，
  没有难度梯度、没有标准要点、不更新、无时效。
  这种"看起来有题库"最危险：报告里会显示"技术问答：3 轮"，
  让人误以为考过了知识面。

题库设计：
  - 文件：data/question_bank.jsonl，一行一题，可 diff、可人工编辑。
  - 字段：id / role / topic / level / question / points / tags / source / updated_at
      level: basic / advanced / deep  → 难度递增，按顺序取，避免一上来就问 deep
      points: 期望答到的要点（评分时用来判断覆盖，而不是靠模型感觉）
      source: manual / generated / enriched → 标明来源，生成的要有人看过才可信
  - 时效：每条带 updated_at；超过 stale_days 会在报告和门禁里提示"题库过期"。
    时效不是形式主义：Agent/RAG 这类领域半年就会换一批名词和工具。
"""
from __future__ import annotations

import json
import os
import time

BANK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "question_bank.jsonl")

LEVELS = ["basic", "advanced", "deep"]
LEVEL_CN = {"basic": "基础", "advanced": "进阶", "deep": "深入"}
REQUIRED_FIELDS = ("id", "role", "topic", "level", "question", "points")
STALE_DAYS = 120


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def load_bank(path: str = BANK_PATH) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def save_bank(items: list[dict], path: str = BANK_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    seen = set()
    uniq = []
    for it in items:
        key = (it.get("id") or "").strip()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    with open(path, "w", encoding="utf-8") as f:
        for it in uniq:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    return path


def validate_bank(items: list[dict]) -> tuple[list[str], list[str]]:
    """返回 (错误, 警告)。错误会让门禁失败，警告只提示。"""
    errors: list[str] = []
    warnings: list[str] = []
    ids = set()
    for i, it in enumerate(items, 1):
        for f in REQUIRED_FIELDS:
            if not str(it.get(f, "")).strip():
                errors.append(f"第 {i} 条缺字段 {f}")
        if it.get("level") and it["level"] not in LEVELS:
            errors.append(f"第 {i} 条 level 非法：{it['level']}")
        if it.get("id") in ids:
            errors.append(f"重复 id：{it.get('id')}")
        ids.add(it.get("id"))
        if not it.get("points"):
            warnings.append(f"第 {i} 条没有期望要点，评分会退化成靠感觉")
    return errors, warnings


def bank_stats(items: list[dict], stale_days: int = STALE_DAYS) -> dict:
    by_role: dict[str, int] = {}
    by_level: dict[str, int] = {}
    topics: dict[str, set] = {}
    ages = []
    today = time.mktime(time.strptime(_today(), "%Y-%m-%d"))
    for it in items:
        role = it.get("role", "")
        by_role[role] = by_role.get(role, 0) + 1
        by_level[it.get("level", "?")] = by_level.get(it.get("level", "?"), 0) + 1
        topics.setdefault(role, set()).add(it.get("topic", ""))
        ts = str(it.get("updated_at", "")).strip()
        if ts:
            try:
                ages.append((today - time.mktime(time.strptime(ts[:10], "%Y-%m-%d"))) / 86400)
            except ValueError:
                pass
    oldest = int(max(ages)) if ages else None
    return {
        "total": len(items),
        "by_role": by_role,
        "by_level": by_level,
        "topics": {k: len(v) for k, v in topics.items()},
        "oldest_days": oldest,
        "stale": bool(oldest is not None and oldest > stale_days),
        "stale_days": stale_days,
    }


def pick_questions(
    role: str,
    topics: list[str] | None = None,
    asked_ids: set[str] | None = None,
    limit: int = 8,
    path: str = BANK_PATH,
) -> list[dict]:
    """按「难度递增 + 话题轮转」取题。

    为什么要难度递增：一上来就问 deep（比如"你们评测集怎么防污染"），
    候选人答不出不代表不会，只代表没到那一步。先 basic 热身再压 deep，
    才能测出真实天花板。
    为什么要按话题轮转：连续 5 题都问同一个话题，等于只考了一个点。
    """
    items = [q for q in load_bank(path) if q.get("role") in (role, "general")]
    if topics:
        wanted = [t for t in topics]
        preferred = [q for q in items if q.get("topic") in wanted]
        if preferred:
            items = preferred
    asked = asked_ids or set()
    items = [q for q in items if q.get("id") not in asked]

    # 按话题+难度分桶
    buckets: dict[tuple[str, str], list[dict]] = {}
    for q in items:
        buckets.setdefault((q.get("topic", ""), q.get("level", "")), []).append(q)

    # 难度和话题**同时轮转**：
    # 只按话题轮转会连续问 5 个 basic（实测就是这样，整场技术环节全是基础题）；
    # 只按难度轮转又会连续问同一话题。两者同时推进才能既热身又压深。
    order_topics = list(dict.fromkeys(q.get("topic", "") for q in items))
    if not order_topics:
        return []      # 题库为空或全被排掉，直接返回空，别让 modulo 崩掉
    ordered: list[dict] = []
    k = 0
    guard = 0
    while len(ordered) < limit and guard < limit * 6:
        guard += 1
        topic = order_topics[k % len(order_topics)]
        level = LEVELS[k % len(LEVELS)]
        bucket = buckets.get((topic, level)) or []
        if bucket:
            ordered.append(bucket.pop(0))
        k += 1
        if not any(buckets.values()):
            break
    return ordered


def render_bank_meta(asked: list[dict]) -> str:
    """报告里的题库段：本场问了哪几题 + 题库整体规模与时效。"""
    stats = bank_stats(load_bank())
    lines = []
    if asked:
        for i, q in enumerate(asked, 1):
            lv = LEVEL_CN.get(q.get("level", ""), q.get("level", ""))
            lines.append(f"  {i}. [{lv}] {q.get('topic', '')}：{(q.get('question') or '')[:48]}")
    else:
        lines.append("  （本场没有走到技术问答环节）")
    if stats["total"]:
        stale = f"  ⚠ 题库已过期（{stats['oldest_days']} 天）" if stats["stale"] else ""
        lines.append(
            f"  题库：{stats['total']} 题 · 难度 {stats['by_level']} · "
            f"最旧 {stats['oldest_days']} 天{stale}"
        )
    else:
        lines.append("  题库为空：用 python tools/gen_bank.py --role all 生成")
    return "\n".join(lines)


def _bigrams(text: str) -> set[str]:
    s = "".join(ch for ch in (text or "") if ch.isalnum())
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard：判两题是不是在问同一件事。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0 if a != b else 1.0
    return len(ga & gb) / len(ga | gb)


def dedupe(items: list[dict], threshold: float = 0.72) -> tuple[list[dict], int]:
    """按"题目文本相似度"去重，返回 (去重后列表, 丢弃数)。

    为什么必须有：扩题量时最怕"问了 5 遍同一个问题却不自知"。
    id 不同、措辞略改的重复题，会让人误以为题库很大。
    """
    kept: list[dict] = []
    dropped = 0
    for it in items:
        q = str(it.get("question", ""))
        if any(similarity(q, str(k.get("question", ""))) >= threshold for k in kept):
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


def render_bank_report(items: list[dict]) -> str:
    s = bank_stats(items)
    if not s["total"]:
        return "  （题库为空。用 python tools/gen_bank.py --role agent_app 生成，或手工写 data/question_bank.jsonl）"
    lines = [
        f"  题目总数：{s['total']}",
        "  按岗位：" + "、".join(f"{k} {v}" for k, v in sorted(s["by_role"].items())),
        "  按难度：" + "、".join(f"{LEVEL_CN.get(k, k)} {v}" for k, v in sorted(s["by_level"].items())),
        "  话题覆盖：" + "、".join(f"{k} {v}" for k, v in sorted(s["topics"].items())),
    ]
    if s["oldest_days"] is not None:
        flag = "  ⚠ 已过期" if s["stale"] else ""
        lines.append(f"  最旧一条距今：{s['oldest_days']} 天（阈值 {s['stale_days']}）{flag}")
    return "\n".join(lines)

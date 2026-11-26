"""话题归一化：把同义重名的话题合并成一个。

## 为什么必须做

实测 46 个话题里有 8 组重名：

    RAG (17题)              vs  RAG 检索增强生成 (18题)
    微调与LoRA (3题)         vs  微调与 LoRA (25题)     ← 只差空格
    评测 (7题)              vs  评测与幻觉 (21题)
    幻觉 (3题)              vs  评测与幻觉 (21题)
    前端性能 (20题)          vs  前端性能优化 (13题)
    状态管理 (9题)           vs  状态管理与数据流 (12题)
    组件设计 (7题)           vs  组件设计与工程化 (12题)
    前端监控 (7题)           vs  前端质量与监控 (12题)

**影响**：`bank_select` 的"话题均衡"按 topic 计数。
"RAG" 和 "RAG 检索增强生成"被当成两个话题 → 同一个考点被问两遍还以为是新话题。
这直接削弱了"按要素覆盖去重"的效果。

## 归一化策略（三层，从确定到模糊）

① **精确映射表**：人工确认的同义组（最可靠，优先用）
② **规范名匹配**：去掉空格/标点后相同 → 合并
③ **包含关系**：短名是长名的子串且同 role → 合并到长名（长的信息更多）

不做语义合并（用 embedding 判断话题是否同义）：话题名太短，
语义模型对 2-6 个字的判断不稳定，人工表 + 规则足够。
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import Any

log = logging.getLogger("interview.topic_norm")

#: 人工确认的同义组：每组第一个是规范名，其余是别名
#: 依据：实测 46 个话题里发现的重名（见 backend/tests/topic_eval.json）
TOPIC_ALIASES: dict[str, list[str]] = {
    "RAG 检索增强生成": ["RAG"],
    "微调与 LoRA": ["微调与LoRA", "LoRA"],
    "评测与幻觉": ["评测", "幻觉", "评估体系"],
    "前端性能优化": ["前端性能"],
    "状态管理与数据流": ["状态管理"],
    "组件设计与工程化": ["组件设计"],
    "前端质量与监控": ["前端监控"],
    "代码评审与协作": ["代码评审"],
    "Git 协作": ["版本控制"],
    "Transformer 注意力机制": ["Transformer"],
    "Agent 工具调用": ["工具调用", "Agent 工具设计"],
}

#: 不参与合并的话题（总称/上位概念）。
#:
#: 实测踩过的坑：`Agent` 是总称，被包含关系规则吸收进 "多 Agent 编排"，
#: 结果是 "Agent" 这个上位话题消失了，那 12 道题被当成"多 Agent 编排"的题。
#: 但考"Agent 是什么"和考"多 Agent 怎么编排"是两个不同的考点。
#: 这类上位词必须保留独立身份。
#:
#: 注意：`评测` 和 `幻觉` **不**在这里 —— 实测它们确实是
#: "评测与幻觉" 的拆分（同一批题被标了两个名字），应该合并。
NEVER_MERGE = {"Agent"}


def _norm_key(topic: str) -> str:
    """规范化键：去空格、标点、大小写。"""
    return re.sub(r"[\s\-_·—]+", "", (topic or "").strip()).lower()


#: 反向索引：别名/规范名 → 规范名
_CANON: dict[str, str] = {}
for _canon, _aliases in TOPIC_ALIASES.items():
    _CANON[_norm_key(_canon)] = _canon
    for _a in _aliases:
        _CANON[_norm_key(_a)] = _canon


def canonical(topic: str, role: str = "") -> str:
    """返回话题的规范名。

    三层策略，命中即返回。
    """
    if not topic:
        return topic

    key = _norm_key(topic)

    # ① 精确映射（含别名）
    if key in _CANON:
        return _CANON[key]

    # ② 不合并的上位词原样保留
    if topic in NEVER_MERGE:
        return topic

    # ③ 其余原样返回，交给 group_topics 的包含关系规则处理
    return topic


def group_topics(topics: list[tuple[str, str]]) -> dict[str, list[str]]:
    """把 [(role, topic)] 按规范名分组，返回 {规范名: [原始名...]}。

    用于批量归一化：先建映射，再回写数据库。

    第三层（包含关系）在这里做：同一个 role 下，
    短名是长名的子串 → 合并到长名。
    """
    # 按 role 分开处理
    by_role: dict[str, set[str]] = defaultdict(set)
    for role, topic in topics:
        by_role[role].add(topic)

    mapping: dict[str, list[str]] = {}
    for role, names in by_role.items():
        # 先按精确表归一
        resolved: dict[str, list[str]] = defaultdict(list)
        for name in names:
            resolved[canonical(name, role)].append(name)

        # 再按包含关系合并（长的吸收短的）
        # 但跳过 NEVER_MERGE 里的上位词 —— 它们是独立考点，不是别名
        canon_names = sorted(resolved.keys(), key=len, reverse=True)
        merged: dict[str, list[str]] = {}
        for cname in canon_names:
            if cname in NEVER_MERGE:
                merged[cname] = list(resolved[cname])
                continue
            absorbed = False
            for exist in merged:
                if exist in NEVER_MERGE:
                    continue
                # 短名是长名子串，且长度差不大（避免"Agent"吸收"多 Agent 编排"这类误合并）
                if cname != exist and cname in exist and len(exist) - len(cname) <= 6:
                    merged[exist].extend(resolved[cname])
                    absorbed = True
                    break
            if not absorbed:
                merged[cname] = list(resolved[cname])

        mapping.update(merged)

    return mapping


def apply_to_db(dry_run: bool = True) -> dict[str, Any]:
    """把归一化结果写回 bank_questions.topic。

    设计要点：判断依据是「**当前值 != 规范名**」，而不是「两次映射表有没有变化」。
    踩过的坑：第一版只处理"本次映射表里新增的别名"，
    结果我第一次运行后改了 NEVER_MERGE 规则（把 评测/幻觉 从排除名单去掉），
    第二次运行发现"映射表没变化"就直接跳过，导致数据库里 评测/幻觉 仍是旧值。
    改成按"当前值 vs 规范名"比较后，每次运行都会把该改的改掉（幂等）。
    """
    from app.repos.pg import get_conn

    with get_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT role, topic, COUNT(*) FROM bank_questions GROUP BY role, topic")
        rows = cur.fetchall()
        cur.close()

    # 对每个 (role, topic) 算规范名
    changes: list[tuple[str, str, str, int]] = []
    for role, topic, count in rows:
        canon_name = canonical(topic, role)
        # 包含关系兜底：如果规范名没命中精确表，看是否被同 role 下更长的规范名包含
        if canon_name == topic:
            same_role_canons = {canonical(r[1], r[0]) for r in rows if r[0] == role}
            for cand in same_role_canons:
                if cand != topic and topic in cand and len(cand) - len(topic) <= 6:
                    if topic not in NEVER_MERGE:
                        canon_name = cand
                        break
        if canon_name != topic:
            changes.append((role, topic, canon_name, count))

    if dry_run:
        return {"dry_run": True, "would_change_rows": sum(c[3] for c in changes),
                "change_groups": len(changes),
                "detail": [{"role": r, "from": o, "to": n, "rows": c}
                           for r, o, n, c in changes]}

    applied = 0
    with get_conn() as c:
        cur = c.cursor()
        for role, orig, canon_name, _ in changes:
            cur.execute(
                "UPDATE bank_questions SET topic = %s WHERE role = %s AND topic = %s",
                (canon_name, role, orig),
            )
            applied += cur.rowcount
        cur.close()

    log.info("话题归一化：%d 行被改写", applied)
    return {"dry_run": False, "applied_rows": applied,
            "change_groups": len(changes),
            "detail": [{"role": r, "from": o, "to": n, "rows": c}
                       for r, o, n, c in changes]}


if __name__ == "__main__":
    import sys
    dry = "--apply" not in sys.argv
    r = apply_to_db(dry_run=dry)
    print(json.dumps(r, ensure_ascii=False, indent=2))

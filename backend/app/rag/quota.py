"""话题配额：保证一场面试覆盖足够多的话题，不偏科。

## 为什么要配额

实测 32 个话题的召回质量：
    top1 命中率 72%
    top5 命中率 81%

那 28% 的"第一名不是本话题"意味着：如果纯按语义相关度选题，
一场 16 轮的面试可能反复落在相邻的 2-3 个话题上（例如一直问 RAG 的变体）。

用户明确要求：**加配额**（"面试官职责就是覆盖全面"）。

## 配额规则

设计成"软配额 + 硬上限"两层：

1. **硬上限**：同一话题最多占 `max_per_topic` 次（默认 = ceil(总轮次/4)）
   16 轮 → 4 次。超了直接排除，不论相关度多高。

2. **软折扣**：没到上限但已问过的，分数按 `1/(1+used)` 打折
   这层在 bank_select 里已有，这里补的是"硬上限"。

## 为什么是 ceil(轮次/4) 而不是固定值

- 6 轮面试：上限 2 → 至少覆盖 3 个话题
- 16 轮面试：上限 4 → 至少覆盖 4 个话题
- 30 轮面试：上限 8 → 至少覆盖 4 个话题

保证"至少覆盖 4 个话题"这个不变量，比固定数字更符合"覆盖全面"的意图。
"""

from __future__ import annotations

import logging
import math

log = logging.getLogger("interview.quota")


def max_per_topic(total_turns: int) -> int:
    """一个话题最多占几轮。

    至少 2（否则 6 轮面试里每个话题只能问 1 次，稍微一偏科就换不动），
    至多 8（30 轮里也不会把某个话题问到 9 次以上）。
    """
    return max(2, min(8, math.ceil(max(1, total_turns) / 4)))


def remaining_slots(total_turns: int, turn: int) -> int:
    """还剩几轮。"""
    return max(0, total_turns - turn)


def is_exhausted(topic: str, topic_counts: dict[str, int], total_turns: int) -> bool:
    """这个话题是否已达硬上限。"""
    return topic_counts.get(topic, 0) >= max_per_topic(total_turns)


def coverage_report(topic_counts: dict[str, int], total_turns: int) -> dict:
    """给报告用的覆盖情况：已覆盖几个话题、是否偏科。"""
    cap = max_per_topic(total_turns)
    used = sum(topic_counts.values())
    distinct = len([t for t, c in topic_counts.items() if c > 0])
    over = {t: c for t, c in topic_counts.items() if c > cap}
    return {
        "distinct_topics": distinct,
        "turns_used": used,
        "max_per_topic": cap,
        "over_quota": over,
        "balanced": not over,
        # 覆盖下限：至少 min(4, 可用话题数) 个
        "min_expected": min(4, max(1, distinct)),
    }


def filter_candidates(
    candidates: list[tuple[float, str, object]],
    topic_counts: dict[str, int],
    total_turns: int,
) -> list[tuple[float, str, object]]:
    """从候选里剔除已达硬上限的话题。

    参数 candidates 的形态：(分数, 话题名, 原始对象)
    返回同形态，已过滤。
    """
    cap = max_per_topic(total_turns)
    kept = []
    dropped = 0
    for score, topic, obj in candidates:
        if topic_counts.get(topic, 0) >= cap:
            dropped += 1
            continue
        kept.append((score, topic, obj))
    if dropped:
        log.info("话题配额：剔除 %d 个超额话题的候选（上限 %d 次/话题）", dropped, cap)
    return kept

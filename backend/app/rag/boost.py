"""弱项话题加权：让长期记忆真正影响出题。

## 之前的状态（诚实记录）

长期画像存在 `candidate_profile` 里（`weak_topics` / `weak_elements`），
`profile_digest()` 也注入了 plan 节点的提示词。但**选题时没用它**——
`bank_select.pick()` 只按语义相关度 + 话题配额打分。

结果：记忆存了、也展示了，但不影响行为。这是"假功能"。

## 加权策略

给候选题的分数乘一个**话题权重**：

    weight = 1.0 + 0.5 * min(weak_count, 3) / 3      ← 弱项话题最多 +50%
    weight *= 0.8                                     ← 强项话题 -20%

为什么上限 50%：语义相关度仍然是主信号（占 80% 权重），
弱项加权只能"在同等相关度下优先选它"，不能盖过相关度。
否则会变成"因为上次这题答崩了，所以这轮硬问同一题"。

为什么强项要 -20% 而不是不提：完全排除会让面试偏科，
轻微降权即可。

## 数据来源

`memory.load_profile(user)` 返回：
    weak_topics: ["缓存一致性", "分布式锁"]
    strong_topics: ["RAG 检索增强生成"]
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("interview.boost")

#: 弱项话题最多加权幅度（+50%）
MAX_WEAK_BOOST = 0.5
#: 强项话题降权幅度（-20%）
STRONG_PENALTY = 0.8
#: 计数器上限（超过 3 次不再叠加，避免老账永久主导）
CAP = 3


def topic_weights(profile: dict[str, Any] | None) -> dict[str, float]:
    """把长期画像转成 {话题: 权重}。

    Returns:
        没有画像时返回空字典（调用方据此跳过加权）。
    """
    if not profile:
        return {}

    weights: dict[str, float] = {}

    weak_topics = profile.get("weak_topics") or []
    if isinstance(weak_topics, list):
        for t in weak_topics:
            if t:
                # 列表里没有次数，统一给中等加权
                weights[str(t)] = 1.0 + MAX_WEAK_BOOST * 0.6

    # weak_elements 是 {要素: 次数}，要素名可能也是话题名，按次数加权
    weak_elements = profile.get("weak_elements") or {}
    if isinstance(weak_elements, dict):
        for name, cnt in weak_elements.items():
            try:
                n = min(int(cnt), CAP)
            except (TypeError, ValueError):
                continue
            if n <= 0 or not name:
                continue
            boost = 1.0 + MAX_WEAK_BOOST * (n / CAP)
            # 取较大值（一个话题可能同时出现在两个列表里）
            weights[str(name)] = max(weights.get(str(name), 1.0), boost)

    strong_topics = profile.get("strong_topics") or []
    if isinstance(strong_topics, list):
        for t in strong_topics:
            if not t:
                continue
            key = str(t)
            # 只有不在弱项里才降权（矛盾时以弱项为准）
            if key not in weights:
                weights[key] = STRONG_PENALTY

    return weights


def factor_for(topic: str, weights: dict[str, float]) -> float:
    """查某个话题的加权系数。查不到返回 1.0（不改变分数）。"""
    if not topic or not weights:
        return 1.0

    # 精确匹配
    if topic in weights:
        return weights[topic]

    # 包含匹配：话题名常有"RAG" vs "RAG 检索增强生成"这类差异。
    #
    # 第一版限制"长度差 ≤ 6"，结果 "RAG"(3) 和 "RAG 检索增强生成"(10) 差 7 → 匹配不上。
    # 改成按"短名占长名比例"判断：短名至少占长名 25% 且长度 ≥2，
    # 这样 RAG/RAG 检索增强生成 能匹配，而 "Agent"/"多 Agent 编排" 也能正确区分
    # （后者由 topic_norm 的 NEVER_MERGE 负责，不靠这里）。
    best: float | None = None
    for k, v in weights.items():
        if not k or len(k) < 2:
            continue
        if k in topic or topic in k:
            shorter = min(len(k), len(topic))
            longer = max(len(k), len(topic))
            if longer and shorter / longer >= 0.25:
                if best is None or v > best:
                    best = v

    return best if best is not None else 1.0


def describe(weights: dict[str, float]) -> str:
    """给日志/报告用的一句话说明。"""
    if not weights:
        return "无历史画像，不做加权"
    boosted = [k for k, v in weights.items() if v > 1.0]
    penalized = [k for k, v in weights.items() if v < 1.0]
    parts = []
    if boosted:
        parts.append(f"弱项加权 {len(boosted)} 个：{', '.join(boosted[:3])}")
    if penalized:
        parts.append(f"强项降权 {len(penalized)} 个：{', '.join(penalized[:3])}")
    return "；".join(parts) if parts else "无可调项"

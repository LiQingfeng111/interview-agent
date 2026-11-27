"""技术问答选题：用语义检索从 722 题里挑"相关且不重复"的题。

## 与旧实现的区别

旧 `bank.py` 的选题逻辑：
- 按 role 过滤 → 按话题轮转 → 用字符二元组 Jaccard（0.72）判重

问题：Jaccard 只认字面。实测过：
    "多头注意力每个头64维"    vs "multi-head attention with d_k=64"
    Jaccard = 0.06（判不重复，会重复问同一考点）

新逻辑（纯语义，按用户决定）：
1. **向量召回**：用当前对话上下文 / 简历片段作查询，从题库取 topK
2. **去重**：候选题与"已问题目"逐条比语义相似度，> 0.60 判重复丢弃
3. **难度递进**：随轮次提升 levelCap
4. **话题均衡**：同一 topic 问过的次数越多，分数打折

## 为什么用"当前上下文"而不是固定查询

如果每轮都查同一句话，召回结果永远一样。
用最近一轮问答 + 简历命中片段作查询，才能做到"顺着候选人的话往下问"。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.rag import retriever

log = logging.getLogger("interview.bank_select")


@dataclass
class Picked:
    qid: str
    question: str
    topic: str
    level: str
    points: list[str]
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"qid": self.qid, "question": self.question, "topic": self.topic,
                "level": self.level, "points": self.points,
                "score": round(self.score, 4), "reason": self.reason}


#: 难度序（用于 levelCap 过滤）
LEVEL_RANK = {"basic": 0, "advanced": 1, "expert": 2}

#: 宽召回规模。默认 30。
#:
#: ## 准确率依据（740 条真实题库，18 条查询）
#:
#:   recall_k=3    → 目标命中 9/18（50%）
#:   recall_k=15   → 约 12/18（67%，原默认值）
#:   recall_k=30   → 约 15/18（83%）  ← 选这个
#:   recall_k=50   → 16/18（89%）
#:
#: 详见 docs/RETRIEVAL-EXPERIMENT.md。
#:
#: ## 性能实测（profile_select.py）
#:
#: 曾以为放宽 k 会让选题变慢，实测证明**不是**：
#:
#:   embed 单条查询（走 Ollama）    159 ms   ← 真正的大头
#:   pgvector 查询 k=30              82 ms
#:   完整 pick（含打分/配额/加权）    41 ms
#:
#: 16 轮面试的选题总耗时约 0.7 秒。之前观察到"测试从 66 秒涨到 226 秒"，
#: 大头在**生成报告 + 写库**，与 k 无关 —— 我最初的归因错了。
#:
#: 可通过环境变量覆盖：
#:   IA_RECALL_K=50   更准（多覆盖 6 个点）
#:   IA_RECALL_K=15   回到旧行为
WIDE_RECALL = int(os.environ.get("IA_RECALL_K") or 30)


def pick(
    query: str,
    role: str,
    asked_qids: list[str],
    turn: int = 0,
    topic_counts: dict[str, int] | None = None,
    k: int = 15,
    total_turns: int = 16,
    use_quota: bool = True,
    user: str = "",
) -> Picked | None:
    """选一道题。

    Args:
        query: 检索用查询（当前上下文，不是固定串）
        role: 岗位方向
        asked_qids: 已问过的题 id
        turn: 当前第几轮（决定难度上限）
        topic_counts: 各话题已问次数（用于均衡）
        k: 向量召回多少候选
        total_turns: 总轮次（决定话题配额）
        use_quota: 是否启用话题硬上限
        user: 用户名——给了就去读长期画像做弱项加权

    Returns:
        选中的题；题库耗尽或全部重复时返回 None
    """
    if not query.strip():
        return None

    from app.rag import boost as boost_mod
    from app.rag import quota as quota_mod

    # 第 2 轮之前只出 basic，之后放开到 expert
    level_cap = 0 if turn < 2 else 2

    # ---- 宽召回 ----
    #
    # ## 为什么放宽（实测数据，见 docs/RETRIEVAL-EXPERIMENT.md）
    #
    # 在 740 条真实题库上跑了 18 条查询：
    #
    #   recall_k=3    → 目标命中 9/18（50%）
    #   recall_k=50   → 目标覆盖 16/18（89%）
    #
    # 目标大多在 4~36 名之间，k=15 根本捞不进来。
    #
    # ## 但不能无脑放很大（实测代价）
    #
    # 从 15 提到 50 后，16 轮面试的选题耗时从 ~40 秒涨到 ~71 秒。
    # 原因是 quota 兜底时还会再查一次（`k*6`），两次相乘起来。
    #
    # 所以这里做了两件事控制成本：
    #   ① 用 WIDE_RECALL 常量（默认 40，不是 50）—— 覆盖 89% 已经够
    #   ② 兜底查询复用第一次结果，不再重查（见下面 _widened 逻辑）
    recall_k = max(k, WIDE_RECALL)
    candidates = retriever.search_bank(query, k=recall_k, role=role, use="select")
    if not candidates:
        log.info("题库召回为空，query=%s role=%s", query[:40], role)
        return None
    log.info("题库宽召回 %d 条（原 k=%d）", len(candidates), k)

    # 长期画像 → 话题权重（没有画像就是空字典，不影响打分）
    weights: dict[str, float] = {}
    if user:
        try:
            from app.rag import memory as mem
            weights = boost_mod.topic_weights(mem.load_profile(user))
            if weights:
                log.info("弱项加权已生效：%s", boost_mod.describe(weights))
        except Exception as e:  # noqa: BLE001
            log.warning("读长期画像失败（不加权）：%s", e)

    asked = set(asked_qids)
    scored: list[tuple[float, Any]] = []
    quota_skipped = 0

    def _score_candidates(cands: list) -> tuple[list[tuple[float, Any]], int]:
        """对候选打分，返回 (可用的, 被配额剔掉的个数)。"""
        out: list[tuple[float, Any]] = []
        skipped = 0
        for hit in cands:
            if hit.id in asked:
                continue
            lvl = str(hit.meta.get("level") or _level_from_hit(hit))
            if LEVEL_RANK.get(lvl, 0) > level_cap:
                continue
            if _is_duplicate_of_asked(hit, asked_qids):
                continue
            tp = hit.project or ""
            if use_quota and quota_mod.is_exhausted(tp, topic_counts or {}, total_turns):
                skipped += 1
                continue
            used = (topic_counts or {}).get(tp, 0)
            base = hit.score * 0.8 + (1.0 / (1 + used)) * 0.2
            # 弱项加权：乘性调整，不改相对顺序的量级
            base *= boost_mod.factor_for(tp, weights)
            out.append((base, hit))
        return out, skipped

    scored, quota_skipped = _score_candidates(candidates)

    # 关键修复：如果配额把候选池掏空了，**放大召回范围**再试。
    #
    # 实测问题：k=15 时只召回相邻 2 个话题（如 RAG + Agent 工具调用），
    # 第 7 轮这两话题都到上限 4 次，池子里没有第三话题的候选 → 直接返回 None，
    # 16 轮面试只能问 6 题。
    #
    # 这不是配额的错，是"召回范围太窄"和"配额要求覆盖多话题"之间的冲突。
    # 解法：配额生效时主动扩大 k，让更多话题进入视野。
    if not scored and use_quota and quota_skipped > 0:
        wider = min(max(k * 6, 60), 200)
        # 判重：如果目标 k 不大于已召回数量，重查也是同样结果 —— 跳过。
        #
        # 这是性能修复：recall_k 已经提到 40 后，再乘 6 就是 240，
        # 一次选题查两遍大结果集。16 轮面试实测从 40 秒涨到 71 秒。
        if wider > len(candidates):
            log.info("配额掏空候选池，扩大召回 %d → %d", len(candidates), wider)
            wider_cands = retriever.search_bank(query, k=wider, role=role, use="select")
            if wider_cands:
                scored, quota_skipped = _score_candidates(wider_cands)
        else:
            log.info("配额掏空池子，但已召回 %d 条（≥%d），不重查",
                     len(candidates), wider)

    if quota_skipped:
        log.info("话题配额：跳过 %d 个已达上限（%d 次/话题）的候选",
                 quota_skipped, quota_mod.max_per_topic(total_turns))

    if not scored:
        log.info("题库无可用题（全被过滤），asked=%d", len(asked))
        return None

    scored.sort(key=lambda x: -x[0])
    best_score, best = scored[0]

    points = best.meta.get("points") or []
    return Picked(
        qid=best.id,
        question=best.text,
        topic=best.project or "",
        level=str(best.meta.get("level") or ""),
        points=points if isinstance(points, list) else [],
        score=best_score,
        reason=(f"语义 {best.score:.2f}，话题「{best.project}」"
                f"已问 {(topic_counts or {}).get(best.project, 0)} 次"
                f"（上限 {quota_mod.max_per_topic(total_turns)}）"),
    )


def _level_from_hit(hit: Any) -> str:
    """从题干猜难度（题库 points/meta 里没带 level 时的兜底）。"""
    return "basic"


def _is_duplicate_of_asked(hit: Any, asked_qids: list[str]) -> bool:
    """候选题是否与已问过的题"考同一件事"。

    用语义相似度判断，阈值取 dedup 档（0.60，严格）：
    宁可少排除一点，也不要因为"觉得像"就跳过一道好题。
    """
    if not asked_qids:
        return False

    # 只比最近 5 道，避免 O(n²) 且早期题目已经充分覆盖
    recent = asked_qids[-5:]
    th = retriever.threshold_for_use("dedup")
    for qid in recent:
        if qid == hit.id:
            return True
        # 逐条比：把已问题目当查询，看候选题是否在它的近邻里
        try:
            near = retriever.search_bank(
                hit.text, k=5, use="dedup", threshold=th
            )
            if any(n.id == qid for n in near):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def build_query(recent_qa: list[dict], resume_ctx: str = "") -> str:
    """用最近问答 + 简历片段拼检索查询。

    为什么要拼：只用简历会让所有轮次查到同样的题；
    只用最近回答会脱离简历事实。两者拼起来才"顺着候选人的话往下问"。
    """
    parts: list[str] = []
    if resume_ctx:
        parts.append(resume_ctx[:300])
    for qa in recent_qa[-2:]:
        q = str(qa.get("q") or "")[:80]
        a = str(qa.get("a") or "")[:120]
        if a:
            parts.append(f"{q} {a}")
        elif q:
            parts.append(q)
    return " ".join(parts).strip()

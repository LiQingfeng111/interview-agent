"""提问质量度量：回答"这些问题到底问得好不好"。

为什么必须有：
  现有评估（阶段完整/深挖深度/话题命中/防死磕/不重复）全是**流程指标**——
  只能证明系统没崩，证明不了问题问得准。一个把"你做了什么"问十遍的系统
  也能拿到满分，这是最典型的"自我感觉良好"。

两个可自动计算的代理指标（不依赖人工标注，也就不用求人）：
  1. anchor（锚定）：问题里是否出现了简历中的具体项目名或技术栈词
     → 不锚定就是在问通用题，和候选人无关
  2. follow（承接）：问题与前一轮回答是否有实质词汇重叠
     → 不承接就是照本宣科，等于没听人说话

它们不等于"问题好"，但能立刻暴露"问得空"和"没在听"这两类硬伤。
真实质量仍需要人工/LLM 打分，这里只做下限保护。
"""
from __future__ import annotations

import re

_STOP = set("的了是在和与也都很就还你他我她它们这那个什么怎么如何为什么可以能不能有没".split())


def _tokens(text: str) -> set[str]:
    """粗粒度词元：英文词 + 中文二字组。不引分词库，保持零依赖。"""
    text = (text or "").lower()
    eng = set(re.findall(r"[a-z][a-z0-9_+.#-]{1,}", text))
    zh = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    bigrams = {zh[i:i + 2] for i in range(len(zh) - 1)}
    return {t for t in (eng | bigrams) if t not in _STOP}


def anchor_score(question: str, projects: list, min_overlap: int = 2) -> float:
    """问题是否锚定到候选人的具体项目/技术栈。

    注意：不能只做"项目名子串匹配"。实测好问题长这样——
    「这 300 份课件里有多少是扫描版？」——它明显锚定了项目，
    但一个字都没提项目名。只比子串会把好问题判成 0 分，
    这种指标比没有指标更误导。

    所以判据是双重的：项目名/技术栈**出现**，或与该项目的
    名称+技术栈+亮点有 ≥2 个词元重叠。
    """
    q_text = (question or "").lower()
    if not q_text:
        return 0.0
    q_tokens = _tokens(question)
    for p in projects or []:
        name = (getattr(p, "name", "") or "").lower()
        if name and name in q_text:
            return 1.0
        stack = [str(s).lower() for s in (getattr(p, "stack", None) or [])]
        if any(s and len(s) >= 2 and s in q_text for s in stack):
            return 1.0
        blob = " ".join([name, " ".join(stack),
                         " ".join(getattr(p, "highlights", None) or [])])
        if len(q_tokens & _tokens(blob)) >= min_overlap:
            return 1.0
    return 0.0


def follow_score(question: str, prev_answer: str, min_overlap: int = 3) -> float:
    """问题是否承接上一轮回答（有实质词汇重叠）。"""
    if not prev_answer:
        return 0.0
    q, a = _tokens(question), _tokens(prev_answer)
    if not q or not a:
        return 0.0
    return 1.0 if len(q & a) >= min_overlap else 0.0


def summarize(turns: list, projects: list) -> dict:
    """对整场深挖轮次算锚定率与承接率。"""
    probe = [t for t in turns if getattr(t, "phase", "") == "PROJECT_PROBE"]
    if not probe:
        return {"available": False, "reason": "没有深挖轮次"}

    anchor_hits = 0
    follow_hits = 0
    follow_denom = 0
    for i, t in enumerate(turns):
        if getattr(t, "phase", "") != "PROJECT_PROBE":
            continue
        q = getattr(t, "question", "")
        if anchor_score(q, projects) > 0:
            anchor_hits += 1
        prev = turns[i - 1] if i > 0 else None
        prev_ans = getattr(prev, "answer", "") if prev else ""
        if prev_ans:
            follow_denom += 1
            if follow_score(q, prev_ans) > 0:
                follow_hits += 1

    n = len(probe)
    return {
        "available": True,
        "probe_turns": n,
        "anchor_rate": round(anchor_hits / n, 2) if n else 0.0,
        "follow_rate": round(follow_hits / follow_denom, 2) if follow_denom else 0.0,
        "note": "代理指标：锚定率低=在问通用题，承接率低=没在听上一轮回答",
    }


def render_quality(q: dict) -> str:
    if not q.get("available"):
        return f"  （无法评估提问质量：{q.get('reason', '')}）"
    return (
        f"  深挖 {q['probe_turns']} 轮 · 锚定率 {q['anchor_rate']:.0%} · "
        f"承接率 {q['follow_rate']:.0%}\n  {q.get('note', '')}"
    )

"""闭环校验：把"该不该结束"和"证据靠不靠得住"变成可判断的函数。

原来 `can_exit` 只回答 True/False，报告里没有"为什么结束"，评审只能猜。
这里把结束原因做成**闭集**，并补两件审计：
  - evidence 可核验率：多少条评分的证据能在答案原文里找到（配合 INV-04）；
  - 计划进度：每个要素问了没、关了没、还欠什么（喂给下一问的提示）。
"""
from __future__ import annotations

import re

FINISH_REASONS = {
    "coverage_reached",     # 每项目要素都问过了
    "budget_exhausted",     # 预算用尽
    "max_turns",            # 轮次保险丝
    "no_new_facts_3",       # 连续 3 轮没抽到新事实
    "user_quit",            # 用户主动结束
}

REASON_CN = {
    "coverage_reached": "要素覆盖完成",
    "budget_exhausted": "预算用尽",
    "max_turns": "轮次用尽",
    "no_new_facts_3": "连续 3 轮无新信息",
    "user_quit": "用户主动结束",
}


def _norm(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()\-—…]+", "", s or "")


def evidence_audit(turns) -> dict:
    """统计评分证据的可核验率。

    为什么要单独算：INV-04 是"逐条拦截"，这里是"整场体检"。
    两者都要——拦截保证不入库，体检数字才是给评委看的。
    """
    total = checked = 0
    fakes: list[dict] = []
    for t in turns:
        ev = getattr(t, "evaluation", None)
        if not ev:
            continue
        evidence = str(getattr(ev, "evidence", "") or "")
        if not evidence:
            continue
        total += 1
        ans = _norm(getattr(t, "answer", ""))
        core = _norm(evidence).strip("…")
        if core and core in ans:
            checked += 1
        elif len(core) < 4 and core in ans:
            checked += 1
        else:
            fakes.append({"idx": getattr(t, "idx", 0),
                          "evidence": evidence[:40],
                          "answer": getattr(t, "answer", "")[:40]})
    return {
        "total": total,
        "verified": checked,
        "ratio": round(checked / total, 3) if total else 1.0,
        "fakes": fakes[:5],
        "fake_count": len(fakes),
    }


def plan_progress(plan: dict, probe_state, projects: list[str]) -> dict:
    """每个要素的进度：未问 / 已问 / 已答到 / 缺口。"""
    out: dict[str, dict[str, str]] = {}
    names = projects or [str(p.get("name", "")) for p in (plan.get("projects") or [])]
    for name in names:
        try:
            out[name] = probe_state.element_report(name)
        except Exception:
            out[name] = {}
    return out


def coverage_ratio(progress: dict) -> float:
    """处理完的要素占比（答到 + 记缺口都算处理完）。"""
    total = done = 0
    for rep in progress.values():
        for status in rep.values():
            total += 1
            if "未问到" not in str(status):
                done += 1
    return round(done / total, 3) if total else 0.0


def pending_elements(progress: dict, project: str = "") -> list[str]:
    """还欠哪些要素——可以直接注进下一问的提示里。"""
    out: list[str] = []
    for name, rep in progress.items():
        if project and name != project:
            continue
        for element, status in rep.items():
            if "未问到" in str(status):
                out.append(element)
    return out


def should_finish(sm, budget=None) -> tuple[bool, str]:
    """返回 (是否结束, 结束原因)。原因一定落在 FINISH_REASONS 里。"""
    if getattr(sm, "phase", "") == "FEEDBACK":
        return True, "coverage_reached" if _probe_complete(sm) else "max_turns"

    if budget is not None and hasattr(budget, "level") and budget.level() >= 2:
        return True, "budget_exhausted"

    if getattr(sm, "total_turns", 0) >= getattr(sm, "max_turns", 10 ** 6):
        return True, "max_turns"

    if _probe_complete(sm) and getattr(sm, "phase", "") == "PROJECT_PROBE":
        return True, "coverage_reached"

    # 连续 3 轮没新事实（事实账本存在时才判定）
    mem = getattr(sm, "memory", None)
    if mem is not None and hasattr(mem, "stale_streak") and mem.stale_streak() >= 3:
        return True, "no_new_facts_3"
    return False, ""


def _probe_complete(sm) -> bool:
    probe = getattr(sm, "probe", None)
    if probe is None:
        return False
    names = list(getattr(probe, "projects", []) or [])
    if not names:
        return False
    for name in names:
        rep = probe.element_report(name)
        if any("未问到" in str(v) for v in rep.values()):
            return False
    return True


def render_progress(progress: dict) -> str:
    lines = []
    for name, rep in progress.items():
        done = sum(1 for v in rep.values() if "未问到" not in str(v))
        hit = sum(1 for v in rep.values() if "已答到" in str(v))
        lines.append(f"  {name}：处理 {done}/{len(rep)} · 答到 {hit}")
    return "\n".join(lines) if lines else "  计划进度：无"

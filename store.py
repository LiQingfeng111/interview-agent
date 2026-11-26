"""会话持久化：每场面试存一个 JSON，方便复盘与做数据集。

为什么用 JSON 而不是数据库？
学生项目的正确选择：零部署、可 diff、能直接当数据喂给评估脚本。
等真需要并发再换 SQLite，接口保持不变。
"""
from __future__ import annotations

import json
import os
import time

from config import RUNS_DIR
from diagnose import render_diagnosis
from bank import render_bank_meta
from quality import render_quality
from schemas import SessionResult


def _ensure_dir() -> str:
    os.makedirs(RUNS_DIR, exist_ok=True)
    return RUNS_DIR


def save_session(result: SessionResult) -> str:
    os.makedirs(RUNS_DIR, exist_ok=True)
    fname = "run_{ts}_{role}.json".format(
        ts=time.strftime("%Y%m%d_%H%M%S"), role=result.role
    )
    path = os.path.join(RUNS_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
    return path


def list_sessions() -> list[str]:
    if not os.path.isdir(RUNS_DIR):
        return []
    return sorted(
        (os.path.join(RUNS_DIR, f) for f in os.listdir(RUNS_DIR) if f.endswith(".json")),
        reverse=True,
    )


def load_session(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def render_audit(meta: dict) -> str:
    """审计区块：6 行数字，回答"这一场可不可信"。

    为什么放报告最前面：评委第一眼要看的不是分数，是"你这个分数凭什么"。
    """
    a = meta.get("audit") or {}
    if not a:
        return ""
    b = a.get("budget") or {}
    ev = a.get("evidence") or {}
    lines = [
        "=" * 62,
        f"审计 · 会话 {a.get('session_id', '-')} · 事件 {a.get('events', 0)} 条",
        f"结束原因：{a.get('finish_reason_cn', '-')}（{a.get('finish_reason', '-')}）"
        f" · 要素覆盖 {meta.get('coverage_ratio', 0):.0%}",
        f"不变量：fatal {a.get('fatal', 0)} · warn {a.get('warn', 0)}"
        + ("（本场不可信：fatal ≥3）" if a.get("fatal", 0) >= 3 else ""),
        f"评分证据：{ev.get('verified', 0)}/{ev.get('total', 0)} 可在答案原文找到"
        f"（可核验 {ev.get('ratio', 0):.0%}，疑似编造 {ev.get('fake_count', 0)} 条）",
        f"成本：{b.get('tokens', 0)}/{b.get('budget_tokens', 0)} token · "
        f"¥{b.get('cost_cny', 0):.4f} · 降级 {b.get('level', 0)} 级"
        + (f"（{', '.join(b.get('degraded') or [])}）" if b.get("degraded") else ""),
        "=" * 62,
        "",
    ]
    if a.get("chain_problems"):
        lines.insert(1, f"! 事件链异常：{a['chain_problems'][0]}")
    return "\n".join(lines)


def render_report(data: dict) -> str:
    """把一次会话渲染成可读报告（终端/README 展示用）。"""
    meta = data.get("meta", {})
    stats = meta.get("stats", {})
    sm = meta.get("sm", {})
    lines = [
        "=" * 62,
        f"面试报告 · 岗位 {data.get('role')}",
        f"轮次 {stats.get('count', 0)} · 均分 {stats.get('avg', 0)} · 最弱维度 {stats.get('weakest_dim', '-')}",
        "=" * 62,
        "",
        "【各维度得分】",
    ]
    for k, v in (stats.get("dims") or {}).items():
        bar = "█" * int(round(float(v)))
        lines.append(f"  {k:<10} {v:<5} {bar}")

    by_phase = stats.get("by_phase") or {}
    if by_phase:
        lines.append("")
        lines.append("【各阶段均分】（避免单一均分掩盖短板）")
        order = ["OPENING", "SELF_INTRO", "PROJECT_PROBE", "TECH_QA", "CANDIDATE_ASK"]
        for ph in order + [p for p in by_phase if p not in order]:
            if ph not in by_phase:
                continue
            b = by_phase[ph]
            bar = "█" * int(round(float(b["avg"])))
            flag = "  ← 最弱" if ph == stats.get("weakest_phase") else ""
            lines.append(f"  {ph:<14} {b['avg']:<5} ({b['count']}轮) {bar}{flag}")

    lines.append("")
    lines.append("【能力画像】（这个人强不强，与上面的系统指标是两层）")
    profile = meta.get("profile") or {}
    try:
        from profile import render_profile

        lines.append(render_profile(profile))
    except Exception:
        lines.append("（画像渲染失败）")
    if profile.get("available"):
        lines.append(f"  {profile.get('disclaimer', '')}")

    lines.append("")
    # 阶段简报：结构化聚合（不调模型），一眼看"知道什么/还差什么"
    brief = meta.get("briefing") or {}
    if brief:
        lines.append("【阶段简报】（证据聚合：已答到 / 回避 / 已确认 / 可疑 / 待补）")
        for key, label in (("covered", "已答到"), ("evaded", "被回避"),
                           ("verified", "已确认事实"), ("suspicious", "可疑/矛盾"),
                           ("weak_points", "薄弱点"), ("pending", "待补")):
            items = brief.get(key) or []
            if items:
                lines.append(f"  {label}：" + "；".join(str(x) for x in items[:6]))
        lines.append("")

    lines.append("【深挖要素覆盖】")
    lines.append("  说明：统计的是候选人**回答里实际讲到**的要素，不是问过的题数。")
    covered = sm.get("covered_elements") or {}
    if not meta.get("plan_enabled"):
        lines.append("  （未启用 --plan，本场由状态机实时提问）")
    if covered:
        try:
            from config import ELEMENT_NAMES
        except Exception:
            ELEMENT_NAMES = []
        element_report = sm.get("element_report") or {}
        for proj, got in covered.items():
            got = list(got or [])
            ratio = f"{len(got)}/{len(ELEMENT_NAMES)}" if ELEMENT_NAMES else str(len(got))
            lines.append(f"  {proj}: 已答到 {ratio}")
            detail = element_report.get(proj) or {}
            for element, status in detail.items():
                mark = "✓" if status == "已答到" else "·"
                lines.append(f"      {mark} {element}: {status}")
    else:
        lines.append("  （无深挖记录）")

    lines.append("")
    lines.append("【技术题库】（本场用了哪些题，以及题库整体状态）")
    lines.append(render_bank_meta(meta.get("bank_asked") or []))

    lines.append("")
    lines.append("【简历体检】（输入信息够不够，直接决定深挖质量）")
    rq = meta.get("resume_quality") or {}
    if rq.get("projects"):
        lines.append(f"  项目 {rq['projects']} 个 · 有技术栈 {rq.get('stack_ratio', 0):.0%} · "
                     f"有量化指标 {rq.get('metrics_ratio', 0):.0%}")
        if rq.get("advice"):
            lines.append(f"  ! {rq['advice']}")
    else:
        lines.append("  （没有简历体检数据）")

    # 深挖树：回答"为什么换项目/为什么不再问"，每条剪枝带原因
    tree_text = meta.get("tree_text") or ""
    if tree_text and tree_text != "（无深挖记录）":
        lines.append("")
        lines.append("【深挖树】（节点=一问，缩进=追问关系，括号=关闭原因）")
        for ln in tree_text.splitlines()[:18]:
            lines.append("  " + ln)

    # 矛盾追问记录：有才渲染，没有就省略（别让报告全是空区块）
    mem = meta.get("memory") or {}
    if mem.get("contradictions"):
        lines.append("")
        lines.append("【矛盾追问记录】（账本记住了旧值，才有这一问）")
        for c in mem["contradictions"][:6]:
            state = "已追问" if c.get("questioned") else "未追问"
            lines.append(f"  #{c.get('turn_idx', 0)} {c.get('project', '')} · {c.get('key', '')}"
                         f"：{c.get('old', '')}  →  {c.get('new', '')} [{state}]")

    lines.append("")
    web = meta.get("web") or {}
    lines.append("【联网核对】（有资料才判对错，没资料一律标 unverifiable）")
    if not web.get("enabled"):
        lines.append("  本场未开启联网（加 --web 开启）")
    else:
        facts = []
        for t in data["turns"]:
            ev = t.get("evaluation") or {}
            if ev.get("fact_status") == "error":
                facts.append((t["idx"], ev.get("fact_detail", ""), t["answer"][:40]))
        lines.append(f"  检索覆盖 {web.get('turns_with_refs', 0)} 轮 · 查出技术表述错误 {len(facts)} 处")
        for idx, detail, ans in facts[:5]:
            lines.append(f"  - 第 {idx} 轮：{detail or '与资料冲突'}｜他说：{ans}")

    lines.append("")
    lines.append("【提问质量】（流程指标之外的下限检查）")
    lines.append(render_quality(meta.get("quality") or {}))

    lines.append("")
    lines.append("【短板诊断】（区分「不会」与「没意识到」，训练动作不同）")
    lines.append(render_diagnosis(meta.get("diagnosis") or []))

    lines.append("")
    lines.append("【深挖轨迹】")
    for t in data.get("turns", []):
        ev = t.get("evaluation") or {}
        lines.append(
            f"  #{t['idx']:<2} [{t['phase']}/L{t['ladder_level']}] "
            f"{ev.get('verdict', '-'):<7} {t['question'][:46]}"
        )
    lines.append("")
    lines.append("【反馈】")
    lines.append(data.get("feedback", ""))
    lines.append("")
    lines.append(f"最深层级：{sm.get('probe', {}).get('ladder_name') or _deepest_from_turns(data)}")
    audit_text = render_audit(meta)
    if audit_text:
        lines = audit_text.rstrip("\n").splitlines() + [""] + lines
    return "\n".join(lines)


def _deepest_from_turns(data: dict) -> str:
    from state_machine import LADDER

    best = 0
    for t in data.get("turns", []):
        if t.get("phase") == "PROJECT_PROBE":
            best = max(best, int(t.get("ladder_level", 0)))
    return LADDER[min(best, len(LADDER) - 1)]

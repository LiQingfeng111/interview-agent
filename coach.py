"""教练层：分两个粒度。

  - `Coach`      整场结束后的五段总结（总评 / 答对 / 答虚 / 下一步追问 / 训练动作）；
  - `TurnCoach`  评估模式下的**单轮**点评（为什么扣分 + 缺什么 + 示范回答）。

为什么分两个：一场结束给总结是"事后复盘"，每轮给点评是"边打边教"。
    后者对心态的影响大得多——分数掉下来时立刻知道为什么，比结尾被总结一顿更受用。
    但单轮点评每次要多一次模型调用，所以做成开关（评估模式），默认关。
"""
from __future__ import annotations

import json

from config import COACH_SYSTEM, TURN_COACH_SYSTEM
from diagnose import diagnose, render_diagnosis

FALLBACK_TEMPLATE = [
    "先给结论与数字（做了什么、结果多少）。",
    "再给依据（怎么测的、对比基线是什么）。",
    "最后给代价与边界（这个方案的缺点、什么情况下会失效）。",
]

# 各维度没打满时，该补什么（规则兜底，真实模型会写更具体的话）
DIM_HINT = {
    "accuracy": "有事实错误或含糊表述，先把数字/术语核对清楚",
    "depth": "停在「做了什么」，补「怎么实现、为什么这么做」",
    "tradeoff": "没提替代方案与代价，补一句「我否掉了 X，因为 Y」",
    "boundary": "没讲方案什么时候失效，补「数据量/并发翻倍会先崩在哪」",
    "clarity": "主线不清，用「问题→做法→结果」三段讲",
}


def _clip(text: str, n: int = 60) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


# ====================================================================== 整场总结
class Coach:
    """整场教练：按 config.COACH_SYSTEM 的五段结构给总结。

    模型失败或 mock 模式 → 走规则版，五段结构照给（报告不该因为模型抖动变空）。
    """

    def __init__(self, llm):
        self.llm = llm

    def feedback(self, sm, turns, stats, role_name: str = "") -> str:
        digest = self._digest(turns)
        if not getattr(self.llm, "is_mock", False):
            try:
                text = self.llm.chat(
                    [{"role": "system", "content": COACH_SYSTEM},
                     {"role": "user", "content": f"岗位方向：{role_name}\n{self._stats_line(stats)}\n\n{digest}"}],
                    task="coach",
                )
                if text and "【总评】" in text:
                    return text.strip()
            except Exception:
                pass
        return self._rule_feedback(sm, turns, stats, role_name)

    # ---------------------------------------------------------------- 工具
    def _stats_line(self, stats: dict) -> str:
        dims = stats.get("dims") or {}
        dim_s = " / ".join(f"{k} {v}" for k, v in dims.items())
        return (f"轮次 {stats.get('count', 0)} · 均分 {stats.get('avg', 0)} · "
                f"最弱维度 {stats.get('weakest_dim', '')} · 最弱阶段 {stats.get('weakest_phase', '')}\n"
                f"维度均分：{dim_s}")

    def _digest(self, turns) -> str:
        lines = []
        for t in turns[-12:]:
            ev = getattr(t, "evaluation", None)
            verdict = getattr(ev, "verdict", "") if ev else ""
            lines.append(f"[{t.phase}] 问：{t.question[:60]}\n答：{(t.answer or '')[:160]}\n判定：{verdict}")
        return "\n".join(lines)

    def _element_report(self, sm) -> dict:
        report = {}
        try:
            for p in (sm.probe.projects or []):
                report[p] = sm.probe.element_report(p)
        except Exception:
            pass
        return report

    def _rule_feedback(self, sm, turns, stats, role_name: str) -> str:
        dims = stats.get("dims") or {}
        answered = [t for t in turns if (t.answer or "").strip()]
        good = [t for t in answered if getattr(t.evaluation, "verdict", "") == "pass"]
        weak = [t for t in answered if getattr(t.evaluation, "verdict", "") == "fail"]
        weakest = stats.get("weakest_dim", "")

        # 好的证据：直接引用原话片段（不编造）
        good_lines = [f"「{_clip(t.answer, 40)}」——回答里带了具体做法。" for t in good[:2]]
        if not good_lines:
            good_lines = ["这次没有出现能直接当范例的回答，先把「做了什么 + 结果数字」讲清。"]

        bad_lines = []
        for t in (weak or answered)[:2]:
            dim = getattr(t.evaluation, "weakest", weakest) if t.evaluation else weakest
            bad_lines.append(f"「{_clip(t.answer, 30)}」——缺{DIM_HINT.get(dim, '支撑证据')}。")
        if not bad_lines:
            bad_lines = ["整体没有明显塌陷，但缺" + DIM_HINT.get(weakest, "量化证据") + "。"]

        diag_items = []
        try:
            diag_items = diagnose(self._element_report(sm), dims, weakest)
        except Exception:
            diag_items = []
        diag_text = render_diagnosis(diag_items) if diag_items else ""

        follower = self._followups(weakest)
        actions = self._actions(diag_items, weakest)

        parts = [
            "【总评】" + (f"最致命的是 {weakest}：{DIM_HINT.get(weakest, '讲得不够具体')}。" if weakest
                          else "整体没有塌陷，但具体度不够。"),
            "【哪里答对了】",
        ]
        parts += [f"{i+1}. {x}" for i, x in enumerate(good_lines)]
        parts.append("【哪里答虚了】")
        parts += [f"{i+1}. {x}" for i, x in enumerate(bad_lines)]
        if diag_text:
            parts.append(diag_text)
        parts.append("【面试官下一步会怎么追】")
        parts += [f"{i+1}. {x}" for i, x in enumerate(follower)]
        parts.append("【下一步训练动作】")
        parts += [f"{i+1}. {x}" for i, x in enumerate(actions)]
        return "\n".join(parts)

    def _followups(self, weakest: str) -> list[str]:
        return {
            "accuracy": ["这个数字具体怎么测的？谁标的？", "换一批数据，结论还会成立吗？"],
            "depth": ["这一步具体怎么实现的？中间哪一步最容易出错？", "如果不用这个组件，你会怎么替代？"],
            "tradeoff": ["你否掉了哪个方案？为什么否？", "这个代价你愿意付到多大？"],
            "boundary": ["数据量翻十倍先崩在哪？", "这套方案什么情况下会失效？"],
            "clarity": ["用一句话总结你的方案解决了什么。", "从请求进来到返回，中间有几步？"],
        }.get(weakest, ["这块能不能举一个具体例子？", "如果重做一次，你会改哪一步？"])

    def _actions(self, diag_items: list[dict], weakest: str) -> list[str]:
        acts = [d["action"] for d in diag_items[:3] if d.get("action")]
        if len(acts) < 3:
            acts += [
                "每个项目准备一句「我否掉了 X，因为 Y」——覆盖权衡维度。",
                "为每个指标写明 baseline 与测量方法，别只报数值。",
                "准备一个失败案例：卡在哪、怎么定位、最后怎么改。",
            ]
        return acts[:3]


class TurnCoach:
    """单轮讲解器：评分 → 为什么 + 缺什么 + 示范。"""

    def __init__(self, llm, enabled: bool = False):
        self.llm = llm
        self.enabled = bool(enabled)
        self.history: list[dict] = []

    # ------------------------------------------------------------ 主入口
    def review(self, question: str, answer: str, evaluation=None,
               project: str = "", phase: str = "", points: list[str] | None = None) -> dict:
        """返回 {verdict, score, why, gaps, ideal, tips, source}。"""
        if not self.enabled:
            return {"enabled": False}

        ev = evaluation
        dims = {d: getattr(ev, d, 0) for d in
                ("accuracy", "depth", "tradeoff", "boundary", "clarity")} if ev else {}
        score = round(sum(dims.values()) / len(dims), 2) if dims else 0.0
        verdict = getattr(ev, "verdict", "") if ev else ""
        weakest = getattr(ev, "weakest", "") if ev else ""

        # 规则版先给一份（模型挂了也有东西可看）
        base = self._rule_review(dims, verdict, weakest, points or [])

        llm_out = self._ask_llm(question, answer, dims, verdict, weakest,
                                project, phase, points or [])
        if llm_out:
            base.update({k: llm_out[k] for k in ("why", "gaps", "ideal") if llm_out.get(k)})
            base["source"] = "llm"
        base.setdefault("source", "rule")
        out = {"enabled": True, "score": score, "verdict": verdict,
               "weakest": weakest, "dims": dims, **base}
        self.history.append({"question": question, "answer": answer, **out})
        return out

    # ------------------------------------------------------------ 规则版
    def _rule_review(self, dims: dict, verdict: str, weakest: str,
                     points: list[str]) -> dict:
        gaps: list[str] = []
        if weakest and weakest in DIM_HINT:
            gaps.append(DIM_HINT[weakest])
        for d, v in dims.items():
            if v <= 2 and d != weakest and d in DIM_HINT:
                gaps.append(DIM_HINT[d])
        if points:
            gaps.append("这题原本想听到：" + "；".join(points[:3]))
        if not gaps:
            gaps = ["已经在合格线上，下一步补具体数字与失败案例"]
        fix = dims.get(weakest, 3)
        why = (f"判定 {verdict or '未判定'}，均分 {sum(dims.values()) / max(1, len(dims)):.1f}。"
               f"最弱在 {weakest or '—'}（{fix} 分）——"
               f"{DIM_HINT.get(weakest, '这块没讲透')}。" if dims else "本轮没有评分。")
        return {"why": why, "gaps": gaps[:4], "ideal": "", "tips": FALLBACK_TEMPLATE}

    # ------------------------------------------------------------ 模型版
    def _ask_llm(self, question: str, answer: str, dims: dict, verdict: str,
                 weakest: str, project: str, phase: str, points: list[str]) -> dict:
        if getattr(self.llm, "is_mock", False):
            return {}
        user = (
            f"岗位方向阶段：{phase}\n项目：{project}\n"
            f"问题：{question}\n"
            f"候选人回答：{answer}\n"
            f"各维度得分：{json.dumps(dims, ensure_ascii=False)}\n"
            f"判定：{verdict}；最弱维度：{weakest}\n"
            + (f"本题想听到的要点：{'；'.join(points)}\n" if points else "")
            + "请输出 JSON：{why, gaps[], ideal, tips[]}"
        )
        try:
            data = self.llm.chat_json(
                [{"role": "system", "content": TURN_COACH_SYSTEM},
                 {"role": "user", "content": user}],
                task="turn_coach",
                hint={"phase": phase, "project": project},
            )
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        out = {}
        why = str(data.get("why") or "").strip()
        if why:
            out["why"] = _clip(why, 160)
        gaps = data.get("gaps") or []
        if isinstance(gaps, str):
            gaps = [gaps]
        gaps = [_clip(str(g), 80) for g in gaps if str(g).strip()][:4]
        if gaps:
            out["gaps"] = gaps
        ideal = str(data.get("ideal") or "").strip()
        if len(ideal) >= 20:            # 太短说明模型没好好写，就别糊弄用户
            out["ideal"] = _clip(ideal, 600)
        tips = data.get("tips") or []
        if isinstance(tips, str):
            tips = [tips]
        tips = [_clip(str(t), 80) for t in tips if str(t).strip()][:3]
        if tips:
            out["tips"] = tips
        return out


def render_card(card: dict) -> str:
    """终端里渲染一张评估卡（CLI 用）。"""
    if not card.get("enabled"):
        return ""
    lines = [f"【本轮点评】{card.get('verdict', '')} · 均分 {card.get('score', 0):.1f}"]
    lines.append(f"  为什么：{card.get('why', '')}")
    if card.get("gaps"):
        lines.append("  缺什么：")
        for g in card["gaps"]:
            lines.append(f"    · {g}")
    if card.get("ideal"):
        lines.append(f"  示范回答：{card['ideal']}")
    if card.get("tips"):
        lines.append("  怎么改：" + " / ".join(card["tips"]))
    return "\n".join(lines)

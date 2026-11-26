"""Agent 2：评分官。

只做一件事：把"一问一答"变成结构化分数。
关键工程点：
  - 强制 JSON + 后处理规整（模型一定会给脏数据）
  - rubric 锚点写进 prompt（不给锚点，模型会给"感情分"）
  - 分数落盘，供教练和回归脚本复用
"""
from __future__ import annotations

import json

from config import ELEMENT_KEYWORDS, ELEMENT_NAMES, EVALUATOR_SYSTEM, RUBRIC_ANCHORS, RUBRIC_DIMS
from llm import BaseLLM
from schemas import Evaluation, normalize_eval


def detect_covered(answer: str) -> list[str]:
    """规则版要素覆盖检测（mock 与降级时使用）。

    为什么需要它：候选人在「目标」那一问里可能顺带讲了 baseline 和指标。
    真人面试官会跳过，不会机械地再问一遍——但跳过需要先知道"讲没讲"。
    真实模型由 LLM 判断（语义更准），这里是指令不通时的兜底。
    """
    text = (answer or "").lower()
    hit = []
    for element, words in ELEMENT_KEYWORDS.items():
        if any(w.lower() in text for w in words):
            hit.append(element)
    return [e for e in ELEMENT_NAMES if e in hit]


def build_rubric_text() -> str:
    lines = []
    for dim in RUBRIC_DIMS:
        anchors = RUBRIC_ANCHORS[dim]
        line = f"- {dim}: " + "；".join(f"{k}分={v}" for k, v in sorted(anchors.items()))
        lines.append(line)
    return "\n".join(lines)


def judge_target_status(answer: str, element: str, covered: list[str]) -> str:
    """判定候选人对某个要素是「会 / 回避 / 不知道」。

    为什么必须区分三态？因为后续处理完全不同：
      covered → 跳过，不再问
      evaded  → 说明在绕，再追一次（用户定的策略：只追一次）
      unknown → 直接记缺口，别浪费轮次

    规则版判据（真实模型由 LLM 判断，这里是兜底）：
      讲到了 → covered
      回答很长但没讲到该点 → evaded（顾左右而言他）
      回答很短 / 出现"不知道""没想过" → unknown
    """
    if element in (covered or []):
        return "covered"
    text = (answer or "").strip()
    low = text.lower()
    if any(w in low for w in ("不知道", "不清楚", "没想过", "没做", "忘了", "没有考虑")):
        return "unknown"
    if len(text) >= 60:
        return "evaded"
    return "unknown"


def reconcile_covered(llm_covered: list[str], answer: str) -> list[str]:
    """把评分官声称覆盖的要素与规则检测交叉验证。

    为什么需要：实测本地小模型（Qwen2.5-1.5B）会把 5 个要素**全填成已覆盖**，
    于是覆盖率恒为 5/5、短板诊断恒为空——比漏判更糟，因为它让报告彻底失效。

    策略：
      - 模型只声称 1-2 个 → 信任（模型通常比关键词准）
      - 模型声称 3 个以上 → 与规则检测取交集（一次回答覆盖四五个要素极罕见）
    """
    covered = [e for e in (llm_covered or []) if e]
    rule = detect_covered(answer)
    if not covered:
        return rule
    if len(covered) <= 2:
        return covered
    return [e for e in covered if e in rule]


class Evaluator:
    def __init__(self, llm: BaseLLM):
        self.llm = llm

    def evaluate(
        self,
        question: str,
        answer: str,
        *,
        phase: str = "",
        ladder_name: str = "",
        project: str = "",
        target_element: str = "",
        expected_points: list[str] | None = None,
        web_refs: str = "",
    ) -> Evaluation:
        mode = "mock" if getattr(self.llm, "is_mock", False) else "real"
        if not answer.strip():
            # evidence 必须是**答案原文的子串**（INV-04 会逐条校验）。
            # 这里原本写死 "未作答"，被审计层抓出来：那是编造证据——
            # 答案里根本没有这四个字，报告却拿它当评分依据。
            return Evaluation(
                accuracy=1, depth=1, tradeoff=1, boundary=1, clarity=1,
                verdict="fail", weakest="depth", evidence="",
                followup_hint="先让候选人开口",
                target_element=target_element, target_status="unknown",
            )

        system = (
            EVALUATOR_SYSTEM
            + "\n评分锚点：\n"
            + build_rubric_text()
            + "\n\n另外请判断这段回答**实际讲到了**下列哪些要素，填进 covered 数组：\n"
            + "\n".join(f"- {n}" for n in ELEMENT_NAMES)
            + "\n只填真正讲到并给出信息量的，泛泛提及不算；没讲到就给空数组。"
        )
        if target_element:
            system += (
                f"\n\n本次追问针对的要素是「{target_element}」。请额外输出 target_status：\n"
                "- covered：讲到了该要素且有实质信息\n"
                "- evaded：说了不少但绕开了该点（顾左右而言他）\n"
                "- unknown：直接承认不知道或没回答\n"
            )
        user = (
            f"面试阶段：{phase}\n"
            f"追问层级：{ladder_name or '无'}\n"
            f"深挖项目：{project or '无'}\n"
            f"本次目标要素：{target_element or '无'}\n"
        )
        if expected_points:
            # 题库题带标准要点：让评分有据可依，而不是靠模型"感觉"
            user += (
                "本题的标准要点（候选人答到几条，accuracy/depth 就按覆盖程度给分）：\n"
                + "\n".join(f"- {p}" for p in expected_points)
                + "\n"
            )
        user += (
            f"\n【问题】\n{question}\n\n"
            f"【候选人回答】\n{answer[:2000]}\n\n"
            "只输出 JSON。"
        )
        if web_refs:
            # 有了外部资料才要求事实核对；没有资料时一律 unverifiable，
            # 让模型凭空"判断对错"是最容易产生假证据的地方。
            user = user.replace(
                "只输出 JSON。",
                "【联网参考资料（用于事实核对，不是评分标准）】\n"
                + web_refs[:900]
                + "\n\n如果候选人这句技术表述与参考资料冲突，fact_check.status 填 error 并在 "
                  "detail 里一句话说明冲突点；与资料一致填 ok；资料不足以判断填 unverifiable。"
                  "注意：不要因为资料里没提到就算 error。\n\n只输出 JSON。",
            )
        try:
            data = self.llm.chat_json(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                task="evaluator",
                hint={"phase": phase, "target_element": target_element},
            )
            data.setdefault("target_element", target_element)
            ev = Evaluation.from_dict(normalize_eval(data), mode=mode)
            # 覆盖要素交叉验证（防止弱模型"全填已覆盖"让报告失真）
            ev.covered = reconcile_covered(ev.covered, answer)
            if target_element and ev.target_status == "covered" and target_element not in ev.covered:
                ev.target_status = judge_target_status(answer, target_element, ev.covered)
            if target_element and not ev.target_status:
                ev.target_status = judge_target_status(answer, target_element, ev.covered)
            return ev
        except Exception:
            return self._heuristic(answer, target_element)

    def _heuristic(self, answer: str, target_element: str = "") -> Evaluation:
        """LLM 挂了也要能打分：按长度、数字、权衡词做粗估。

        这不是"凑合"，是真实系统的降级要求——评估环节挂掉会污染
        整场记录，宁可给保守分也不能丢数据。
        """
        n = len(answer)
        has_num = any(c.isdigit() for c in answer)
        trade_words = ["但是", "不过", "代价", "权衡", "取舍", "替代", "对比", "因为", "所以"]
        has_trade = any(w in answer for w in trade_words)
        depth = 3 if n > 80 else (2 if n > 30 else 1)
        covered = detect_covered(answer)
        return Evaluation(
            accuracy=3,
            depth=min(4, depth + (1 if has_num else 0)),
            tradeoff=3 if has_trade else 2,
            boundary=3 if ("失败" in answer or "失效" in answer) else 2,
            clarity=3 if n < 600 else 2,
            verdict="partial",
            weakest="boundary" if "失效" not in answer else "tradeoff",
            evidence=answer[:30],
            followup_hint="（降级评分，请人工复核）",
            fact_status="unverifiable",
            fact_detail="降级评分，未做事实核对",
            covered=covered,
            target_element=target_element,
            target_status=judge_target_status(answer, target_element, covered) if target_element else "",
        )


def score_turns(turns: list) -> dict:
    """汇总统计，供教练和报告使用。

    为什么不只给一个全场均分？
    实测：深挖强(3.05) + 技术弱(1.73) 的候选人，全场均分被拉成 2.81，
    看着"还行"，实际短板被 12 个深挖轮次稀释了。**单一均分掩盖阶段差异**，
    所以这里额外按阶段拆分，让每个阶段的短板都能暴露。
    """
    evals = [t.evaluation for t in turns if t.evaluation]
    if not evals:
        return {"count": 0, "avg": 0.0, "dims": {}, "verdicts": {}, "by_phase": {}}
    dims = {d: round(sum(getattr(e, d) for e in evals) / len(evals), 2) for d in RUBRIC_DIMS}
    verdicts: dict[str, int] = {}
    for e in evals:
        verdicts[e.verdict] = verdicts.get(e.verdict, 0) + 1

    by_phase: dict[str, dict] = {}
    for t in turns:
        if not t.evaluation:
            continue
        bucket = by_phase.setdefault(t.phase, {"scores": [], "count": 0})
        bucket["scores"].append(t.evaluation.avg)
        bucket["count"] += 1
    by_phase = {
        ph: {"avg": round(sum(b["scores"]) / len(b["scores"]), 2), "count": b["count"]}
        for ph, b in by_phase.items()
    }

    return {
        "count": len(evals),
        "avg": round(sum(e.avg for e in evals) / len(evals), 2),
        "dims": dims,
        "verdicts": verdicts,
        "weakest_dim": min(dims, key=lambda k: dims[k]) if dims else "",
        "by_phase": by_phase,
        "weakest_phase": (
            min(by_phase, key=lambda k: by_phase[k]["avg"]) if by_phase else ""
        ),
    }


def dump_eval(e: Evaluation) -> str:
    return json.dumps(e.to_dict(), ensure_ascii=False, indent=2)

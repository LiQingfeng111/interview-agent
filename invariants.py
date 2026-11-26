"""运行时约束（不变量）：把"靠提示词祈祷"换成"用代码断言"。

为什么这一步价值最大：
  现有 8 类规则（同一要素最多问 2 次、不重复、问题不能太短、评分要有证据、
  结束原因必须合法……）散在 4 个文件里，各自为政，**没人能回答"这一场违规了几次"**。
  把它们变成一张声明式表之后：加规则 = 加一行 + 一个 case，跑完一场就有 fatal/warn 计数。

两级严重度（借 DSPy Assertions 的 Assert/Suggest 语义，不引入框架）：
  - fatal：本次输出不采纳，回退兜底（问题重生成 / 评分走启发式），并记账；
  - warn ：只记账，报告里可见。

判定原则：**能用代码判的不问模型**。所以 evidence 是不是答案子串、
verdict 与分数是否自相矛盾，这些一律代码判。
"""
from __future__ import annotations

import dataclasses
import re
from typing import Callable

from config import VERDICT_THRESHOLDS


@dataclasses.dataclass(frozen=True)
class Violation:
    id: str
    severity: str          # "fatal" | "warn"
    desc: str
    detail: str = ""
    fix: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"[\s，。！？、,.!?：:；;“”\"'（）()\-—…]+", "", s or "")


def _evidence_in_answer(evidence: str, answer: str) -> bool:
    """证据必须是答案的子串（去标点空白后比较）。

    允许两种放宽，避免误杀正常引用：
      1. 证据很短（<4 字）时按"关键词出现"判；
      2. 证据是答案的连续片段但被标点打断 → 归一化后比较。
    """
    ev, ans = _norm(evidence), _norm(answer)
    if not ev:
        return False
    if ev in ans:
        return True
    if len(ev) < 4:
        return ev in ans
    # 允许首尾省略号引用
    core = ev.strip("…")
    return bool(core) and core in ans


# ------------------------------------------------------------------ 规则
def _inv01(ctx: dict) -> Violation | None:
    """同一(项目,要素)追问不超过 2 次。"""
    if (ctx.get("element_attempts") or 0) <= 2:
        return None
    return Violation("INV-01", "fatal", "同一要素追问超过 2 次",
                     detail=f"{ctx.get('project')}/{ctx.get('element')} 已问 {ctx.get('element_attempts')} 次",
                     fix="换要素或换项目")


def _inv02(ctx: dict) -> Violation | None:
    """与最近若干问不能高度相似（防复读）。"""
    q = ctx.get("question") or ""
    recent = ctx.get("recent_questions") or []
    for prev in recent[-6:]:
        if _similar(q, prev) >= 0.62:
            return Violation("INV-02", "fatal", "问题与最近的问题过于相似",
                             detail=f"「{q[:16]}」≈「{prev[:16]}」", fix="换角度或换要素")
    return None


def _similar(a: str, b: str) -> float:
    """字符二元组 Jaccard，和 interviewer._is_repeat 同一口径。"""
    def grams(s: str) -> set[str]:
        s = _norm(s)
        return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def _inv03(ctx: dict) -> Violation | None:
    """问题必须够长、且不能是模型的内心独白。"""
    q = (ctx.get("question") or "").strip()
    if len(q) < 8:
        return Violation("INV-03", "warn", "问题过短", detail=q[:20], fix="重新生成或走兜底")
    if re.search(r"(^\W*(好的|让我|我需要|作为面试官|首先|接下来我))", q):
        return Violation("INV-03", "warn", "问题里出现内心独白", detail=q[:24], fix="清洗前缀")
    return None


def _inv04(ctx: dict) -> Violation | None:
    """评分证据必须是候选人答案的子串，否则视为编造。

    这是当前最大的漏洞：evidence 一直没校验，模型完全可以编一句
    "候选人说用了 BM25"来支撑评分。评委问一句"这句在哪"，就露馅了。
    """
    if "eval" not in ctx:
        return None          # 本轮没有评分，不适用（不是"缺证据"）
    ev = ctx.get("eval") or {}
    evidence = str(ev.get("evidence") or "")
    answer = str(ctx.get("answer") or "")
    if not evidence:
        return Violation("INV-04", "warn", "评分没有给证据", fix="要求 evaluator 补 evidence")
    if not _evidence_in_answer(evidence, answer):
        return Violation("INV-04", "fatal", "评分证据不是答案原文（疑似编造）",
                         detail=f"evidence=「{evidence[:28]}」", fix="改用启发式评分")
    return None


def _inv05(ctx: dict) -> Violation | None:
    """verdict 与分数必须一致（pass 却低分 = 尺子坏了）。"""
    ev = ctx.get("eval") or {}
    verdict, score = ev.get("verdict"), ev.get("score")
    if verdict is None or score is None:
        return None
    try:
        s = float(score)
    except (TypeError, ValueError):
        return Violation("INV-05", "warn", "分数不是数字", detail=str(score))
    # 阈值必须跟 config 走：写死 3.0/4.0 会与校准结果脱节，
    # 结果就是"尺子改了、体检没跟着改"，一条不一致都报不出来（实测 10/12 没被发现）
    pass_min, partial_min = VERDICT_THRESHOLDS.get("real", (4.0, 2.5))
    if verdict == "pass" and s < pass_min:
        return Violation("INV-05", "warn", "pass 但分数低于合格线", detail=f"score={s} < {pass_min}")
    if verdict == "fail" and s >= partial_min:
        return Violation("INV-05", "warn", "fail 但分数到了可评区间", detail=f"score={s} >= {partial_min}")
    if ctx.get("verdict_conflict"):
        return Violation("INV-05", "warn", "模型判定与阈值判定不一致（以阈值为准）",
                         detail=f"model={ctx.get('model_verdict')} score={s}")
    return None


def _inv06(ctx: dict) -> Violation | None:
    """问题要么在计划内，要么显式记 deviation。"""
    if ctx.get("plan_empty"):
        return None
    if ctx.get("in_plan") or ctx.get("deviation_reason"):
        return None
    return Violation("INV-06", "warn", "问题偏离计划且未记录原因",
                     detail=f"target={ctx.get('element') or '(空)'}", fix="补 deviation_reason")


def _inv07(ctx: dict) -> Violation | None:
    """阶段流转原因必须在白名单里。"""
    reason = ctx.get("transition_reason")
    if not reason:
        return None
    allowed = {"阶段完成", "轮次用尽", "要素收敛", "用户退出", "预算用尽", "无新信息"}
    if reason not in allowed:
        return Violation("INV-07", "fatal", "阶段流转原因不在白名单",
                         detail=str(reason), fix="改用白名单词或补进白名单")
    return None


def _inv08(ctx: dict) -> Violation | None:
    """超预算必须已经降级，不能悄悄超。"""
    level = ctx.get("budget_level")
    if level is None or level < 1:
        return None
    if ctx.get("degraded_noted"):
        return None
    return Violation("INV-08", "fatal", "预算超限但没有记录降级",
                     detail=f"level={level}", fix="写 budget.exceeded 并降级")


@dataclasses.dataclass(frozen=True)
class Invariant:
    id: str
    when: str
    severity: str
    desc: str
    check: Callable[[dict], Violation | None]


INVARIANTS: list[Invariant] = [
    Invariant("INV-01", "turn.asked", "fatal", "同一要素追问 ≤2 次", _inv01),
    Invariant("INV-02", "turn.asked", "fatal", "不复读（相似度 <0.62）", _inv02),
    Invariant("INV-03", "turn.asked", "warn", "问题 ≥8 字且无内心独白", _inv03),
    Invariant("INV-04", "eval.returned", "fatal", "evidence ⊆ answer", _inv04),
    Invariant("INV-05", "eval.returned", "warn", "verdict 与分数一致", _inv05),
    Invariant("INV-06", "turn.asked", "warn", "计划内或记 deviation", _inv06),
    Invariant("INV-07", "state.transition", "fatal", "流转原因白名单", _inv07),
    Invariant("INV-08", "budget", "fatal", "超预算须已降级", _inv08),
]


def check_all(ctx: dict, only: str = "") -> list[Violation]:
    """跑一遍全部规则，返回违规列表（空列表=干净）。"""
    out: list[Violation] = []
    for inv in INVARIANTS:
        if only and inv.when != only:
            continue
        try:
            v = inv.check(ctx)
        except Exception as e:                 # 规则自身出错不能挂掉面试
            v = Violation(inv.id, "warn", "规则执行异常", detail=f"{type(e).__name__}: {e}")
        if v:
            out.append(v)
    return out


def render(violations: list[Violation]) -> str:
    if not violations:
        return "不变量：0 违规"
    fatal = sum(1 for v in violations if v.severity == "fatal")
    lines = [f"不变量：fatal {fatal} · warn {len(violations) - fatal}"]
    for v in violations[:8]:
        lines.append(f"  [{v.severity}] {v.id} {v.desc}（{v.detail}）→ {v.fix}")
    return "\n".join(lines)

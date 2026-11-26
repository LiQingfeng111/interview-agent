"""候选人能力画像：把整场面试的评分聚合成"人"的能力维度。

⚠ 这个模块和 eval/ 是**两个不同的层次**，不要混：
  - eval/run_eval.py  → 衡量"这个 Agent 系统好不好"（给开发者看）
  - profile.py        → 衡量"这个候选人强不强"（给用户看）

设计上的三个关键决定：

1. **能力维度 ≠ 单题评分维度**
   单题评分是 accuracy/depth/tradeoff/boundary/clarity（针对一句话）。
   能力画像是 思考深度 / 语言表达 / 工程能力 / 项目经历 / 技术广度（针对一个人）。
   前者是后者的原料，但需要重新映射，不能直接照搬。

2. **有些维度不能从"回答"算**
   "项目经历"根本不在任何一句回答里，它来自简历（项目数量、技术栈广度、
   是否有量化指标）以及面试中被深挖到的层数。如果只用回答打分，
   一个简历很硬但当天紧张的人会被误判成"没经历"。

3. **必须给置信度**
   8 轮面试算出的"工程能力 78 分"和 20 轮算出的不是一回事。
   所以每个画像都带 confidence，轮次太少时明确标"低置信"。
   这一条是为了防止把数字当成结论。
"""
from __future__ import annotations

from config import LADDER, RUBRIC_DIMS

# 能力维度定义：(key, 中文名, 权重)
# 权重和为 1，用于算总分；单项也能单独看。
ABILITY_DIMS = [
    ("thinking_depth", "思考深度", 0.30),
    ("engineering", "工程能力", 0.25),
    ("project_exp", "项目经历", 0.20),
    ("communication", "语言表达", 0.15),
    ("tech_breadth", "技术广度", 0.10),
]

LEVELS = [
    (80, "优秀"),
    (65, "良好"),
    (50, "合格"),
    (0, "待加强"),
]


def _clamp(v: float) -> float:
    return max(0.0, min(100.0, float(v)))


def _scale(score_1_5: float) -> float:
    """1-5 分制 → 0-100。"""
    return _clamp((float(score_1_5) - 1) / 4 * 100)


def build_profile(
    turns: list,
    stats: dict,
    projects: list,
    deepest_index: int,
    mode: str = "mock",
) -> dict:
    """把逐轮评分 + 简历信息聚合成候选人能力画像。"""
    evals = [t.evaluation for t in turns if t.evaluation]
    if not evals:
        return {"available": False, "reason": "无有效评分记录"}

    dims = stats.get("dims") or {}

    # ---- 思考深度：木梯打到多深（60%）+ 单答 depth 均分（40%）
    deepest_ratio = deepest_index / max(1, len(LADDER) - 1)
    thinking = _clamp(deepest_ratio * 100 * 0.6 + _scale(dims.get("depth", 1)) * 0.4)

    # ---- 工程能力：准确性 + 权衡意识 + 边界意识
    engineering = _clamp(
        _scale(dims.get("accuracy", 1)) * 0.4
        + _scale(dims.get("tradeoff", 1)) * 0.3
        + _scale(dims.get("boundary", 1)) * 0.3
    )

    # ---- 语言表达：清晰度为主，过长扣分（冗长也是表达问题）
    clarity = _scale(dims.get("clarity", 1))
    long_answers = sum(1 for t in turns if t.evaluation and len(t.answer) > 800)
    penalty = min(20.0, long_answers * 5.0)
    communication = _clamp(clarity - penalty)

    # ---- 项目经历：来自简历 + 被深挖到的层数（不是从回答打分）
    n_projects = len(projects or [])
    stack_breadth = sum(len(p.stack) for p in (projects or []))
    has_metrics = sum(1 for p in (projects or []) if p.metrics)
    probed_layers = sum(
        1 for t in turns
        if t.phase == "PROJECT_PROBE" and t.ladder_level >= 2
    )
    project_exp = _clamp(
        min(n_projects, 3) / 3 * 40
        + min(stack_breadth, 12) / 12 * 20
        + min(has_metrics, 3) / 3 * 20
        + min(probed_layers, 4) / 4 * 20
    )

    # ---- 技术广度：技术问答阶段的表现 + 考点覆盖
    tech_turns = [t for t in turns if t.phase == "TECH_QA" and t.evaluation]
    if tech_turns:
        tech_avg = sum(t.evaluation.avg for t in tech_turns) / len(tech_turns)
        coverage = len({t.question[:12] for t in tech_turns})
        tech_breadth = _clamp(_scale(tech_avg) * 0.8 + min(coverage, 3) / 3 * 20)
    else:
        tech_breadth = 0.0

    values = {
        "thinking_depth": round(thinking, 1),
        "engineering": round(engineering, 1),
        "project_exp": round(project_exp, 1),
        "communication": round(communication, 1),
        "tech_breadth": round(tech_breadth, 1),
    }

    overall = sum(values[k] * w for k, _, w in ABILITY_DIMS)
    level = next(label for threshold, label in LEVELS if overall >= threshold)

    # ---- 置信度：轮次太少 / mock 模式 都要打折
    n = len(evals)
    conf = 1.0
    if n < 8:
        conf *= 0.4
    elif n < 14:
        conf *= 0.7
    if mode == "mock":
        conf *= 0.5
    confidence = round(conf, 2)

    weakest = min(values, key=lambda k: values[k])
    strongest = max(values, key=lambda k: values[k])
    name_of = {k: cn for k, cn, _ in ABILITY_DIMS}

    # KSA 三轨（Knowledge / Skill / Attitude）
    # 来源：PolyInterview(arXiv 2607.10310) 用 KSA + STAR 组织评估。
    # 它的价值在于：招聘方习惯按 KSA 看人，而求职者更习惯看五项细分。
    # 两套视图共用同一批分数，只是聚合口径不同。
    ksa = {
        "K 知识": round(values["tech_breadth"], 1),
        "S 技能": round((values["engineering"] + values["communication"]) / 2, 1),
        "A 态度": round(values["thinking_depth"], 1),
    }

    return {
        "available": True,
        "mode": mode,          # real / mock：趋势统计只认 real，别让 mock 分数污染
        "values": values,
        "ksa": ksa,
        "ksa_note": "A（态度）当前用「深挖能打到反事实层」代理，代表复盘与 ownership 意愿",
        "overall": round(overall, 1),
        "level": level,
        "weakest": name_of[weakest],
        "strongest": name_of[strongest],
        "confidence": confidence,
        "confidence_note": _confidence_note(confidence, n, mode),
        "evidence_turns": n,
        "disclaimer": (
            "本画像仅反映【本场表现】，不等于真实能力评估；"
            "单场面试样本量小，不宜用于任何录用决策。"
        ),
    }


def _confidence_note(conf: float, n: int, mode: str) -> str:
    parts = [f"{n} 轮有效评分"]
    if mode == "mock":
        parts.append("mock 评分（仅验证流程）")
    if conf < 0.4:
        parts.append("样本不足，结论仅供参考")
    elif conf < 0.7:
        parts.append("样本偏少，置信度中等")
    return "，".join(parts)


def render_profile(profile: dict, width: int = 20) -> str:
    """终端字块图。真要做雷达图，交给 Web 面板（见 README 扩展）。

    mock 模式**不打印分数**：那些分数是规则模板算出来的，
    打印成"综合 71.7 分 · 良好"会让用户误以为是真实评估——
    这是本项目最容易误导人的一处，宁可少显示也不要假象。
    """
    if not profile.get("available"):
        return f"（能力画像不可用：{profile.get('reason', '')}）"

    if profile.get("mode") == "mock":
        return (
            "  mock 模式：不计算能力分数。\n"
            "  原因：mock 评分由关键词规则生成，打出来的分会让人误以为是真实评估。\n"
            "  想看真实画像，请用 --llm auto（会连 DSH 免费中继）或配置远程 API。"
        )

    lines = [
        f"综合 {profile['overall']} 分 · {profile['level']}"
        f" · 最强 {profile['strongest']} · 最弱 {profile['weakest']}",
        f"置信度 {profile['confidence']}（{profile['confidence_note']}）",
    ]
    for key, cn, _ in ABILITY_DIMS:
        v = profile["values"][key]
        filled = int(round(v / 100 * width))
        bar = "█" * filled + "░" * (width - filled)
        lines.append(f"  {cn:<6}{bar} {v:>5}")

    ksa = profile.get("ksa") or {}
    if ksa:
        ksa_line = "  ".join(f"{k} {v}" for k, v in ksa.items())
        lines.append(f"  KSA 三轨：{ksa_line}")
    return "\n".join(lines)

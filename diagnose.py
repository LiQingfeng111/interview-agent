"""短板分类：把"答得不好"翻译成可执行的短板类型。

来源：对标 llx9826/llm-interview-coach 的 references/diagnostic-output.md，
它给出 7 类固定短板，最关键的设计是——**把"不会"和"没意识到"分开**：

  - 基础概念    → 真不懂，需要补知识
  - 指标意识    → 知道有指标这回事，但没主动拿指标说话
  - 项目 ownership → 讲不清哪些是自己做的
  - tradeoff    → 只会说结论，说不出取舍
  - 系统视角    → 只看单个模块，不看上下游与失效
  - 业务视角    → 只讲技术，不讲为什么做
  - 失败复盘    → 没有失败样本，或失败原因说不清

为什么要分开？因为两者的训练动作完全不同：
  "不懂 baseline"        → 去学 concept，一周能补
  "没意识到要讲 baseline" → 只需要改表达结构，今天就见效

判定不能只靠 LLM 自由发挥，否则每次分类都不一样。做法：
  要素三态（已答到/回避/未答出）× rubric 弱维 → 规则映射到固定分类，
  LLM 只在给定分类里挑，不再自己发明标签。
"""
from __future__ import annotations

WEAKNESS_CATEGORIES = {
    "基础概念": "概念本身说不清，需要补知识",
    "指标意识": "没有主动用指标/对照说话，需要改表达结构",
    "项目 ownership": "讲不清哪些是自己做的，容易被追问穿",
    "tradeoff": "只会给结论，说不出代价与替代方案",
    "系统视角": "只看单点，不讲上下游与失效边界",
    "业务视角": "只讲技术实现，不讲为什么做、给谁做",
    "失败复盘": "没有失败样本，或失败原因说不清",
}

# 要素缺口 → 短板分类
ELEMENT_TO_WEAKNESS = {
    "目标": "业务视角",
    "baseline": "指标意识",
    "指标": "指标意识",
    "失败方案": "失败复盘",
    "个人贡献": "项目 ownership",
}

# rubric 弱维 → 短板分类
DIM_TO_WEAKNESS = {
    "accuracy": "基础概念",
    "depth": "系统视角",
    "tradeoff": "tradeoff",
    "boundary": "系统视角",
    "clarity": "项目 ownership",
}


def diagnose(element_report: dict, dims: dict, weakest_dim: str = "") -> list[dict]:
    """输出短板清单：[{category, reason, evidence_level, action}]。

    element_report: {项目: {要素: "已答到"/"回避（问2次）"/"未答出（问2次）"/"未问到"}}
    dims: {"accuracy": 3.6, ...} 全场维度均分
    """
    found: dict[str, dict] = {}

    def add(category: str, reason: str, level: str, action: str) -> None:
        if category not in found:
            found[category] = {
                "category": category,
                "reason": reason,
                "level": level,          # 不会 / 没意识
                "action": action,
            }

    # 1) 要素缺口优先（最具体的证据）
    for proj, statuses in (element_report or {}).items():
        for element, status in (statuses or {}).items():
            if status == "已答到" or status.startswith("已答到"):
                continue
            category = ELEMENT_TO_WEAKNESS.get(element)
            if not category:
                continue
            # "回避" = 讲了但绕开 → 没意识；"未答出" = 讲不出 → 可能是不会
            if "回避" in status:
                level = "没意识"
            elif "未答出" in status:
                level = "不会"
            else:
                level = "未问到"       # 没问过，不算短板，只是覆盖缺口
            if level == "未问到":
                continue
            add(
                category,
                f"{proj}·{element}：{status}",
                level,
                _action_for(category, element, level),
            )

    # 2) rubric 弱维补充（分数低但要素都答到了的情况）
    if dims:
        for dim, val in dims.items():
            if val >= 3.0:
                continue
            category = DIM_TO_WEAKNESS.get(dim)
            if not category:
                continue
            add(
                category,
                f"{dim} 均分偏低（{val}）",
                "没意识" if val >= 2.0 else "不会",
                _action_for(category, "", ""),
            )

    order = list(WEAKNESS_CATEGORIES)
    return sorted(found.values(), key=lambda x: order.index(x["category"]))


def _action_for(category: str, element: str, level: str) -> str:
    """训练动作必须区分"补知识"和"改结构"，这是分类的意义所在。"""
    if category == "指标意识":
        if level == "不会":
            return "先搞懂指标怎么定义，再为项目补一句『对比什么、提升多少』"
        return "每个项目准备一句『baseline 是什么、为什么这么比』，答任何问题都带上"
    if category == "业务视角":
        return "练一句开场：这个项目解决谁的什么问题、不做会怎样"
    if category == "失败复盘":
        return "为每个项目写一个失败方案，格式：想做什么→卡在哪→后来怎么改"
    if category == "项目 ownership":
        return "把『我负责什么』改成『我做了什么决定、带来什么结果』"
    if category == "tradeoff":
        return "每个选型补一句『我否掉了 X，因为 Y』"
    if category == "系统视角":
        return "补一个失效场景 + 你怎么发现的（监控指标/告警）"
    if category == "基础概念":
        return f"先把「{element or '相关概念'}」的定义和边界讲清楚，再谈项目"
    return "找一个真实案例把它讲具体"


def render_diagnosis(items: list[dict]) -> str:
    if not items:
        return "  （本场没有明显短板，或数据不足以判定）"
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"  {i}. {it['category']}（{it['level']}）")
        lines.append(f"     证据：{it['reason']}")
        lines.append(f"     动作：{it['action']}")
    return "\n".join(lines)

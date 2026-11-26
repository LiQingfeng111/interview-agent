"""面试前问题计划：LLM 读简历生成计划，代码校验覆盖。

回答一个关键疑问：「为什么不能直接让 LLM 读简历提问？」

可以，而且现在就是这么做的。区别在于**调用形态**：

  形态 A（每轮现编）：每次只给当前上下文 → 问题贴合当前回答，但容易漏项目、
                     容易在一个点上反复打转，长面试尤其明显。
  形态 B（先出计划）：LLM 一次读完整份简历，产出结构化清单 → 覆盖面可控、
                     可以提前看到"要问哪些点"，面试中只需按计划推进。
                     风险是 LLM 会漏项目、编造细节、给空问题。

所以真正的分工是：
  - **LLM 负责"想出有内容的问题"**（代码写不出针对 RAG chunk 的那种问题）
  - **代码负责"保证覆盖与配额"**（LLM 一次生成清单，常漏第二个项目）

这就是 EZInterviewer(arXiv 2301.00972) 说的「知识选择器 / 对话生成器」解耦：
计划 = 知识选择器的输出，措辞 = 对话生成器的事。
"""
from __future__ import annotations

from config import PROBE_CHECKLIST

PLAN_SCHEMA_HINT = """你是面试计划生成器。读简历后，为每个项目生成深挖计划。
只输出 JSON：{"projects":[{"name":"项目名","focus":[{"element":"目标|baseline|指标|失败方案|个人贡献","seed":"一句具体的提问"}]}]}
要求：
1. 简历里每个项目都必须出现在 projects 里，一个都不能漏。
2. 每个项目至少覆盖 3 个 element，seed 必须锚定该项目自己的技术细节。
3. seed 是给你自己看的草稿，不是最终措辞，但必须能看出问的是什么。
"""

# mock 计划用的种子问题（真实模型会自己生成，这里保证离线可跑）
_SEEDS = {
    "目标": "「{p}」最初要解决什么问题？为什么当时必须做这个？",
    "baseline": "「{p}」的效果，你对比的基线是什么？这个对比公平吗？",
    "指标": "「{p}」里提到的指标怎么定义、怎么测出来的？",
    "失败方案": "做「{p}」时试过哪些没成的方案？失败原因是什么？",
    "个人贡献": "「{p}」里哪部分是你独立做的？团队成果和你个人成果的边界在哪？",
}


# 同一要素的第二次追问：必须换一个真正不同的问法，不能只是加前缀。
# 用户策略是「追问一次就判定」，所以这一次的质量很关键——
# 问得和第一次一样，等于没追。
RETRY_SEEDS = {
    "目标": "如果当时不做「{p}」，后果会是什么？为什么非做不可？",
    "baseline": "如果不跟任何基线比，你怎么证明「{p}」真的有用？",
    "指标": "「{p}」那个数字具体怎么算出来的？换个算法会不会不一样？",
    "失败方案": "做「{p}」时有没有一条路走到一半发现不通？具体卡在哪一步？",
    "个人贡献": "「{p}」里哪一部分离了你就不成立？",
}


def mock_plan(projects: list) -> dict:
    """确定性计划：每个项目覆盖全部五要素。用于离线跑通与测试。"""
    out = {"projects": []}
    for p in projects:
        focus = []
        for element, _desc in PROBE_CHECKLIST:
            seed = _SEEDS.get(element, "关于「{p}」，讲讲{X}这部分。")
            focus.append({
                "element": element,
                "seed": seed.format(p=p.name, X=element),
            })
        out["projects"].append({"name": p.name, "focus": focus})
    return out


def build_plan(llm, projects: list, jd_keywords: list[str] | None = None) -> dict:
    """让 LLM 读简历生成计划；失败或 mock 时退回确定性计划。"""
    if not projects:
        return {"projects": []}
    if getattr(llm, "is_mock", False):
        return mock_plan(projects)

    resume_text = "\n".join(
        f"- {p.name}｜角色:{p.role or '未写'}｜技术栈:{'/'.join(p.stack)}｜"
        f"亮点:{'；'.join(p.highlights)}｜指标:{'；'.join(p.metrics) or '无'}"
        for p in projects
    )
    user = f"简历项目清单：\n{resume_text}\n"
    if jd_keywords:
        user += f"\n目标岗位关注：{'、'.join(jd_keywords[:10])}\n"
    user += "\n请按 schema 输出计划。"
    try:
        plan = llm.chat_json(
            [{"role": "system", "content": PLAN_SCHEMA_HINT},
             {"role": "user", "content": user}],
            task="planner",
        )
        return plan if plan.get("projects") else mock_plan(projects)
    except Exception:
        return mock_plan(projects)


def validate_plan(plan: dict, projects: list, min_elements: int = 3) -> tuple[bool, list[str]]:
    """代码负责的那一半：校验覆盖，不负责想问题。

    典型失败模式（真实 LLM 高概率出现）：
      1. 只写了一个项目，漏掉第二个
      2. 某个项目只覆盖 1 个要素，等于没计划
      3. focus 里 seed 为空字符串——占位但不可用
    """
    issues: list[str] = []
    want = {p.name for p in projects}
    got_projects = plan.get("projects") or []
    got = {str(x.get("name", "")).strip() for x in got_projects}

    missing = want - got
    if missing:
        issues.append(f"计划漏掉项目：{'、'.join(sorted(missing))}")

    for proj in got_projects:
        name = str(proj.get("name", "")).strip()
        focus = proj.get("focus") or []
        elements = {str(f.get("element", "")).strip() for f in focus}
        if len(elements) < min_elements:
            issues.append(f"「{name}」只覆盖 {len(elements)} 个要素（要求 ≥{min_elements}）")
        for f in focus:
            if not str(f.get("seed", "")).strip():
                issues.append(f"「{name}」有空的 seed")

    return (not issues), issues


def plan_summary(plan: dict) -> str:
    lines = []
    for proj in plan.get("projects") or []:
        name = proj.get("name", "")
        elements = [str(f.get("element", "")) for f in proj.get("focus") or []]
        lines.append(f"  {name}: {len(elements)} 个要素（{'/'.join(elements)}）")
    return "\n".join(lines) or "  （空计划）"

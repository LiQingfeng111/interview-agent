"""提示词模板化：把"拼字符串"换成 ChatPromptTemplate + LCEL。

## 为什么要做这一步

原来 `Interviewer._build_prompt()` 是 69 行 if/elif 加 `parts.append(...)`，
问题不在长度，而在**可变部分藏在代码里**：

- 想改一句固定话术，得读 69 行代码才知道它会插在哪个位置；
- 想知道"这次请求到底带了哪些上下文"，只能打断点；
- 想做 A/B（比如去掉 checklist 看看效果），得改代码分支。

模板化之后，每个变量都是一个**有名字的槽位**（`{ladder}` `{checklist}` `{seed}`…），
骨架一眼可见，A/B 就是"给某个槽传空串"。

## 三条硬约束

1. **渲染出的文本必须与旧实现逐字一致**。
   证据：`tests/golden/prompt_build.json`（重构前拍的 9 条快照）+ `tests/test_prompt_templates.py`。
   提示词差一个字，模型行为就会漂，而这种漂移单测看不出来。
2. **不接管重试与记账**。链的末端仍是 `LlmPort.chat()`，
   `LLMLogger`、usage、重试次数全都不变——LangChain 只负责"组 prompt"和"串流程"。
3. **可回退**。`IA_PROMPT_ENGINE=template|legacy`，出问题切回旧实现。

## 空槽位怎么处理

模板里槽位是**相邻**的，每个槽位值自带结尾换行（没有内容就是空串）。
这样 `{followup}{retry}{checklist}` 在渲染时自然"要么出现一整段，要么什么都不留"，
不会像 `"{a}\n{b}"` 那样在缺值时留下空行——旧实现用 `"\n".join(parts)` 也没有空行，
两边行为因此一致。
"""

from __future__ import annotations

import os

from config import INTERVIEWER_LADDER_HINT, PROBE_CHECKLIST_HINT

# ---------------------------------------------------------------- 模板骨架

#: 项目深挖阶段。固定文字留在模板里，变量全部显式命名。
PROBE_TEMPLATE = """当前阶段：{phase}（{phase_desc}）
{ladder}当前要深挖的项目：{project}
{followup}{retry}{checklist}{seed}{recent}{asked}请提出下一个问题。"""

#: 技术问答阶段。
TECH_TEMPLATE = """当前阶段：{phase}（{phase_desc}）
要考察的知识点：{topic}
{bank}{points}{seed}{jd_keywords}{jd_hint}{ask}{web}"""

#: 简单阶段（开场 / 自我介绍 / 反问）：一句固定指引即可。
SIMPLE_TEMPLATE = """当前阶段：{phase}（{phase_desc}）
{guide}"""

PHASE_TEMPLATES = {
    "PROJECT_PROBE": PROBE_TEMPLATE,
    "TECH_QA": TECH_TEMPLATE,
}


def _nl(text: str | None) -> str:
    """把"一段内容"变成"以换行结尾的一段"，空内容变成空串。

    这是整个模板化能保持逐字一致的关键。旧实现是 `"\\n".join(parts)`，
    语义等价于"每段后面跟一个换行、最后一段除外"。所以：

    - 中间位置的槽位一律用本函数（**不做 rstrip**：像 ladder 提示词本身
      就以换行结尾，剥掉会少一个空行，实测差 1 个字符）；
    - 模板里最后一个槽位不许带换行（否则文本末尾多一个 \\n）。
    """
    if not text:
        return ""
    return text + "\n"


# ---------------------------------------------------------------- 槽位组装

def probe_slots(sm, hint: dict, *, jd_keywords=None, web_refs: str = "") -> dict:
    """PROJECT_PROBE 阶段的槽位。逻辑与旧实现一一对应，只是把结果命名。"""
    from interviewer import LEVEL_GOALS, _recent  # 延迟导入，避免循环依赖

    ladder_name = hint.get("ladder_name", "是什么")
    goal = LEVEL_GOALS.get(ladder_name, "")
    ladder = INTERVIEWER_LADDER_HINT.format(
        level=hint.get("ladder_level", 0),
        level_name=ladder_name,
        level_goal=goal,
        verdict=hint.get("verdict", "无（首问）"),
        weakest=hint.get("weakest", "无"),
    )

    followup = hint.get("followup_hint") or ""
    followup = f"评分官建议的追问方向：{followup}" if followup else ""

    retry = ""
    if int(hint.get("attempt", 0)) > 0:
        retry = (
            "注意：这是同一层级的再次追问。必须换一个明显不同的角度，"
            "不要重复上一问的措辞，也不要只把问题换个主语。"
        )

    seed = ""
    if hint.get("seed_question"):
        seed = (
            "面试前计划中为这一点准备的提问草稿（可改写措辞，不要照抄）：\n"
            + hint["seed_question"]
        )

    recent = ""
    conv = _recent(sm.history, 4)
    if conv:
        recent = "最近对话：\n" + conv

    asked = ""
    asked_items = [h["q"][:50] for h in sm.history[-6:] if h.get("q")]
    if asked_items:
        asked = "已经问过、不要重复的问题：\n" + "\n".join(f"- {q}" for q in asked_items)

    return {
        "ladder": _nl(ladder),
        "project": hint.get("project") or "候选人提到的项目",
        "followup": _nl(followup),
        "retry": _nl(retry),
        "checklist": _nl(PROBE_CHECKLIST_HINT),
        "seed": _nl(seed),
        "recent": _nl(recent),
        "asked": _nl(asked),
    }


def tech_slots(sm, hint: dict, *, jd_keywords=None, web_refs: str = "") -> dict:
    """TECH_QA 阶段的槽位。"""
    from knowledge import topic_to_question_seed  # 延迟导入，避免循环依赖

    bank = ""
    if hint.get("bank_question"):
        bank = (
            "题库里这道题（可改写措辞，但必须保持考点一致，不要换题）：\n"
            f"  {hint['bank_question']}"
        )

    points = ""
    if hint.get("bank_points"):
        points = "评分要点（你自己知道就行，不要念给候选人）：" + "；".join(hint["bank_points"])

    seed = ""
    seed_angle = topic_to_question_seed(hint.get("topic", ""))
    if seed_angle:
        seed = f"参考提问角度（不要照抄）：{seed_angle}"

    jd_keywords_line = ""
    jd_hint_line = ""
    if jd_keywords:
        jd_keywords_line = "目标岗位 JD 关注点：" + "、".join(jd_keywords)
        jd_hint_line = "尽量把问题引向 JD 关注的技术，但不要生硬堆词。"

    web = ""
    if hint.get("web_refs"):
        web = _nl(
            "【联网参考资料】\n" + str(hint["web_refs"])[:800]
            + "\n（只用来确认自己的技术判断是否过时，不要照抄，不要提“网上说”。"
              "如果资料与简历冲突，以简历为准。）"
        )

    return {
        "topic": hint.get("topic"),
        "bank": _nl(bank),
        "points": _nl(points),
        "seed": _nl(seed),
        "jd_keywords": _nl(jd_keywords_line),
        "jd_hint": _nl(jd_hint_line),
        "ask": _nl("请结合候选人前面的回答，提一个具体问题。"),
        "web": web,
    }


def simple_slots(sm, hint: dict) -> dict:
    """开场 / 自我介绍 / 反问：一句固定指引。"""
    phase = sm.phase
    if phase == "SELF_INTRO":
        guide = "请让候选人做自我介绍，并提示他会围绕项目深挖。"
    elif phase == "CANDIDATE_ASK":
        guide = "请把话语权交给候选人，让他提问。"
    else:
        guide = "请简短开场，说明本次面试流程（自我介绍→项目深挖→技术问答→反问）。"
    return {"guide": guide}


# ---------------------------------------------------------------- 渲染

def _render_text(template: str, values: dict) -> str:
    """用 ChatPromptTemplate 渲染成纯文本。

    为什么还要转回纯文本：下游 `LlmPort.chat()` 收的是 OpenAI 风格 dict 消息，
    而这个项目的对账样本（golden）也是纯文本。模板负责组装，端口负责发送。
    """
    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([("human", template)])
    messages = prompt.format_messages(**values)
    text = "\n".join(m.content for m in messages)
    # 旧实现是 join(parts)，最后一段之后不带换行；这里统一收掉尾部换行，
    # 否则"最后一个槽位为空"时（例如 TECH_QA 没有联网资料）会多出一个 \n。
    return text.rstrip("\n")


def build_question_text(sm, hint: dict, *, jd_keywords=None, web_refs: str = "") -> str:
    """对外入口：按阶段选模板、组装槽位、渲染成文本。"""
    phase = sm.phase
    if phase == "PROJECT_PROBE":
        values = probe_slots(sm, hint, jd_keywords=jd_keywords, web_refs=web_refs)
        template = PROBE_TEMPLATE
    elif phase == "TECH_QA":
        values = tech_slots(sm, hint, jd_keywords=jd_keywords, web_refs=web_refs)
        template = TECH_TEMPLATE
    else:
        values = simple_slots(sm, hint)
        template = SIMPLE_TEMPLATE

    base = {"phase": phase, "phase_desc": sm.phase_cfg["desc"]}
    return _render_text(template, {**base, **values})


def engine() -> str:
    """当前提示词引擎：template（默认）或 legacy（旧实现，用于回退）。"""
    return (os.getenv("IA_PROMPT_ENGINE") or "template").strip().lower()


# ---------------------------------------------------------------- LCEL 链

def make_question_chain(llm, *, task: str = "interviewer"):
    """把"模板 → 调模型 → 取文本"串成一条 LCEL 链。

    链的中间那步是 `RunnableLambda`，包的是我们自己的 `LlmPort.chat()`：
    这样重试、记账、mock 后端全都照旧，LangChain 只负责编排。
    用 `llm | StrOutputParser()` 会更"像 LangChain"，但那样会绕过 LLMLogger，
    账本里就少一条记录——审计能力比"看起来原生"更重要。
    """
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import RunnableLambda

    prompt = ChatPromptTemplate.from_messages([("system", "{system}"), ("human", "{user}")])

    def _call(prompt_value) -> str:
        # 管道把 ChatPromptValue 传进来（不是 dict），必须先摊成消息列表。
        # 这里刻意不用 `llm | StrOutputParser()`：那会绕过 LlmPort，
        # 记账、重试、mock 后端都会失效。
        messages = [
            {"role": "system" if m.type == "system" else "user", "content": m.content}
            for m in prompt_value.messages
        ]
        return llm.chat(messages, task=task)

    return prompt | RunnableLambda(_call) | StrOutputParser()

"""Agent 1：提问官。

职责边界（很重要，面试时会问"你怎么划 Agent 边界"）：
  - 只负责"下一句问什么"
  - 不负责打分（交给 Evaluator）
  - 不负责决定阶段推进（交给状态机）
输入是"状态机给出的结构化 hint"，输出是"一句问题"。
约束越窄，输出越稳。
"""
from __future__ import annotations

from config import (
    ELEMENT_NAMES,
    INTERVIEWER_LADDER_HINT,
    INTERVIEWER_PERSONA_HINT,
    INTERVIEWER_SYSTEM,
    LADDER,
    PERSONAS,
    PROBE_CHECKLIST_HINT,
)
from knowledge import topic_to_question_seed
from llm import MOCK_LADDER_Q, BaseLLM
from planner import RETRY_SEEDS
from prompt_templates import build_question_text as _build_question_text
from prompt_templates import engine as _prompt_engine
from state_machine import InterviewStateMachine

LEVEL_GOALS = {
    "是什么": "确认项目目标、规模、候选人真实承担的部分（防止把团队成果说成自己的）",
    "为什么": "确认技术选型有依据：能说出**被否掉的替代方案**与**baseline**，而不是只有结论",
    "怎么做": "确认细节落到实现层：数据结构、参数、失败样貌",
    "取舍": "确认理解成本与收益，能说出**试过的失败方案**及其失败原因，能量化更好",
    "边界": "确认知道方案何时失效，以及怎么发现失效（监控/告警）",
    "反事实": "确认有复盘能力，能提出改进设计",
}


class Interviewer:
    def __init__(
        self,
        llm: BaseLLM,
        persona: str = "balanced",
        jd_keywords: list[str] | None = None,
        plan: dict | None = None,
        system_prompt: str | None = None,
    ):
        self.llm = llm
        self.persona = persona if persona in PERSONAS else "balanced"
        self.jd_keywords = [k for k in (jd_keywords or []) if k][:10]
        self.plan = plan or {}
        # 允许替换提问官 prompt（A/B 用）；不传就用 config 默认
        self.system_prompt = system_prompt or INTERVIEWER_SYSTEM
        # 记录计划里已经用过的提问点，避免重复问同一个要素
        self._used_plan: set[tuple[str, str]] = set()
        # 真正问出口过的要素（含重追问），用于统计计划覆盖率
        self.asked_elements: set[tuple[str, str]] = set()
        # 最近一次出题瞄准的要素（供会话层记账）
        self.last_target = ""

    def _next_plan_seed(
        self, project: str, attempt: int, covered: set[str] | None = None,
        closed: set[str] | None = None,
    ) -> str:
        """从计划里取当前项目下一个**未被覆盖且未关闭**的提问点。

        跳过规则（用户定的策略）：
          - 已经答到的要素 → 跳过
          - 已问满两次仍未答到 → 记为缺口，跳过（不逼供）

        注意 attempt>0 时**不再直接返回空**：上一版这么做，导致
        "要素未答到要再追一次"和"木梯重追问换角度"互相打架，
        第二次追问永远发不出去。现在允许重问，但加"换个角度"前缀，
        避免逐字重复（后面还有去重护栏兜底）。
        """
        self.last_target = ""
        if not project:
            return ""
        covered = covered or set()
        closed = closed or set()
        for proj in self.plan.get("projects") or []:
            if str(proj.get("name", "")).strip() != project:
                continue
            for f in proj.get("focus") or []:
                element = str(f.get("element", "")).strip()
                if element in covered or element in closed:
                    continue
                seed = str(f.get("seed", "")).strip()
                if not seed:
                    continue
                key = (project, element)
                if attempt > 0:
                    # 同一要素的第二次追问：换一个真正不同的问法（不是加前缀）
                    self.last_target = element
                    self.asked_elements.add(key)
                    retry = RETRY_SEEDS.get(element, "")
                    return retry.format(p=project) if retry else "换个角度再问一次：" + seed
                if key in self._used_plan:
                    continue
                self._used_plan.add(key)
                self.asked_elements.add(key)
                self.last_target = element
                return seed
        return ""

    def next_question(
        self,
        sm: InterviewStateMachine,
        last_eval: dict | None = None,
        stack: str = "",
    ) -> str:
        # 矛盾优先：账本发现候选人前后数字不一致时，先问清楚。
        # 这一步**不走模型**——"记住旧值"是系统的确定性能力，
        # 交给模型只会变成"有时候想得起来、有时候想不起来"。
        c = getattr(self, "contradiction", None)
        if c is not None:
            self.contradiction = None
            self.last_target = ""
            proj = c.project or sm.probe.current
            return f"你说 {proj} 这块，{c.question()}"
        hint = self._build_hint(sm, last_eval, stack)
        persona = PERSONAS[self.persona]
        system = self.system_prompt + INTERVIEWER_PERSONA_HINT.format(
            name=persona["name"], style=persona["style"]
        )
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": self._build_prompt(sm, hint)},
        ]
        try:
            q = self.llm.chat(msgs, task="interviewer", hint=hint).strip()
        except Exception:
            q = self._fallback(hint)
        q = _clean_question(q) or self._fallback(hint)
        # 去重护栏 v2：只跟"上一问"比是不够的。
        # 实测本地小模型会把同一个问题隔两三问再问一次（"你如何确保检索准确"连问 5 次），
        # 因此要比对最近 N 问，命中即强制换成本层级的兜底问法。
        if q and self._is_repeat(q, sm):
            q = self._fallback({**hint, "attempt": max(1, int(hint.get("attempt", 0)))})
        return q

    @staticmethod
    def _is_repeat(q: str, sm: InterviewStateMachine, lookback: int = 6) -> bool:
        """相似度去重，不是字符串完全相等。

        实测：本地小模型会把同一个问题换个尾缀再问一次
        （"如何在60天内实现…且用户满意" / "…且用户满意度提升"），
        严格相等比对抓不到，必须按字符二元组相似度判定。
        """
        target = _norm_question(q)
        if not target:
            return False
        for item in sm.history[-lookback:]:
            # 阈值 0.62：实测小模型会把同一问改两三个词再问一遍
            # （"遇到了哪些技术挑战" vs "遇到过哪些技术挑战"），
            # 0.8 抓不到，0.5 又会误杀同层级的不同问法。
            if _similarity(target, _norm_question(item.get("q", ""))) >= 0.62:
                return True
        return False

    # ------------------------------------------------------------ 内部
    def _build_hint(self, sm: InterviewStateMachine, last_eval: dict | None, stack: str) -> dict:
        hint: dict = {"phase": sm.phase, "persona": self.persona}
        if getattr(self, "web_refs", ""):
            hint["web_refs"] = self.web_refs
        if sm.phase == "PROJECT_PROBE":
            hint.update(
                {
                    "project": sm.probe.current,
                    "stack": stack,
                    "ladder_level": sm.probe.ladder_level,
                    "ladder_name": sm.ladder_name,
                    # attempt: 当前层级已重追几次，用于强制换问法
                    "attempt": sm.probe.attempts,
                }
            )
            seed = self._next_plan_seed(
                sm.probe.current,
                sm.probe.attempts,
                sm.probe.elements_of(sm.probe.current),
                {e for e in ELEMENT_NAMES if sm.probe.element_closed(sm.probe.current, e)},
            )
            if seed:
                hint["seed_question"] = seed
                hint["target_element"] = self.last_target
        if sm.phase == "TECH_QA":
            # 题库驱动：优先用真题库的题（带期望要点），没有才退回话题自由提问
            q = sm.tech.next_question()
            hint["topic"] = q.get("topic", "")
            hint["bank_question"] = q.get("question", "")
            hint["bank_points"] = q.get("points") or []
            hint["bank_level"] = q.get("level", "")
            hint["bank_id"] = q.get("id", "")
        if last_eval:
            hint["verdict"] = last_eval.get("verdict", "")
            hint["weakest"] = last_eval.get("weakest", "")
            hint["followup_hint"] = last_eval.get("followup_hint", "")
        return hint

    def _build_prompt(self, sm: InterviewStateMachine, hint: dict) -> str:
        """组 user 消息文本。

        两条实现路径，由 IA_PROMPT_ENGINE 决定：
        - template（默认）：走 prompt_templates 里的 ChatPromptTemplate，
          每个可变部分都是有名字的槽位（{ladder} {checklist} {seed} …）；
        - legacy：保留旧的分支拼接实现，用于回退与逐字比对。

        两条路径渲染结果必须逐字一致——证据在 tests/golden/prompt_build.json。
        """
        if _prompt_engine() == "legacy":
            return self._build_prompt_legacy(sm, hint)
        return _build_question_text(
            sm, hint, jd_keywords=getattr(self, "jd_keywords", None) or [],
            web_refs=getattr(self, "web_refs", "") or "",
        )

    def _build_prompt_legacy(self, sm: InterviewStateMachine, hint: dict) -> str:
        parts = [f"当前阶段：{sm.phase}（{sm.phase_cfg['desc']}）"]
        if sm.phase == "PROJECT_PROBE":
            goal = LEVEL_GOALS.get(hint.get("ladder_name", "是什么"), "")
            parts.append(
                INTERVIEWER_LADDER_HINT.format(
                    level=hint.get("ladder_level", 0),
                    level_name=hint.get("ladder_name", "是什么"),
                    level_goal=goal,
                    verdict=hint.get("verdict", "无（首问）"),
                    weakest=hint.get("weakest", "无"),
                )
            )
            parts.append(f"当前要深挖的项目：{hint.get('project') or '候选人提到的项目'}")
            if hint.get("followup_hint"):
                parts.append(f"评分官建议的追问方向：{hint['followup_hint']}")
            if int(hint.get("attempt", 0)) > 0:
                parts.append(
                    "注意：这是同一层级的再次追问。必须换一个明显不同的角度，"
                    "不要重复上一问的措辞，也不要只把问题换个主语。"
                )
            # 五要素覆盖提醒（来自对标项目：真实大厂必问 baseline 与失败方案）
            parts.append(PROBE_CHECKLIST_HINT)
            if hint.get("seed_question"):
                parts.append(
                    "面试前计划中为这一点准备的提问草稿（可改写措辞，不要照抄）：\n"
                    + hint["seed_question"]
                )

            conv = _recent(sm.history, 4)
            if conv:
                parts.append("最近对话：\n" + conv)
            # 显式列出已问过的问题：小模型光靠"别重复"这句指令不管用，
            # 把候选问题列出来，重复率会明显下降（比事后兜底更自然）。
            asked = [h["q"][:50] for h in sm.history[-6:] if h.get("q")]
            if asked:
                parts.append("已经问过、不要重复的问题：\n" + "\n".join(f"- {q}" for q in asked))
            parts.append("请提出下一个问题。")
        elif sm.phase == "TECH_QA":
            seed = topic_to_question_seed(hint.get("topic", ""))
            parts.append(f"要考察的知识点：{hint.get('topic')}")
            if hint.get("bank_question"):
                parts.append(
                    f"题库里这道题（可改写措辞，但必须保持考点一致，不要换题）：\n"
                    f"  {hint['bank_question']}"
                )
                if hint.get("bank_points"):
                    parts.append("评分要点（你自己知道就行，不要念给候选人）："
                                 + "；".join(hint["bank_points"]))
            if seed:
                parts.append(f"参考提问角度（不要照抄）：{seed}")
            if self.jd_keywords:
                parts.append("目标岗位 JD 关注点：" + "、".join(self.jd_keywords))
                parts.append("尽量把问题引向 JD 关注的技术，但不要生硬堆词。")
            parts.append("请结合候选人前面的回答，提一个具体问题。")
            if hint.get("web_refs"):
                parts.append(
                    "【联网参考资料】\n" + str(hint["web_refs"])[:800]
                    + "\n（只用来确认自己的技术判断是否过时，不要照抄，不要提“网上说”。"
                      "如果资料与简历冲突，以简历为准。）"
                )
        elif sm.phase == "SELF_INTRO":
            parts.append("请让候选人做自我介绍，并提示他会围绕项目深挖。")
        elif sm.phase == "CANDIDATE_ASK":
            parts.append("请把话语权交给候选人，让他提问。")
        else:
            parts.append("请简短开场，说明本次面试流程（自我介绍→项目深挖→技术问答→反问）。")
        return "\n".join(parts)

    def _fallback(self, hint: dict) -> str:
        """LLM 不可用时的确定性兜底，保证流程不断。

        注意：兜底也必须带层级信息。第一版重追问的兜底是一句固定话，
        结果在不同层级上反复出现同一句（实测 L1 和 L4 撞成同一问），
        兜底反而成了重复来源。
        """
        phase = hint.get("phase")
        if phase == "PROJECT_PROBE":
            lvl = min(int(hint.get("ladder_level", 0)), len(LADDER) - 1)
            proj = hint.get("project") or "这个项目"
            level_name = LADDER[lvl]
            if int(hint.get("attempt", 0)) > 0:
                retry = RETRY_SEEDS.get(hint.get("target_element", ""), "")
                if retry:
                    return retry.format(p=proj)
                # 同一层级重追问时，按 attempt 轮换问法，否则同层级会撞成同一句
                variants = MOCK_LADDER_Q.get(lvl) or []
                if variants:
                    idx = min(int(hint.get("attempt", 1)), len(variants) - 1)
                    return variants[idx].format(p=proj, s=hint.get("stack") or "这套方案")
                return (
                    f"还是「{proj}」，从「{level_name}」这一层换个角度："
                    f"如果只给你一句话解释，你会怎么讲？"
                )
            return f"关于「{proj}」，从「{level_name}」的角度讲讲：你是怎么做的？"
        if phase == "TECH_QA":
            bank_q = hint.get("bank_question")
            if bank_q:
                return bank_q
            return f"围绕{hint.get('topic', '这个技术点')}，你实际踩过什么坑？"
        if phase == "SELF_INTRO":
            return "请用两分钟做自我介绍，重点讲你最想被深挖的项目。"
        if phase == "CANDIDATE_ASK":
            return "你有什么想问我的？"
        return "我们开始。请先做个自我介绍。"


def _clean_question(q: str) -> str:
    """去掉小模型爱加的开场废话与编号，以及"内心独白"泄漏。

    实测两类脏输出：
      1. Qwen2.5-1.5B：「接下来的问题是：…」「请告诉我，…」
      2. deepseek-flash：「候选人没有回答扫描版的问题，而是直接讲了方案选型。
         我换个角度追问边界：…」——把分析过程写进了问题里，真人面试官不会这么说
    """
    q = q.strip().strip('"').strip("'").strip("「」")
    prefixes = (
        "面试官：", "面试官:", "问题：", "问题:", "Q:", "Q：", "1.", "1、",
        "接下来的问题是：", "接下来我想问：", "好的，", "好的，面试官：",
        "请告诉我，", "请问，", "那么，", "我们来谈谈，",
    )
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if q.startswith(prefix):
                q = q[len(prefix):].strip()
                changed = True

    # 内心独白泄漏：只保留最后一个"自述式"标记之后的问题本体
    leak_markers = ("候选人没有回答", "候选人未回答", "候选人没有", "我换个角度",
                    "我换一个角度", "注意到候选人", "候选人回答的是")
    cut = -1
    for m in leak_markers:
        idx = q.rfind(m)
        if idx > cut:
            cut = idx
    if cut > 0:
        tail = q[cut:]
        if "：" in tail:
            tail = tail.split("：", 1)[1]
        elif ":" in tail:
            tail = tail.split(":", 1)[1]
        q = tail.strip()

    q = q.split("\n")[0].strip()
    return q if len(q) >= 4 else ""


def _norm_question(q: str) -> str:
    """归一化后比对，避免标点/空格差异让"同一问"漏过去。"""
    import re as _re

    return _re.sub(r"[\s，。？！、,.?!「」“”\"'：:]", "", (q or ""))[:80]


def _similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard 相似度。中文短句上没有分词也能用。"""
    if not a or not b:
        return 0.0
    ga = {a[i:i + 2] for i in range(len(a) - 1)}
    gb = {b[i:i + 2] for i in range(len(b) - 1)}
    if not ga or not gb:
        return 1.0 if a == b else 0.0
    return len(ga & gb) / len(ga | gb)


def _recent(history: list[dict], n: int) -> str:
    lines = []
    for h in history[-n:]:
        lines.append(f"问：{h['q'][:80]}")
        if h.get("a"):
            lines.append(f"答：{h['a'][:160]}")
    return "\n".join(lines)

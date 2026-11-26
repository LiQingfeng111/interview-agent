"""面试阶段状态机 —— 本项目的核心。

为什么不用"让模型自己决定下一步"？
因为实测中模型有三个稳定缺陷：
  1. 会跳阶段（自我介绍还没问完就去问八股）
  2. 会卡死（反复问同一个点）
  3. 会忘记截止（面试聊到 40 轮）

解法：把"节奏"从模型手里拿走，交给代码。模型只负责"出题"和"打分"，
推进与否由 can_exit() 依据可观察信号（轮数、评估结果、命中点数）决定。
这就是"用确定性骨架包裹非确定性模型"。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import LADDER, PHASE_CONFIG, allocate_phase_budget
from probe_tree import MAX_ELEMENT_ATTEMPTS, ProbeTree

ORDER = ["OPENING", "SELF_INTRO", "PROJECT_PROBE", "TECH_QA", "CANDIDATE_ASK", "FEEDBACK"]


@dataclass
class ProbeState:
    """项目深挖阶段的内部状态。

    注意（S5 切换后）：要素账本已搬到 `ProbeTree`，这里只剩"推进游标"
    （当前项目、当前木梯层级、连续失败次数等）。要素相关的读写全部走
    `self.tree`，`covered_elements / element_attempts / element_status`
    三个属性是**只读视图**，保留它们是为了报告和旧测试不用改。
    """
    projects: list[str] = field(default_factory=list)   # 待深挖项目名
    current: str = ""                                   # 当前项目
    ladder_level: int = 0                               # 0..5 木梯层级
    asked_points: set[str] = field(default_factory=set) # 已问过的关键点（去重）
    covered_layers: dict[str, set[int]] = field(default_factory=dict)  # 每个项目打到过几层
    strikes: int = 0                                    # 连续 fail 次数（防死磕）
    attempts: int = 0                                   # 当前层级已追问次数（防原地打转）
    exhausted: bool = False                             # 可挖项目是否已耗尽
    done: set[str] = field(default_factory=set)         # 已完成深挖的项目
    tree: "ProbeTree" = field(default_factory=lambda: ProbeTree())

    # ---- 只读视图：要素账本的真身在树里 ----
    @property
    def covered_elements(self) -> dict[str, set[str]]:
        return self.tree.covered

    @property
    def element_attempts(self) -> dict[tuple[str, str], int]:
        return self.tree.attempts

    @property
    def element_status(self) -> dict[tuple[str, str], str]:
        return self.tree.status

    def mark_elements(self, project: str, elements: list[str]) -> None:
        """记录"候选人已经讲到过的要素"。

        这是"跳过已答问题"的状态基础：语义判断由评分官（LLM）给出，
        记账必须在这里，否则同一个要素会被反复问。
        """
        self.tree.mark_covered(project, elements)

    def elements_of(self, project: str) -> set[str]:
        return self.tree.elements_of(project)

    # -------------------------------------------------- 要素追问策略
    # 用户定的策略：每个要素最多追问一次 → 判定「会 / 回避 / 不知道」→ 记账放行。
    # 不这么做的话，要么把人卡死在一个点上（逼供），要么让"深度不够"被
    # 掩盖成"没问到"。两种都会让面试失真。
    MAX_ELEMENT_ATTEMPTS = MAX_ELEMENT_ATTEMPTS

    def note_element_attempt(self, project: str, element: str, status: str = "") -> None:
        self.tree.note_attempt(project, element, status)

    def element_closed(self, project: str, element: str) -> bool:
        return self.tree.element_closed(project, element)

    def element_report(self, project: str) -> dict[str, str]:
        return self.tree.coverage(project)

    def mark_covered(self, project: str, level: int) -> None:
        self.covered_layers.setdefault(project, set()).add(level)

    def depth_of(self, project: str) -> int:
        layers = self.covered_layers.get(project, set())
        return max(layers) + 1 if layers else 0


@dataclass
class TechState:
    topics: list[str] = field(default_factory=list)
    asked: set[str] = field(default_factory=set)
    # 题库驱动：每题带 topic/level/question/points，按难度递增 + 话题轮转取
    bank: list[dict] = field(default_factory=list)
    asked_ids: set[str] = field(default_factory=set)
    current: dict = field(default_factory=dict)
    asked_questions: list[dict] = field(default_factory=list)

    def next_topic(self) -> str:
        for t in self.topics:
            if t not in self.asked:
                return t
        return self.topics[0] if self.topics else "基础工程能力"

    def next_question(self) -> dict:
        """取下一道题。题库空了就回退到"围绕话题自由提问"。"""
        for q in self.bank:
            if q.get("id") in self.asked_ids:
                continue
            self.asked_ids.add(q.get("id", ""))
            self.current = q
            self.asked_questions.append(q)
            self.asked.add(q.get("topic", ""))
            return q
        self.current = {}
        topic = self.next_topic()
        self.asked.add(topic)
        return {"topic": topic, "question": "", "points": [], "level": ""}


class InterviewStateMachine:
    def __init__(self, projects: list[str], tech_topics: list[str], max_turns: int = 24):
        self.phase = "OPENING"
        self.turn_in_phase = 0
        self.total_turns = 0
        self.max_turns = max_turns
        # 总时长 → 各阶段题量预算（见 config.allocate_phase_budget）
        self.phase_budget = allocate_phase_budget(max_turns)
        self.probe = ProbeState(projects=list(projects))
        self.tech = TechState(topics=list(tech_topics))
        self.history: list[dict] = []
        self.transitions: list[tuple[int, str, str, str]] = []  # (轮次, 从, 到, 原因)

    # ------------------------------------------------------------ 查询
    @property
    def phase_cfg(self) -> dict:
        # 阶段题量按总时长分配（allocate_phase_budget），不是写死在常量里。
        # 这样用户把"最多轮次"从 24 拖到 10，深挖和技术题会等比收缩，
        # 而不是"深挖照样问 8 轮、把自我介绍和反问挤掉"。
        return self.phase_budget.get(
            self.phase, {"max_turns": 2, "min_turns": 1, "desc": ""})

    @property
    def ladder_name(self) -> str:
        if self.phase != "PROJECT_PROBE":
            return ""
        lvl = min(self.probe.ladder_level, len(LADDER) - 1)
        return LADDER[lvl]

    # ------------------------------------------------------------ 推进
    def record(self, question: str, answer: str, verdict: str = "") -> None:
        self.history.append({"phase": self.phase, "q": question, "a": answer, "verdict": verdict})
        self.turn_in_phase += 1
        self.total_turns += 1

    def can_exit(self) -> bool:
        cfg = self.phase_cfg
        if self.turn_in_phase < cfg["min_turns"]:
            return False
        if self.turn_in_phase >= cfg["max_turns"]:
            return True
        if self.total_turns >= self.max_turns and self.phase != "FEEDBACK":
            return True
        if self.phase == "PROJECT_PROBE":
            return self._probe_done()
        if self.phase == "TECH_QA":
            return len(self.tech.asked) >= len(self.tech.topics)
        return True

    def _probe_done(self) -> bool:
        """什么时候算"深挖够了"？

        判定标准是**每个项目都被标记 done**，而不是"深度到 3 就收工"。
        第一版用 depth>=3 判定，导致强候选人只被问到"怎么做"就结束——
        正好把最能拉开差距的"取舍/边界/反事实"三层全跳过了。
        现在的规则：项目必须打到顶（反事实层）或连续 fail 被判定挖不动，
        才算 done；全部 done 才退出深挖阶段。max_turns 仍是最终保险丝。
        """
        if self.probe.exhausted:
            return True
        if not self.probe.projects:
            return self.turn_in_phase >= 3
        return all(p in self.probe.done for p in self.probe.projects)

    def advance(self, reason: str = "") -> str:
        idx = ORDER.index(self.phase)
        if idx >= len(ORDER) - 1:
            return self.phase
        nxt = ORDER[idx + 1]
        self.transitions.append((self.total_turns, self.phase, nxt, reason))
        self.phase = nxt
        self.turn_in_phase = 0
        if nxt == "PROJECT_PROBE" and self.probe.projects and not self.probe.current:
            self.probe.current = self.probe.projects[0]
        return self.phase

    def maybe_advance(self, reason: str = "") -> str:
        if self.phase == "FEEDBACK":
            return self.phase
        return self.advance(reason) if self.can_exit() else self.phase

    # ------------------------------------------------------------ 深挖调度
    def note_eval(self, verdict: str, weakest: str, followup_hint: str) -> None:
        """依据评估结果更新木梯位置。

        策略（这是调过多轮才定下来的）：
          - pass    → 升一层
          - partial → 同层最多再追一次，第二次仍 partial 就升层
                      （答不完美不代表没答，卡着不动等于浪费轮次）
          - fail    → 同层换角度重问；同层两次都 fail 就**升到下一层**继续问，
                      只有打到最高层（反事实）还答不到，才换项目
        任何一次层级/项目变动都重置 attempts，否则新层级会立刻被判"超限"。

        为什么 fail 不再直接换项目（这是实测改出来的）：
        接真实模型后评分偏严，10 问里 9 个 fail，于是"连续两次 fail 换项目"
        在第 0 层就把项目切走了——木梯永远到不了取舍/边界/反事实。
        而"答不上来"本来就应该换一个角度或换一层问，不是判定这个人不行。
        换项目只在"整个项目挖不动"时才合理。
        """
        if self.phase != "PROJECT_PROBE":
            return
        p = self.probe.current
        if p:
            self.probe.mark_covered(p, self.probe.ladder_level)

        if verdict == "pass":
            self.probe.strikes = 0
            self._level_up()
        elif verdict == "partial":
            self.probe.strikes = max(0, self.probe.strikes - 1)
            self.probe.attempts += 1
            if self.probe.attempts >= 2:
                self._level_up()
        else:  # fail
            self.probe.strikes += 1
            self.probe.attempts += 1
            if self.probe.attempts >= 2:
                self._level_up()

    def _level_up(self) -> None:
        self.probe.attempts = 0
        if self.probe.ladder_level < len(LADDER) - 1:
            self.probe.ladder_level += 1
        else:
            self._next_project("该项目已打到最高层")

    def _next_project(self, reason: str) -> None:
        self.probe.strikes = 0
        self.probe.attempts = 0
        self.probe.ladder_level = 0
        # 用 done 集合而不是"排除当前"，否则会在项目间来回横跳
        # （第一版 bug：A→B 后 remaining 又包含 A，A 被重复深挖）
        self.probe.done.add(self.probe.current)
        remaining = [p for p in self.probe.projects if p not in self.probe.done]
        if remaining:
            self.probe.current = remaining[0]
            self.transitions.append((self.total_turns, "PROBE:" + reason, self.probe.current, "换项目"))
        else:
            self.probe.exhausted = True
            self.transitions.append((self.total_turns, "PROBE:" + reason, "EXHAUSTED", "项目已挖完"))

    def probe_snapshot(self) -> dict:
        return {
            "current": self.probe.current,
            "ladder_level": self.probe.ladder_level,
            "ladder_name": self.ladder_name,
            "strikes": self.probe.strikes,
            "depth": {p: self.probe.depth_of(p) for p in self.probe.projects},
        }

    # ------------------------------------------------------------ 诊断
    def deepest_layer_reached(self) -> str:
        return LADDER[self.deepest_layer_index()]

    def deepest_layer_index(self) -> int:
        if not self.probe.covered_layers:
            return 0
        best = max(max(v) for v in self.probe.covered_layers.values())
        return min(best, len(LADDER) - 1)

    def summary(self) -> dict:
        return {
            "phase": self.phase,
            "total_turns": self.total_turns,
            "probe": self.probe_snapshot(),
            "tech_asked": sorted(self.tech.asked),
            "transitions": self.transitions,
            # 每个项目已覆盖的深挖要素（含空集合，便于报告显示缺口）
            "covered_elements": {
                p: sorted(self.probe.covered_elements.get(p, set()))
                for p in self.probe.projects
            },
            # 每个要素的三态：已答到 / 回避 / 未答出 / 未问到
            "element_report": {
                p: self.probe.element_report(p) for p in self.probe.projects
            },
        }

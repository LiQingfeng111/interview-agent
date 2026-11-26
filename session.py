"""会话编排：把三个 Agent 和状态机串起来。

这是"编排层（orchestration）"，它自己**不含任何 prompt**。
职责：
  1. 每个阶段该问什么 → 交给 Interviewer
  2. 每个答案怎么打分 → 交给 Evaluator
  3. 结果如何影响节奏 → 交给 StateMachine
  4. 最后怎么总结   → 交给 Coach

这样分层的好处：任何一环想换实现（比如把 Evaluator 换成小模型省钱），
都不用动其他代码。面试官问"你的 Agent 是什么架构"时，这就是答案。
"""
from __future__ import annotations

import os
import briefing
import time
import uuid

from coach import Coach, TurnCoach, render_card
from config import ROLE_KB, SELF_INTRO_POINTS
from evaluator import Evaluator, score_turns
from interviewer import Interviewer
from knowledge import pick_tech_topics, resolve_role, role_topics
from bank import pick_questions
from diagnose import diagnose
from llm import BaseLLM
from planner import build_plan, mock_plan, validate_plan
from profile import build_profile
from prompts_loader import load_prompt
from quality import summarize as quality_summary
from websearch import gather as web_gather
from resume import pick_probe_targets, resume_quality
from schemas import Project, SessionResult, Turn
from state_machine import InterviewStateMachine
from store import save_session
from budget import Budget
from invariants import check_all
from ledger import E, Ledger
from replay import Cassette, cassette_path, ledger_path
from tracing import TracedLLM
from memory import Memory
from probe_tree import ProbeTree
from verifier import REASON_CN, coverage_ratio, evidence_audit, plan_progress, should_finish


class InterviewSession:
    def __init__(
        self,
        llm: BaseLLM,
        projects: list[Project],
        role_key: str = "agent_app",
        max_turns: int = 30,
        tech_topics: list[str] | None = None,
        verbose: bool = True,
        save: bool = True,
        seed: int | None = None,
        persona: str = "balanced",
        jd_keywords: list[str] | None = None,
        use_plan: bool = False,
        prompt_version: str = "default",
        web: bool = False,
        coach: bool = False,
        audit: bool = False,
        audit_path: str = "",
    ):
        self.llm = llm
        self.role_key = resolve_role(role_key)
        self.projects = projects
        self.verbose = verbose
        self.save = save
        self.persona = persona
        self.jd_keywords = jd_keywords or []

        targets = pick_probe_targets(projects, limit=2)
        probe_names = [p.name for p in targets]
        # JD 里出现的话题优先考，其余按岗位随机（seed 固定保证可复现）
        topics = tech_topics or pick_tech_topics(
            self.role_key, n=3, seed=seed, prefer=self.jd_keywords
        )

        self.sm = InterviewStateMachine(probe_names, topics, max_turns=max_turns)
        # 题库驱动技术环节：按难度递增 + 话题轮转取题（原来是抽 3 话题问 3 题）
        self.sm.tech.bank = pick_questions(self.role_key, limit=8)
        # 面试前计划：LLM 读简历出清单，代码只管校验覆盖
        self.plan = build_plan(llm, targets, self.jd_keywords) if use_plan else {}
        # 即使没开 --plan，也要有一份**确定性的要素计划**：
        # 没有它，提问官只能按木梯层级走，12 轮全耗在一个项目的同一层上，
        # 实测 5 个深挖要素只覆盖到 3 个。mock_plan 不花 token、结果稳定，
        # 正好当"默认要素清单"用（--plan 时用 LLM 生成的更贴简历的版本）。
        if not self.plan:
            try:
                self.plan = mock_plan(targets)
            except Exception:
                self.plan = {}
        self.plan_issues: list[str] = []
        if use_plan:
            _, self.plan_issues = validate_plan(self.plan, targets)
        self.interviewer = Interviewer(
            llm,
            persona=self.persona,
            jd_keywords=self.jd_keywords,
            plan=self.plan,
            system_prompt=load_prompt("interviewer", prompt_version),
        )
        self.prompt_version = prompt_version
        # 运行时联网：追问参考 + 事实核对。默认关，因为会引入延迟和外部依赖；
        # 打开后失败也只会降级成 unverifiable，不会让面试中断。
        self.web = web
        self._web_cache: dict[str, str] = {}
        self._web_turns = 0
        self.evaluator = Evaluator(llm)
        self.coach = Coach(llm)

        self.turns: list[Turn] = []
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._project_by_name = {p.name: p for p in projects}

        # ---------------- 审计层（账本 / 预算 / 录制）----------------
        # 默认**内存账本**：零成本、零副作用，任何一场都能打印审计数字。
        # 只有 save/audit 时才落盘，避免把测试和演示目录塞满 jsonl。
        self.audit = audit
        # 秒级时间戳 + 4 位随机：同一秒内连开两场会撞 id，
        # 撞了之后 SQLite 里后一场会覆盖前一场（实测：连开 3 场只剩 1 条记录）
        _sid = time.strftime("aud_%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
        # 注意：这里必须**调用** ledger_path(sid)。上一版把函数对象当路径传了，
        # 结果 _flush 里 os.path.abspath(function) 抛 TypeError 被吞掉，
        # 落盘永远失败、账本静默降级成内存模式——"可重放"就成了空话。
        _lpath = audit_path or (ledger_path(_sid) if (audit or save) else "")
        self.ledger = Ledger(path=_lpath, session_id=_sid)
        self.budget = Budget()
        self.memory = Memory()
        # 主路径：树就是状态本身（在 sm.probe.tree），会话不再单独持有一份
        self.tree = self.sm.probe.tree
        # 评估模式：每轮额外给「为什么扣分 + 示范回答」，默认关闭（多一次模型调用）
        self.turn_coach = TurnCoach(self.llm, enabled=coach)
        self.cards: list[dict] = []
        self._last_card: dict = {}
        self.sm.memory = self.memory
        # cassette 跟账本放同一目录，便于"一场 = 一对文件（events/cassette）"
        if _lpath:
            _cdir = os.path.dirname(os.path.abspath(_lpath))
            _cname = os.path.basename(_lpath).replace("events_", "cassette_")
            self.cassette = Cassette(os.path.join(_cdir, _cname))
        elif audit or save:
            self.cassette = Cassette(cassette_path(self.ledger.session_id))
        else:
            self.cassette = Cassette("")
        self.llm = TracedLLM(llm, self.ledger, self.budget, self.cassette)
        self.interviewer.llm = self.llm
        self.evaluator.llm = self.llm
        # 教练也必须走 TracedLLM：否则那一次调用不进账本、不进预算、不进重放——
        # 实测四组对比时发现账本里恒为 3 次调用，多了 3 张卡却一分钱没记。
        # 漏记的代价是"成本与调用次数"全部失真，审计层再漂亮也是假的。
        self.turn_coach.llm = self.llm
        self.coach.llm = self.llm
        self.violations: list = []
        self.finish_reason = ""
        self._recent_questions: list[str] = []
        self._tree_node = ''
        self.ledger.append(E.RESUME_PARSED,
                           {"projects": [p.name for p in projects],
                            "metrics_ratio": resume_quality(projects)["metrics_ratio"]})
        if self.plan:
            self.ledger.append(E.PLAN_BUILT,
                               {"projects": len(self.plan.get("projects") or []),
                                "enabled": bool(use_plan)})

    # ------------------------------------------------------------ 审计
    def _check(self, when: str, ctx: dict) -> list:
        """跑一组不变量，违规既记账也留在内存里供报告使用。

        返回值是**本次**违规，而不是累计列表——上一版 `_guard_question` 读累计列表，
        导致 INV-02 触发过一次之后，此后每一问都被判定为 fatal 而被替换，
        问题全变成兜底句。这是个隐蔽的功能性 bug：报告看不出异常，对话全是套话。
        """
        ctx.setdefault("recent_questions", self._recent_questions[:-1])
        ctx.setdefault("plan_empty", not self.plan)
        ctx.setdefault("budget_level", self.budget.level())
        ctx.setdefault("degraded_noted", bool(self.budget.exceeded_log))
        found = check_all(ctx, only=when)
        for v in found:
            self.violations.append(v)
            self.ledger.append(E.INVARIANT_VIOLATED, v.to_dict())
        return found

    def _guard_question(self, question: str, ctx: dict, found: list) -> str:
        """问题级 fatal → 回退兜底，而不是把坏问题发出去。

        只对**本次**检查出的 fatal 生效（复读 / 要素追问超限）。
        """
        if not any(v.severity == "fatal" and v.id in ("INV-01", "INV-02") for v in found):
            return question
        try:
            fallback = self.interviewer._fallback(self.sm, stack=ctx.get("stack", ""))
        except Exception:
            fallback = ""
        if fallback and fallback != question:
            self.ledger.append(E.EVAL_DEGRADED,
                               {"why": "invariant_fatal", "from": question[:40]})
            return fallback
        return question

    # ------------------------------------------------------------ 主循环
    # ------------------------------------------------------------ 联网
    def _web_term(self, sm) -> str:
        """本轮检索词：必须是**外部可检索的技术主题**，不能是项目名。

        踩过的坑：拿"高校课程问答助手 + 目标"去搜，搜不到任何东西，
        因为那是候选人的私有项目名，互联网上没有这个词条 →
        结果每轮 fact_status 都是 unverifiable，联网等于没开。
        改成用「技术栈 + 该层主题」检索（FAISS 取舍 / RAG 边界），才是真正
        能查到外部资料的查询。
        """
        if sm.phase == "TECH_QA":
            q = sm.tech.current or {}
            return q.get("topic") or (sm.tech.topics[0] if sm.tech.topics else "")
        if sm.phase == "PROJECT_PROBE":
            proj = next((p for p in self.projects if p.name == sm.probe.current), None)
            stack = " ".join((proj.stack or [])[:2]) if proj else ""
            level_topic = {
                0: "是什么", 1: "为什么", 2: "怎么实现", 3: "取舍", 4: "边界 失效", 5: "重做 反事实",
            }.get(sm.probe.ladder_level, "")
            term = f"{stack} {level_topic}".strip()
            return term if stack else ""
        return ""

    def _web_refs(self, term: str) -> str:
        """带内存缓存的检索：同一话题一场面试里只查一次。"""
        if not term:
            return ""
        if term in self._web_cache:
            return self._web_cache[term]
        try:
            pack = web_gather(term, kind="fact", max_chars=700)
            text = pack.get("text", "") if pack.get("ok") else ""
        except Exception:
            text = ""
        self._web_cache[term] = text
        return text

    def step(self, answer: str) -> str:
        """喂入候选人的一句话，返回面试官下一句话。

        单步语义（无副作用扩散）：
          answer → 评分 → 更新状态机 → 生成下一问
        这个顺序很重要：状态机必须在生成问题之前就看到评分，
        否则木梯永远升不上去。
        """
        sm = self.sm
        prev_q = self._last_question()
        prev_target = getattr(self, "_pending_target", "")

        # 0) 联网准备：本轮的参考资料，追问和验答共用一份（同一主题，不重复检索）
        refs = ""
        if self.web:
            term = self._web_term(sm)
            refs = self._web_refs(term)
            self.interviewer.web_refs = refs
            if refs:
                self._web_turns += 1
        else:
            self.interviewer.web_refs = ""

        # 1) 本阶段是否该收尾？先推进再出题，避免多问一句
        if prev_q:
            ev = self.evaluator.evaluate(
                prev_q,
                answer,
                phase=sm.phase,
                ladder_name=sm.ladder_name,
                project=sm.probe.current,
                target_element=prev_target,
                expected_points=self._expected_points(),
                web_refs=refs,
            )
            self._record(prev_q, answer, ev)
            # 评估模式：给这一轮出"为什么扣分 + 缺什么 + 示范回答"
            # 只读评分结果，不改 verdict —— 教练层绝不能影响判定
            if self.turn_coach.enabled:
                card = self.turn_coach.review(
                    prev_q, answer, ev,
                    project=sm.probe.current, phase=sm.phase,
                    points=(sm.tech.current or {}).get("points") or [],
                )
                self.cards.append(card)
                self._last_card = card
                if self.verbose:
                    self._emit(render_card(card))
            # 记录已覆盖要素 + 该要素的追问次数与判定（会/回避/不知道）
            sm.probe.mark_elements(sm.probe.current, getattr(ev, "covered", []) or [])
            self.tree.mark_covered(sm.probe.current, getattr(ev, "covered", []) or [])
            if prev_target:
                sm.probe.note_element_attempt(
                    sm.probe.current, prev_target, getattr(ev, "target_status", "") or ""
                )
            # 审计：评分返回后立刻查 evidence 是否编造（INV-04 是 fatal）
            self._check("eval.returned", {
                "eval": ev.to_dict() if hasattr(ev, "to_dict") else {},
                "answer": answer,
                "project": sm.probe.current,
                "element": prev_target,
                # 模型判定与阈值判定打架时要留痕：否则"尺子被模型覆盖"永远查不出来
                "verdict_conflict": bool(getattr(ev, "verdict_conflict", False)),
                "model_verdict": getattr(ev, "model_verdict", ""),
            })
            self.ledger.append(E.EVAL_RETURNED, {
                "verdict": getattr(ev, "verdict", ""),
                "model_verdict": getattr(ev, "model_verdict", ""),
                "verdict_conflict": bool(getattr(ev, "verdict_conflict", False)),
                "score": getattr(ev, "score", None),
                "evidence": getattr(ev, "evidence", ""),
                "target_status": getattr(ev, "target_status", ""),
            })
            # 事实账本：抽数字、登记、把旧值标失效；同一属性出现两个值就是矛盾
            before = len(self.memory.contradictions)
            for fact in self.memory.observe(answer, sm.probe.current, len(self.turns)):
                self.ledger.append(E.MEMORY_FACT, fact.to_dict())
            for c in self.memory.contradictions[before:]:
                self.ledger.append(E.MEMORY_CONTRADICTION, c.to_dict())
            sm.note_eval(ev.verdict, ev.weakest, ev.followup_hint)
            sm.record(prev_q, answer, ev.verdict)
            # 影子树：记判定 + 关节点（只写不读，状态推进仍由计数器负责）
            if self._tree_node:
                self.tree.note_verdict(self._tree_node, getattr(ev, "verdict", ""),
                                       getattr(ev, "evidence", ""))
                self.tree.close(self._tree_node,
                                getattr(ev, "verdict", "") or "answered")
                if prev_target and sm.probe.element_closed(sm.probe.current, prev_target):
                    self.tree.close_element(sm.probe.current, prev_target, "element_closed")
                self._tree_node = ""
            if sm.phase != "FEEDBACK":
                sm.maybe_advance("阶段完成")

        # 预算兜底：level 2 不再调模型，直接收尾（否则会烧穿预算）
        if self.budget.level() >= 2 and sm.phase != "FEEDBACK":
            self.ledger.append(E.BUDGET_EXCEEDED, self.budget.summary())
            sm.phase = "FEEDBACK"
            return self.finish()

        # 2) 出下一问
        if sm.phase == "FEEDBACK":
            return self.finish()

        last_eval = self.turns[-1].evaluation.to_dict() if self.turns and self.turns[-1].evaluation else None
        # 有未追问的矛盾时，先用一句话要求澄清（这是系统自己的记账，不靠模型想起来）
        pending = self.memory.pending_contradiction()
        self.interviewer.contradiction = pending
        if pending is not None:
            self.memory.mark_questioned(pending)
            self.ledger.append(E.MEMORY_CONTRADICTION,
                               {**pending.to_dict(), "questioned": True})
        # 注意：不要把候选人原文当 topic 传进去。
        # 第一版这么写，导致技术问答的话题变成"候选人上一句的整段回答"，
        # 提问官于是围绕那段话出题，知识库形同虚设。话题必须由状态机给。
        stack = ""
        if sm.phase == "PROJECT_PROBE" and sm.probe.current:
            p = self._project_by_name.get(sm.probe.current)
            if p:
                stack = "/".join(p.stack[:4])
        question = self.interviewer.next_question(sm, last_eval=last_eval, stack=stack)        # 审计：问题级不变量（复读/过短/要素超限）在此拦截
        found = self._check("turn.asked", {
            "question": question,
            "project": sm.probe.current,
            "element": getattr(self.interviewer, "last_target", "") or "",
            "element_attempts": (sm.probe.element_attempts.get(
                (sm.probe.current, getattr(self.interviewer, "last_target", "")), 0)
                if hasattr(sm.probe, "element_attempts") else 0),
            "in_plan": bool(self.plan),
            "stack": stack,
        })
        question = self._guard_question(question, {"stack": stack}, found)
        self._recent_questions.append(question)
        self._emit(question)
        self.ledger.append(E.TURN_ASKED, {
            "question": question, "phase": sm.phase,
            "project": sm.probe.current, "level": sm.probe.ladder_level,
            "element": getattr(self.interviewer, "last_target", "") or "",
        })
        self._pending_question = question
        # 影子树建节点：记录"这一问是追问哪个要素、追到第几层、父节点是谁"
        if sm.phase == "PROJECT_PROBE":
            self._tree_node = self.tree.add_question(
                sm.probe.current, sm.probe.ladder_level,
                getattr(self.interviewer, "last_target", "") or "", question,
            )
        # 记录这一问瞄准的要素，供下一轮记账（会/回避/不知道）
        self._pending_target = getattr(self.interviewer, "last_target", "") or ""
        return question

    def start(self) -> str:
        q = self.interviewer.next_question(self.sm)
        self._recent_questions.append(q)
        self._emit(q)
        self.ledger.append(E.TURN_ASKED, {
            "question": q, "phase": self.sm.phase,
            "project": self.sm.probe.current, "level": self.sm.probe.ladder_level,
            "element": getattr(self.interviewer, "last_target", "") or "",
        })
        self._pending_question = q
        self._pending_target = getattr(self.interviewer, "last_target", "") or ""
        return q

    def finish(self) -> str:
        # 结束原因走闭集：报告里能回答"为什么停了"
        done, reason = should_finish(self.sm, self.budget)
        return self._wrap_up(reason or "max_turns")

    def end_early(self, reason: str = "user_quit") -> str:
        """候选人主动喊停。

        为什么要有这条路：面试是有心理压力的场景，卡住、心态崩了都很正常。
        没有"体面退出"的按钮，人只能直接关窗口——那场数据就废了，
        而且他得不到任何反馈。停下来也应该拿到完整报告。
        结束原因进闭集（user_quit），报告里如实写"由候选人提前结束"，
        分数不因此打折，缺口标"未问到"而不是"未答出"。
        """
        self.sm.phase = "FEEDBACK"
        return self._wrap_up(reason or "user_quit")

    def _wrap_up(self, reason: str) -> str:
        self.finish_reason = reason
        stats = score_turns(self.turns)
        role_name = ROLE_KB.get(self.role_key, {}).get("name", self.role_key)
        feedback = self.coach.feedback(self.sm, self.turns, stats, role_name)
        self.feedback = feedback
        self._emit(feedback)
        self.ledger.append(E.FINISH, {
            "reason": self.finish_reason,
            "turns": len(self.turns),
            "fatal": self.ledger.fatal_count(),
            "warn": self.ledger.warn_count(),
            "budget": self.budget.summary(),
        })
        if self.save:
            try:
                save_session(self.result())
            except Exception:
                pass
        return feedback

    # ------------------------------------------------------------ 便捷
    def run_scripted(self, answers: list[str]) -> SessionResult:
        """脚本化跑完整场，用于评估与回归（不需要人机交互）。"""
        self.start()
        pending = list(answers)
        while self.sm.phase != "FEEDBACK":
            ans = pending.pop(0) if pending else "（候选人沉默）"
            self.step(ans)
            if self.sm.total_turns > 40:
                break
        return self.result()

    def result(self) -> SessionResult:
        stats = score_turns(self.turns)
        # 能力画像与"系统评估"是两层：这里算的是"这个人强不强"
        profile = build_profile(
            self.turns,
            stats,
            self.projects,
            self.sm.deepest_layer_index(),
            mode="mock" if getattr(self.llm, "is_mock", False) else "real",
        )
        # 短板分类：要素三态 × rubric 弱维 → 固定 7 类（区分"不会"与"没意识"）
        sm_summary = self.sm.summary()
        diagnosis = diagnose(
            sm_summary.get("element_report") or {},
            stats.get("dims") or {},
            stats.get("weakest_dim", ""),
        )
        # 提问质量代理指标（锚定率/承接率）：流程指标之外的"问得好不好"下限
        question_quality = quality_summary(self.turns, self.projects)
        audit = {
            "session_id": self.ledger.session_id,
            "events": len(self.ledger.events),
            "fatal": self.ledger.fatal_count(),
            "warn": self.ledger.warn_count(),
            "violations": [v.to_dict() for v in self.violations[:20]],
            "chain_problems": self.ledger.verify_chain(),
            "finish_reason": self.finish_reason or "max_turns",
            "finish_reason_cn": REASON_CN.get(self.finish_reason, self.finish_reason),
            "budget": self.budget.summary(),
            "evidence": evidence_audit(self.turns),
            "cassette": self.cassette.stats(),
            "ledger_path": self.ledger.path,
        }
        return SessionResult(
            role=self.role_key,
            projects=self.projects,
            turns=self.turns,
            feedback=getattr(self, "feedback", ""),
            meta={
                "started_at": self.started_at,
                "stats": stats,
                "sm": sm_summary,
                "profile": profile,
                "plan_enabled": bool(self.plan),
                "diagnosis": diagnosis,
                "quality": question_quality,
                "bank_asked": list(getattr(self.sm.tech, "asked_questions", []) or []),
                "web": {"enabled": self.web, "turns_with_refs": self._web_turns},
                "resume_quality": resume_quality(self.projects),
                "audit": audit,
                "tree": self.tree.stats(),
                "tree_text": self.tree.render_text(),
            "briefing": briefing.build(self).to_dict(),
            "briefing_text": briefing.build(self).render(),
                "memory": {
                    "facts": [f.to_dict() for f in self.memory.facts],
                    "contradictions": [c.to_dict() for c in self.memory.contradictions],
                    "digest": self.memory.prompt_digest(),
                },
                "coverage_ratio": coverage_ratio(
                    plan_progress(self.plan, self.sm.probe, [p.name for p in self.projects])
                ),
            },
        )

    def _expected_points(self) -> list[str]:
        """这一轮该拿什么考点去量。

        自我介绍用 SELF_INTRO_POINTS，而不是项目深挖的考点：
        深挖问"为什么这么做、代价是什么"，自我介绍问"90 秒能不能说清你是谁"。
        之前在自我介绍上套用深挖考点，一段合格的自我介绍被判 fail——
        这是拿真实截图看出来的，不是测试抓到的（测试只验流程，不验合理性）。
        """
        if self.sm.phase == "SELF_INTRO":
            return list(SELF_INTRO_POINTS)
        return list((self.sm.tech.current or {}).get("points") or [])

    def _record(self, question: str, answer: str, ev) -> None:
        self.turns.append(
            Turn(
                idx=len(self.turns) + 1,
                phase=self.sm.phase,
                question=question,
                answer=answer,
                evaluation=ev,
                ladder_level=self.sm.probe.ladder_level,
                project=self.sm.probe.current,
            )
        )

    def _last_question(self) -> str:
        return getattr(self, "_pending_question", "")

    def _emit(self, text: str) -> None:
        if self.verbose:
            print(text)

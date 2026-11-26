"""会话运行时：把旧的 InterviewSession 拆成"图节点可调用的四个方法"。

## 为什么要拆

旧 `InterviewSession.step(answer)` 是一体的：
评分 → 状态机推进 → 出题，全在一个函数里。

LangGraph 需要它们**分开**，因为：
- `evaluate` 节点只做评分
- `route` 节点只推进状态机（纯规则）
- `probe` 节点只出题

拆开的好处不只是"适配图"，而是**可以单独测试每一环**：
"评分挂了但状态机还能推进" 这种场景，一体式函数根本没法构造。

## 不改旧代码

这里**不重写** `step()` 的内部逻辑。
做法是：内部仍持有旧的 `InterviewSession` 实例，
把它的能力按需暴露成四个方法，并在必要时读它的内部状态
（`sm`、`turns`、`interviewer` 等）。

风险是"读内部状态"可能因旧代码改动而失效 → 所以每处读取都用
`getattr(obj, name, default)` 做防御，读不到就降级而不是崩。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import bridge

log = logging.getLogger("interview.runtime")


class SessionRuntime:
    """一场面试的运行时。

    职责：持有旧 session 实例 + 图需要的那几个动作。
    """

    def __init__(self, session_id: str, user: str, req: Any):
        self.session_id = session_id
        self.user = user
        self.req = req
        self._ask_seq = 0
        self._last_target = ""
        self._session = self._build_legacy_session()

    # ------------------------------------------------------------ 构建
    def _build_legacy_session(self) -> Any:
        """构造旧的 InterviewSession。

        参数映射注意：
        - 旧类要 `resume` 文本，自己内部解析项目
        - `llm` 必须给（它内部会调模型）；moot 模式传 MockLLM
        """
        bridge.ensure_legacy_importable()

        from llm import MockLLM, build_llm  # type: ignore
        from resume import parse_resume  # type: ignore
        from session import InterviewSession  # type: ignore

        req = self.req
        if getattr(req, "mock", False):
            llm = MockLLM(seed=1)
        else:
            llm = build_llm(mode="auto")

        # 包一层记账：让每次模型调用的 token/成本能归到本会话
        #
        # 关键点：包装后的对象要**替换所有持有者**，不能只传给 InterviewSession。
        # 旧 `session.py` 里 `Evaluator(llm)` 拿的是**原始 llm**（第 112 行），
        # 而 `self.llm = TracedLLM(...)` 只更新了 session 自己和 interviewer。
        # 所以只包一次的话，**评分路径的调用完全不记账**——
        # 实测现象：6 轮面试只记到 6 次（出题），评分那 6 次丢了。
        try:
            from app.core import usage_patch
            llm = usage_patch.wrap_llm(llm)
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 记账包装失败（成本统计会缺）：%s", e)

        projects = []
        try:
            projects = parse_resume(getattr(req, "resume", "") or "", llm=None)
        except Exception as e:  # noqa: BLE001
            log.warning("简历解析失败：%s", e)

        kwargs: dict[str, Any] = {
            "llm": llm,
            "projects": projects,
            "role_key": getattr(req, "role", "ai_algo") or "ai_algo",
            "max_turns": getattr(req, "max_turns", 16) or 16,
            "verbose": False,
            "save": False,               # 不用旧的文件保存，新体系走 PG
            "persona": getattr(req, "persona", "balanced") or "balanced",
            "web": bool(getattr(req, "web", False)),
            "coach": bool(getattr(req, "coach", False)),
        }
        jd = getattr(req, "jd", "") or ""
        if jd:
            kwargs["jd_keywords"] = _jd_keywords(jd)

        session = InterviewSession(**kwargs)

        # 不再包装 LLM！
        #
        # 踩过的坑（两次）：
        # ① 把记账塞进包装类的 `chat()`，每次调用后同步读 logger →
        #    旧 `LLMLogger.write()` 要落盘 + fsync → **单轮从 5 秒涨到 19 秒**。
        # ② 包装对象破坏了旧核心的 `isinstance` / 属性判断，
        #    导致 `evaluator` 里的 `getattr(self.llm, "is_mock")` 走错分支。
        #
        # 现在的方案：**不碰 llm**。记账在图节点里调用完成后做
        # （见 `graph/interview_graph._record_from_logger`），
        # 从 logger 的 `last_usage` 读，完全不进调用路径。
        return session

    @staticmethod
    def _unwrap_llm(llm: Any) -> Any:
        """拆掉已有包装，拿到最内层的原始 LLM。"""
        cur = llm
        for _ in range(3):
            inner = getattr(cur, "_inner", None)
            if inner is None:
                break
            cur = inner
        return cur

    @staticmethod
    def _attach_recording(session: Any, raw_llm: Any) -> None:
        """已废弃：不再包装 LLM，保留空实现避免调用方报错。"""
        return

    # ------------------------------------------------------------ 出题
    def next_question(self, resume_context: str = "", bank_qid: str = "") -> str:
        """出一题。

        **不改旧的出题逻辑**，只在它前面补两件事：
        1. 把 RAG 检索到的简历上下文塞进 interviewer（它已有 web_refs 机制）
        2. 若指定了题库题，把题干塞进 hint

        为什么不直接改 `_build_prompt`：那会破坏 prompt 黄金快照（9 条一致性测试）。
        """
        session = self._session
        interviewer = getattr(session, "interviewer", None)
        sm = getattr(session, "sm", None)
        if interviewer is None or sm is None:
            return "请先做个自我介绍。"

        # 注入 RAG 上下文：复用旧的 web_refs 通道（它就是"外部参考资料"）
        if resume_context:
            try:
                interviewer.web_refs = resume_context
            except Exception:  # noqa: BLE001
                pass

        # 首次出题走 session.start()（它会处理 OPENING 的初始化）
        if self._ask_seq == 0:
            try:
                q = session.start()
                self._ask_seq = 1
                self._last_target = str(getattr(interviewer, "last_target", "") or "")
                return q
            except Exception as e:  # noqa: BLE001
                log.exception("start() 失败，改走 next_question")
                self._ask_seq = 1

        # 后续轮次：直接调 Interviewer（跳过评分，因为评分在 evaluate 节点做）
        try:
            last_eval = self._last_eval_dict()
            stack = ""
            if getattr(sm, "phase", "") == "PROJECT_PROBE" and getattr(sm.probe, "current", ""):
                prog = getattr(session, "_project_by_name", {}).get(sm.probe.current)
                if prog is not None:
                    stack = "/".join(getattr(prog, "stack", [])[:4])
            q = interviewer.next_question(sm, last_eval=last_eval, stack=stack)
            self._last_target = str(getattr(interviewer, "last_target", "") or "")
            return q
        except Exception as e:  # noqa: BLE001
            log.exception("出题失败，用兜底")
            return f"（降级）请继续讲讲{getattr(sm.probe, 'current', '这个项目')}的细节。"

    # ------------------------------------------------------------ 评分
    def _resume_context(self, query: str, k: int = 3) -> str:
        """取与当前问题相关的简历片段，供评分核对一致性。

        ## 为什么评分也需要简历

        出题用简历（问"你简历里写的 71%→82% 是怎么测的"），
        但评分之前**没有简历**，于是：
          - 候选人说"78%"，模型无法判断这和简历里写的 82% 冲突
          - 候选人把项目讲成别人的，模型看不出来

        有了简历片段，评分提示词里就能加一句
        "如果候选人说的数字/做法和简历冲突，consistent 填 false"。

        ## 为什么只取 3 条

        评分提示词里已经有：系统提示 + 评分锚点 + 五个要素 + 对话历史 6 轮。
        再塞太多简历会把上下文撑爆，反而降低评分质量。
        3 条 ≈ 600-900 token，够核对关键数字了。
        """
        if not query.strip():
            return ""
        try:
            from app.rag import retriever

            owner = getattr(self, "user", "") or ""

            # 优先查当前候选人的简历；没有 owner 时退化为全库（单机场景）
            hits = retriever.search_resume(query, owner=owner, k=k)
            if not hits and owner:
                hits = retriever.search_resume(query, owner="", k=k)
            if not hits:
                return ""

            parts: list[str] = []
            for h in hits:
                # parent_text 是切片时保留的完整段落，上下文更完整
                txt = (getattr(h, "parent_text", "") or h.text or "").strip()
                if txt:
                    parts.append(txt[:500])
            return "\n---\n".join(parts)
        except Exception as e:  # noqa: BLE001
            log.debug("取简历片段失败：%s", e)
            return ""

    # ------------------------------------------------------------ 评分
    def evaluate_answer(self, question: str, answer: str, element: str = "") -> dict:
        """评一题。只评分，不推进状态机。

        旧实现里评分和推进混在 `step()` 里；这里只调 evaluator，
        所以"评分失败不影响状态机"成为可能。
        """
        session = self._session
        evaluator = getattr(session, "evaluator", None)
        sm = getattr(session, "sm", None)

        if evaluator is None:
            return {"verdict": "partial", "avg": 2.5, "evidence": "", "degraded": True}

        # ---- 取对话历史（之前完全没传，是"评分只看当前一句"的根因）----
        #
        # 来源：状态机的 history（每轮 record() 会追加 {q, a, verdict}）
        # 只取最近 6 轮，控制 token（一轮约 300 token，6 轮约 1.8k）
        history: list[dict] = []
        try:
            raw_hist = getattr(sm, "history", None) or []
            for h in raw_hist[-6:]:
                if not isinstance(h, dict):
                    continue
                history.append({
                    "q": h.get("q") or h.get("question") or "",
                    "a": h.get("a") or h.get("answer") or "",
                    "verdict": h.get("verdict") or "",
                })
        except Exception as e:  # noqa: BLE001
            log.warning("读取对话历史失败（评分将缺少上下文）：%s", e)

        # ---- 取简历片段（之前没传，模型无法核对答案与简历是否一致）----
        resume_context = ""
        try:
            resume_context = self._resume_context(question)
        except Exception as e:  # noqa: BLE001
            log.warning("取简历片段失败（评分将无法核对一致性）：%s", e)

        try:
            ev = evaluator.evaluate(
                question,
                answer,
                phase=str(getattr(sm, "phase", "")),
                ladder_name=str(getattr(sm, "ladder_name", "")),
                project=str(getattr(getattr(sm, "probe", None), "current", "") or ""),
                target_element=element or self._last_target,
                expected_points=self._expected_points(),
                history=history,
                resume_context=resume_context,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("评分异常，降级")
            return {"verdict": "partial", "avg": 2.5, "evidence": "",
                    "degraded": True, "error": str(e)}

        # 记到旧 session 的 turns 里（让它的 get_report 能读到）
        try:
            sm_note = getattr(sm, "note_eval", None)
            if callable(sm_note):
                sm_note(getattr(ev, "verdict", ""), getattr(ev, "weakest", ""),
                        getattr(ev, "followup_hint", ""))
            record = getattr(sm, "record", None)
            if callable(record):
                record(question, answer, getattr(ev, "verdict", ""))
        except Exception as e:  # noqa: BLE001
            log.warning("回写到状态机失败：%s", e)

        # 评估模式的点评卡（coach）
        #
        # 为什么在这里补：前端勾了"评估模式"，后端 `InterviewSession.__init__`
        # 也确实创建了 `turn_coach`，但**只有旧的 `step()` 会调 `review()`**。
        # 我们走的是拆开的 `evaluate_answer()`，所以卡片从来没生成过——
        # 前端拿到的 `card` 永远是 null（用户勾了但看不到任何东西）。
        self._last_card = self._build_coach_card(session, question, answer, ev)

        d = ev.to_dict() if hasattr(ev, "to_dict") else (
            ev if isinstance(ev, dict) else {})

        # ---- 双轨评分：模型分 + 规则分 ----
        #
        # 为什么在模型评分之后再合一次：
        # 模型分只反映"读起来像不像好回答"，规则分反映"有没有硬证据"（数字、
        # 基线、边界、结构）。两个信号融合后的 avg 才既不会漂移，
        # 又不会把有料但朴素的话判死。
        #
        # 合并策略见 app/core/rubric.py 的注释（每维权重不同）。
        try:
            from app.core import rubric

            d = rubric.apply(d, answer, resume_context=resume_context)
        except Exception as e:  # noqa: BLE001
            # 规则分失败不影响主流程：模型分本身可用
            log.warning("双轨评分失败（保留模型分）：%s", e)

        # 补上 avg —— 这里踩过一个隐蔽的坑：
        #
        # 旧 `Evaluation.avg` 是 `@property`，而 `to_dict()` 用的是
        # `dataclasses.asdict()`，**asdict 不包含 property**。
        # 所以 d 里只有 accuracy/depth/... 五个维度，没有 avg。
        #
        # 后果：写 turns 表时 `float(ev.get("avg") or 0)` 恒为 0 →
        # 报告里"均分 0.0"、成本统计对不上、前端雷达图全空。
        # 这个 bug 只有跑真实模型看数据库才发现的，mock 模式下
        # 因为 evaluator 返回 dict 带 avg 所以掩盖了。
        #
        # 注意执行顺序：上面 rubric.apply() 已经按合并后的维度重算过 avg，
        # 所以这里只在**没有 avg** 时才补（不要覆盖双轨结果）。
        if not d.get("avg"):
            dims = [d.get(k) for k in
                    ("accuracy", "depth", "tradeoff", "boundary", "clarity")
                    if isinstance(d.get(k), (int, float))]
            if dims:
                d["avg"] = round(sum(dims) / len(dims), 2)
            else:
                d["avg"] = float(getattr(ev, "avg", 0) or 0)

        self._last_eval = d
        self._last_target = element or self._last_target
        return d

    # ------------------------------------------------------------ 推进
    def advance(self, last_eval: dict) -> tuple[str, int, str]:
        """推进状态机，返回 (phase, ladder_level, ladder_name)。纯规则。"""
        session = self._session
        sm = getattr(session, "sm", None)
        if sm is None:
            return "FEEDBACK", 0, "是什么"

        try:
            if str(getattr(sm, "phase", "")) != "FEEDBACK":
                maybe = getattr(sm, "maybe_advance", None)
                if callable(maybe):
                    maybe("阶段完成")
        except Exception as e:  # noqa: BLE001
            log.warning("状态机推进失败：%s", e)

        probe = getattr(sm, "probe", None)
        return (
            str(getattr(sm, "phase", "FEEDBACK")),
            int(getattr(probe, "ladder_level", 0) or 0),
            str(getattr(sm, "ladder_name", "是什么") or "是什么"),
        )

    # ------------------------------------------------------------ 报告
    def build_report(self) -> dict:
        """生成报告（Markdown + 结构化分维度）。"""
        session = self._session
        text = ""
        try:
            fin = getattr(session, "finish", None)
            if callable(fin):
                text = str(fin() or "")
        except Exception as e:  # noqa: BLE001
            log.warning("报告生成失败：%s", e)

        dims = _aggregate_dims(getattr(session, "turns", []) or [])
        return {"markdown": text, "dims": dims}

    # ------------------------------------------------------------ 恢复
    def _build_coach_card(self, session: Any, question: str, answer: str,
                          ev: Any) -> dict | None:
        """生成评估模式的点评卡。

        复用旧 `TurnCoach.review()`，它返回一个 dict（含"哪里扣分 + 缺什么 + 示范回答"）。
        没启用 coach 或生成失败 → 返回 None（不影响主流程）。
        """
        coach = getattr(session, "turn_coach", None)
        if coach is None or not getattr(coach, "enabled", False):
            return None
        try:
            sm = getattr(session, "sm", None)
            probe = getattr(sm, "probe", None) if sm else None
            tech = getattr(sm, "tech", None) if sm else None
            cur = getattr(tech, "current", None) if tech else None
            points = []
            if isinstance(cur, dict):
                pts = cur.get("points") or []
                points = pts if isinstance(pts, list) else []

            card = coach.review(
                question, answer, ev,
                project=str(getattr(probe, "current", "") or ""),
                phase=str(getattr(sm, "phase", "") or ""),
                points=points,
            )
            if isinstance(card, dict):
                return card
            # 有些实现返回 dataclass
            if hasattr(card, "to_dict"):
                return card.to_dict()
            return None
        except Exception as e:  # noqa: BLE001
            log.warning("生成点评卡失败：%s", e)
            return None

    # ------------------------------------------------------------ 恢复
    def restore(self, progress: dict, asked_qids: list[str],
                topic_counts: dict[str, int]) -> None:
        """把图状态回灌进旧 session 的内部计数器。

        为什么必须做：`InterviewSession` 内部有自己的 `sm.total_turns`、
        `sm.probe.ladder_level` 等计数器。重建实例后它们都是 0，
        如果不回灌，面试会"从第 0 轮重新开始"——这正是实测踩到的现象
        （重启后 answer 返回 turn:0）。

        回灌失败不抛异常：宁可轮次不准，也不要让恢复整个失败。
        """
        session = self._session
        sm = getattr(session, "sm", None)
        if sm is None:
            return

        turn = int(progress.get("turn", 0) or 0)
        phase = str(progress.get("phase", "") or "")
        ladder = int(progress.get("ladder_level", 0) or 0)

        # 轮次
        for attr in ("total_turns", "turn"):
            if hasattr(sm, attr):
                try:
                    setattr(sm, attr, turn)
                except Exception:  # noqa: BLE001
                    pass

        # 阶段
        if phase:
            try:
                sm.phase = phase
            except Exception:  # noqa: BLE001
                pass

        # 木梯层级
        probe = getattr(sm, "probe", None)
        if probe is not None and hasattr(probe, "ladder_level"):
            try:
                probe.ladder_level = ladder
            except Exception:  # noqa: BLE001
                pass

        # 已问题的记录（供防复读与语义去重）
        self._asked_qids = list(asked_qids)
        self._topic_counts = dict(topic_counts)

        # 让"下一次出题"走后续分支而不是 start()
        self._ask_seq = max(1, turn)

        log.info("状态已回灌：turn=%d phase=%s ladder=%d asked=%d",
                 turn, phase, ladder, len(asked_qids))

    def asked_qids(self) -> list[str]:
        return list(getattr(self, "_asked_qids", []))

    def topic_counts(self) -> dict[str, int]:
        return dict(getattr(self, "_topic_counts", {}))

    # ------------------------------------------------------------ 内部
    def _last_eval_dict(self) -> dict | None:
        return getattr(self, "_last_eval", None)

    def _expected_points(self) -> list[str]:
        sm = getattr(self._session, "sm", None)
        tech = getattr(sm, "tech", None) if sm else None
        cur = getattr(tech, "current", None) if tech else None
        if isinstance(cur, dict):
            pts = cur.get("points") or []
            return pts if isinstance(pts, list) else []
        return []


def _jd_keywords(jd: str) -> list[str]:
    """从 JD 里抽技术关键词（简单实现，够用）。"""
    import re
    known = ["RAG", "Agent", "LLM", "PyTorch", "TensorFlow", "LoRA", "Transformer",
             "Redis", "Kafka", "Docker", "Kubernetes", "Python", "Java", "Go",
             "向量", "检索", "微调", "多模态", "prompt", "评测"]
    found = []
    for k in known:
        if re.search(re.escape(k), jd, re.IGNORECASE):
            found.append(k)
    return found[:8]


def _aggregate_dims(turns: list) -> dict[str, float]:
    acc: dict[str, list[float]] = {}
    for t in turns:
        ev = getattr(t, "evaluation", None)
        if ev is None:
            continue
        d = ev.to_dict() if hasattr(ev, "to_dict") else (ev if isinstance(ev, dict) else {})
        for k in ("accuracy", "depth", "tradeoff", "boundary", "clarity"):
            v = d.get(k)
            if isinstance(v, (int, float)):
                acc.setdefault(k, []).append(float(v))
    return {k: round(sum(v) / len(v), 2) for k, v in acc.items()}

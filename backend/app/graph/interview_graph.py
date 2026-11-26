"""LangGraph 状态图：把"一轮面试"表达成可暂停、可恢复的图。

## 为什么用图而不是直接调方法

旧实现是 `session.step(answer)` —— 一个同步函数，跑完返回下一问。
问题在于**它不能暂停**：

- 候选人关掉浏览器，30 分钟后回来 → 内存里的会话没了，只能重来
- 服务重启 → 所有进行中的面试丢失（旧 API 直接返回"会话不存在"）

LangGraph 的 `interrupt()` + checkpointer 解决这个：
图在"等候选人回答"处暂停，整个状态存进数据库；收到答案后 `Command(resume=answer)` 从断点继续。

## 图的结构

```
     ┌─────────┐
     │  plan   │  从简历构建项目列表 + 要素计划（调 old planner）
     └────┬────┘
          ▼
     ┌─────────┐
     │  probe  │  出题：RAG 检索简历 → 选要素 → 调模型措辞
     └────┬────┘
          ▼
    ╔═══════════╗
    ║ interrupt ║  ← 等候选人回答（HTTP 请求到达时 resume）
    ╚═════┬═════╝
          ▼
     ┌─────────┐
     │ evaluate│  评分：调模型 → 阈值规整 → 不变量检查
     └────┬────┘
          ▼
     ┌─────────┐
     │  route  │  纯规则：升木梯 / 换阶段 / 收尾（不调模型）
     └────┬────┘
      ┌───┴───┐
      ▼       ▼
  (继续)   (结束)
      │       ▼
      │  ┌─────────┐
      │  │ report  │  生成报告 + 写长期记忆
      │  └─────────┘
      └──▶ probe
```

## 规则层零重写

`route` 节点内部调的还是旧的 `state_machine.maybe_advance()`；
`evaluate` 节点内部调的还是旧的 `evaluator.evaluate()`。
图只负责**编排 + 持久化**，判定语义一行没改。
这是 379 条旧测试仍然有效的前提。
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

log = logging.getLogger("interview.graph")


# ============================================================================
# 状态定义
# ============================================================================

class InterviewState(TypedDict, total=False):
    """图的状态。

    total=False 的原因：节点是逐步填充的，开局只有 session_id/user/resume。
    强制要求所有字段会让每个节点都要带全量，反而不好维护。
    """
    # 身份
    session_id: str
    user: str
    role: str
    persona: str

    # 输入
    resume_text: str
    jd_text: str
    max_turns: int

    # 计划（plan 节点产出）
    projects: list[str]
    plan: dict

    # 当前轮
    turn: int
    phase: str
    ladder_level: int
    ladder_name: str
    current_project: str
    current_element: str

    # 待回答
    pending_question: str
    pending_element: str

    # 最近一次评分
    last_eval: dict
    last_card: dict
    last_answer: str

    # RAG 上下文（probe 节点注入）
    resume_context: str
    bank_question_id: str
    current_topic: str

    # 运行时
    topic_counts: dict[str, int]
    asked_qids: list[str]
    recent_qa: list[dict]

    # 报告
    report: dict
    done: bool
    finish_reason: str


# ============================================================================
# 节点
# ============================================================================

def node_plan(state: InterviewState) -> dict:
    """计划节点：从简历构建项目列表 + 要素计划。

    这一节点只在第一轮跑一次（之后路由回 probe 不再回到这里）。

    复用旧组件：
    - `resume.parse_resume()` 抽项目
    - `planner.mock_plan()` 生成确定性要素清单（不花 token）
    """
    from app.core import bridge

    bridge.ensure_legacy_importable()
    from resume import parse_resume  # type: ignore
    from planner import mock_plan  # type: ignore

    resume_text = state.get("resume_text", "")
    try:
        projects = parse_resume(resume_text, llm=None)
    except Exception as e:  # noqa: BLE001
        log.warning("简历解析失败：%s", e)
        projects = []

    names = [p.name for p in projects if getattr(p, "name", "")]
    if not names:
        # 解析不出项目 → 用简历首行当占位，保证流程能走
        first_line = next((ln.strip() for ln in resume_text.splitlines() if ln.strip()), "项目")
        names = [first_line[:30]]

    plan: dict = {}
    try:
        plan = mock_plan(projects) if projects else {}
    except Exception as e:  # noqa: BLE001
        log.warning("要素计划生成失败：%s", e)

    # 长期记忆：把"这人上次哪弱"注入计划（没有历史就返回空串）
    user = state.get("user", "")
    digest = ""
    try:
        from app.rag.memory import profile_digest
        digest = profile_digest(user)
        if digest:
            log.info("已注入长期画像（%d 字）", len(digest))
    except Exception as e:  # noqa: BLE001
        log.warning("读长期画像失败：%s", e)

    log.info("计划完成：%d 个项目，%d 个要素组", len(names), len(plan))
    return {
        "projects": names,
        "plan": plan or {},
        "turn": 0,
        "phase": "OPENING",
        "ladder_level": 0,
        "ladder_name": "是什么",
        "current_project": names[0] if names else "",
        "topic_counts": {},
        "asked_qids": [],
        "recent_qa": [],
        "done": False,
        "profile_digest": digest,
    }


def node_probe(state: InterviewState) -> dict:
    """出题节点：RAG 检索 + 选要素 + 让模型措辞。

    三步：
    1. **RAG**：用最近对话查简历，命中后取 parent_text（整项目上下文）
    2. **选要素**：PROJECT_PROBE 阶段按木梯走；TECH_QA 阶段用语义选题
    3. **出题**：调旧 Interviewer（它已含清洗、去重、不变量、兜底）
    """
    from app.core import bridge, registry

    bridge.ensure_legacy_importable()

    phase = state.get("phase", "OPENING")
    resume_ctx = ""

    # ---- ⓪ 取运行时（出题和选题都要用）----
    #
    # 踩过的坑：改 TECH_QA 选题逻辑时把 `session = ...` 这行删掉了，
    # 结果后面 `session.next_question()` 抛 NameError，被 except 吞掉 →
    # 走降级分支"（出题降级）请讲讲…" → **模型一次都没调** →
    # 重启后的 4 轮完全不记账。
    #
    # 这个 bug 的表象是"成本统计不对"，真因是"出题降级了"。
    # 所以下面把取运行时放到最前面，并在失败时明确报错。
    from app.core import recover
    session = recover.ensure_runtime(state.get("session_id", ""), state)

    # ---- ① RAG：查简历，拿整项目上下文 ----
    try:
        from app.rag import retriever
        query = _build_rag_query(state)
        if query:
            hits = retriever.search_resume(query, owner=state.get("user", ""), k=3)
            resume_ctx = retriever.build_context(hits, max_chars=1500)
            if resume_ctx:
                log.info("RAG 命中 %d 条简历片段（query=%.30s）", len(hits), query)
    except Exception as e:  # noqa: BLE001
        log.warning("简历检索失败（降级为无上下文）：%s", e)

    # ---- ② TECH_QA 阶段：语义选题 ----
    #
    # ## 为什么不能用 _build_rag_query() 当查询（踩过的坑）
    #
    # 第一版用 `_build_rag_query(state)`（拼的是"当前项目 + 最近回答"）去查题库，
    # 实测跑偏：候选人在讲"工单分类准确率 0.86"，检索却召回了
    # "Transformer 注意力里缩放点积" —— 语义空间里"分类模型"和"注意力机制"
    # 确实接近，但这不是面试官该问的东西。
    #
    # 根因：**话题应该由状态机给，不能由候选人上一句话给。**
    # 旧代码 `session.py` 里有明确注释：
    #   "注意：不要把候选人原文当 topic 传进去。第一版这么写，导致技术问答的
    #    话题变成'候选人上一句的整段回答'，提问官于是围绕那段话出题，
    #    知识库形同虚设。话题必须由状态机给。"
    #
    # 所以现在的查询 = **状态机指定的话题**（`sm.tech.current`），
    # 只有在状态机没给话题时才退到项目名。
    bank_qid = ""
    if phase == "TECH_QA":
        try:
            from app.rag import bank_select

            topic = _tech_topic_of(state)
            query = topic or state.get("current_project", "")
            if query:
                picked = bank_select.pick(
                    query=query,
                    role=state.get("role", "ai_algo"),
                    asked_qids=state.get("asked_qids", []),
                    turn=state.get("turn", 0),
                    topic_counts=state.get("topic_counts", {}),
                    total_turns=state.get("max_turns", 16),
                    user=state.get("user", ""),
                )
                if picked:
                    bank_qid = picked.qid
                    log.info("题库选题：%s（查询=%s，%s）",
                             picked.qid, query[:20], picked.reason)
        except Exception as e:  # noqa: BLE001
            log.warning("题库选题失败：%s", e)

    # ---- ③ 出题：复用旧 Interviewer ----
    # 这里不重写措辞逻辑，因为它已经包含：清洗前缀/引号、只留第一问、
    # 复读检测（Jaccard 0.62）、不变量拦截、失败兜底。
    try:
        from app.core import usage
        with usage.bind(state.get("session_id", "")):
            question = session.next_question(
                resume_context=resume_ctx,
                bank_qid=bank_qid,
            )
    except Exception as e:  # noqa: BLE001
        # 出题失败要**响亮**地记日志：它会连带导致"模型没被调用 → 成本统计缺失"
        log.exception("出题失败（模型未被调用，成本统计会缺这一轮）")
        proj = state.get("current_project", "这个项目")
        question = f"（出题降级）请讲讲你在{proj}里做了什么。"

    # 出题也记一次账（模型被调用过一次）
    _record_from_logger(state, state.get("session_id", ""))

    return {
        "pending_question": question,
        "resume_context": resume_ctx,
        "bank_question_id": bank_qid,
    }


def node_evaluate(state: InterviewState) -> dict:
    """评分节点：调模型 → 阈值规整 → 不变量。全部复用旧实现。"""
    from app.core import usage

    answer = state.get("last_answer", "")
    question = state.get("pending_question", "")
    sid = state.get("session_id", "")

    try:
        from app.core import recover
        session = recover.ensure_runtime(sid, state)

        with usage.bind(sid):
            ev = session.evaluate_answer(question, answer,
                                        element=state.get("pending_element", ""))
    except Exception as e:  # noqa: BLE001
        log.exception("评分失败，降级")
        ev = {"verdict": "partial", "avg": 2.5, "evidence": "",
              "degraded": True, "error": str(e)}

    # 记账：从 LLMLogger 的 last_usage 拿（这是在调用路径之外做的，
    # 不会拖慢模型调用——详见 usage.RecordingLlm 的注释）
    _record_from_logger(state, sid)

    # 记录问答到 RAG 历史（供跨会话记忆 + 报告雷达图）
    try:
        _persist_turn(state, question, answer, ev)
    except Exception as e:  # noqa: BLE001
        log.warning("写入 turns 表失败：%s", e)

    # 评估模式的点评卡（没启用就是 None）
    #
    # 注意：`session` 只在 try 块里赋值。如果评分失败走了 except 分支，
    # 这里会 NameError → 整个节点炸掉 → 图运行中断。
    # 用 globals 兜底不安全，所以显式判空。
    card = getattr(session, "_last_card", None) if "session" in dir() else None

    return {"last_eval": ev, "last_card": card}


def _record_from_logger(state: InterviewState, sid: str) -> None:
    """从运行时持有的 LLM logger 里读上一次调用的 usage 并记账。

    ## 为什么放在调用之后而不是包装 llm

    第一版把记账塞进 `RecordingLlm.chat()`，每次调用后同步读 logger。
    但旧 `LLMLogger.write()` 会落盘 + fsync，导致**单轮从 5 秒涨到 19 秒**。

    现在改成：调用完成后，在图节点里集中读一次 logger 并记账。
    读取发生在调用路径**之外**，不影响模型响应时间。

    ## 踩过的坑：必须在这里 bind 上下文

    `usage.record()` 第一行就读 `current_session()`，没绑定就**直接 return**。
    我在这个函数里忘了 `with usage.bind(sid)`，结果记账全部静默失败——
    表现是"模型调了、日志里有 usage、但 sessions 表 tokens 一直 0"。
    """
    try:
        from app.core import usage
        if not sid:
            return

        from app.core import recover
        from app.core.runtime import SessionRuntime

        rt = recover.ensure_runtime(sid, state)
        session = getattr(rt, "_session", None)
        llm = SessionRuntime._unwrap_llm(getattr(session, "llm", None))
        logger = getattr(llm, "logger", None) if llm else None
        last = getattr(logger, "last_usage", None) if logger else None

        with usage.bind(sid):          # ← 关键：没有这行 record() 会直接丢弃
            if isinstance(last, dict) and last.get("ok", True) and (
                    last.get("prompt_tokens") or last.get("completion_tokens")):
                usage.record(
                    int(last.get("prompt_tokens") or 0),
                    int(last.get("completion_tokens") or 0),
                    str(last.get("model") or ""),
                )
            else:
                # 拿不到 usage → 退化估算（按答案长度，标注 estimated）
                ans = str(state.get("last_answer") or "")
                q = str(state.get("pending_question") or "")
                est_out = max(40, int(len(ans) * 1.2)) if ans else 40
                est_in = max(60, int(len(q) * 1.2)) if q else 60
                usage.record(est_in, est_out, "estimated")
    except Exception as e:  # noqa: BLE001
        log.debug("记账失败（不影响主流程）：%s", e)


def node_route(state: InterviewState) -> dict:
    """路由节点：纯规则决定下一步。不调模型。

    这是整个设计的关键：**判定与推进都不依赖模型**。
    即使模型评分挂了，路由仍能按现有信息升木梯/换阶段/收尾。
    """
    from app.core import bridge

    bridge.ensure_legacy_importable()

    turn = state.get("turn", 0) + 1
    max_turns = state.get("max_turns", 16)
    phase = state.get("phase", "OPENING")

    # 累加话题计数（TECH_QA 阶段按题库 topic 记）
    topic_counts = dict(state.get("topic_counts", {}))
    if phase == "TECH_QA":
        # 从最近一次评分或备忘里拿 topic
        pass

    # 阶段推进：复用旧状态机的规则
    try:
        from app.core import registry
        from app.core import recover
        session = recover.ensure_runtime(state.get("session_id", ""), state)
        new_phase, new_ladder, new_ladder_name = session.advance(
            last_eval=state.get("last_eval") or {}
        )
    except Exception as e:  # noqa: BLE001
        log.warning("状态机推进失败（保持当前阶段）：%s", e)
        new_phase, new_ladder, new_ladder_name = phase, state.get("ladder_level", 0), state.get("ladder_name", "是什么")

    done = new_phase == "FEEDBACK" or turn >= max_turns

    return {
        "turn": turn,
        "phase": new_phase,
        "ladder_level": new_ladder,
        "ladder_name": new_ladder_name,
        "topic_counts": topic_counts,
        "done": done,
    }


def node_report(state: InterviewState) -> dict:
    """收尾节点：生成报告。

    ## 为什么不在这里调旧 session 的 finish()

    踩过的坑（第三次）：这里原本调 `rt.build_report()`（内部走旧
    `session.finish()`），而那个 session 实例是**从 PG 恢复重建**的，
    内部 `turns` 列表是空的 → 旧实现调 LLM 生成一份"0 轮、均分 0.0、
    没有原话"的报告 → 写进 reports 表 → 用户看到的就是这份错报告。

    真实数据在 **PG 的 turns 表**里（每轮都写穿）。所以这里直接用 turns
    生成结构化报告，旧 session 的内存状态一律不采信。

    报告生成逻辑统一在 `core/service._report_from_turns`，
    这里只负责触发 + 写长期记忆。
    """
    from app.core import registry
    from app.core.service import _report_from_turns
    from app.rag.memory import _load_turns, update_profile

    sid = state.get("session_id", "")

    # ① 权威数据源：PG 的 turns
    turns: list[dict] = []
    try:
        turns = _load_turns(sid)
    except Exception as e:  # noqa: BLE001
        log.warning("读 turns 失败：%s", e)

    if turns:
        report = _report_from_turns(turns)
        log.info("报告已从 %d 轮 turns 生成（%d 字符）",
                 len(turns), len(report.get("markdown") or ""))
    else:
        # ② 没有 turns（第一轮就崩）→ 退到运行时
        report = {"markdown": "（本场没有产生问答记录）", "dims": {}}
        try:
            from app.core import recover
            rt = recover.ensure_runtime(sid, state)
            r = rt.build_report()
            if r and r.get("markdown"):
                report = r
        except Exception:  # noqa: BLE001
            pass

    # 长期记忆：把本场薄弱点累加到 candidate_profile
    try:
        update_profile(state.get("user", ""), state)
    except Exception as e:  # noqa: BLE001
        log.warning("长期记忆写入失败：%s", e)

    return {"report": report, "finish_reason": "completed"}


# ============================================================================
# 辅助
# ============================================================================

def _build_rag_query(state: InterviewState) -> str:
    """拼简历检索查询：当前项目 + 最近两轮问答。

    只用于**简历检索**（找"该问这个项目的哪个细节"）。
    不要用它查题库——题库的话题必须由状态机指定，
    否则会出现"讲工单分类却问注意力机制"这种跑偏（见 node_probe 的注释）。
    """
    parts: list[str] = []
    if state.get("current_project"):
        parts.append(state["current_project"])
    for qa in (state.get("recent_qa") or [])[-2:]:
        a = str(qa.get("a") or "")[:120]
        if a:
            parts.append(a)
    return " ".join(parts).strip()


def _tech_topic_of(state: InterviewState) -> str:
    """取状态机当前指定的技术话题。

    三个来源，按可靠性：
    1. 状态里显式存的 `current_topic`（图自己维护）
    2. 旧状态机的 `sm.tech.current`（题库选题的产物）
    3. 都没有 → 返回空串（调用方退到项目名）
    """
    explicit = str(state.get("current_topic") or "")
    if explicit:
        return explicit

    try:
        from app.core import recover
        rt = recover.ensure_runtime(state.get("session_id", ""), state)
        sm = getattr(getattr(rt, "_session", None), "sm", None)
        tech = getattr(sm, "tech", None) if sm else None
        cur = getattr(tech, "current", None) if tech else None
        if isinstance(cur, dict):
            return str(cur.get("topic") or cur.get("name") or "")
        if isinstance(cur, str):
            return cur
    except Exception:  # noqa: BLE001
        pass
    return ""


def _persist_turn(state: InterviewState, question: str, answer: str, ev: dict) -> None:
    """把这一轮写进 turns 表（含向量，供跨会话记忆检索）。

    turns 表对 sessions 有外键约束，所以先确保 sessions 行存在。
    踩过的坑：调试时直接建 runtime 跑图，没走 service.start 的落库，
    结果 INSERT turns 报 `violates foreign key constraint`，
    整轮评分因此静默降级（turn 永远 0）。
    """
    from app.repos.pg import get_conn

    sid = state.get("session_id", "")
    if not sid:
        return

    seq = state.get("turn", 0) + 1
    text = f"问：{question}\n答：{answer}"

    vec = None
    try:
        from app.rag.embedder import get_embedder
        vec = get_embedder().embed([text])[0]
    except Exception:  # noqa: BLE001
        pass

    with get_conn() as c:
        cur = c.cursor()
        # 先保证父行存在（幂等）
        cur.execute(
            """INSERT INTO sessions (id, owner, role, phase, status, max_turns)
               VALUES (%s, %s, %s, %s, 'running', %s)
               ON CONFLICT (id) DO NOTHING""",
            (sid, state.get("user", ""), state.get("role", ""),
             state.get("phase", ""), int(state.get("max_turns", 16))),
        )
        # 五个维度分单独写列（雷达图要用）
        #
        # 为什么不留空：报告页的雷达图按维度聚合（AVG(accuracy) 等）。
        # 第一版只写了 score（=avg），没写维度，
        # 结果前端拿到的 dims 永远是空 {}，雷达图一片空白。
        cur.execute(
            """INSERT INTO turns
               (session_id, seq, question, answer, verdict, score, phase, element,
                degraded, accuracy, depth, tradeoff, boundary, clarity)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (session_id, seq) DO UPDATE
                 SET answer = EXCLUDED.answer, verdict = EXCLUDED.verdict,
                     score = EXCLUDED.score,
                     accuracy = EXCLUDED.accuracy, depth = EXCLUDED.depth,
                     tradeoff = EXCLUDED.tradeoff, boundary = EXCLUDED.boundary,
                     clarity = EXCLUDED.clarity""",
            (sid, seq, question, answer,
             ev.get("verdict", ""), float(ev.get("avg") or 0),
             state.get("phase", ""), state.get("pending_element", ""),
             bool(ev.get("degraded", False)),
             _dim(ev, "accuracy"), _dim(ev, "depth"), _dim(ev, "tradeoff"),
             _dim(ev, "boundary"), _dim(ev, "clarity")),
        )
        if vec:
            cur.execute(
                "UPDATE turns SET embedding = %s::vector WHERE session_id = %s AND seq = %s",
                (_vec_literal(vec), sid, seq),
            )
        cur.close()


def _dim(ev: dict, name: str) -> float | None:
    """从 eval 里取某个维度的分数；取不到返回 None（列可空）。"""
    v = ev.get(name)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


# ============================================================================
# 图构建
# ============================================================================

def build_graph(checkpointer: Any = None):
    """构建并编译状态图。

    Args:
        checkpointer: LangGraph 的 checkpointer（Postgres/内存）。
            传 None 则用内存版（测试用，进程退出即丢）。

    ## 图的结构（条件入口是关键）

    ```
    START ─┬─(无 projects)──→ plan ──→ probe ──→ END      ← 开场第一问
           └─(有 projects)──→ evaluate → route ─┬→ probe → END   ← 后续每轮
                                                 └→ report → END  ← 收尾
    ```

    为什么要条件入口：
    第一版把 `plan` 固定成入口，结果每次 `invoke` 都重跑 plan，
    `turn` 永远被重置成 0、`pending_question` 永远是第一问——
    实测"答三轮还是第 0 轮、问题一字不变"。

    现在改成：有 projects 说明已经开过场，直接从 evaluate 进，
    这样每次 invoke 正好处理一轮（评分→路由→出下一问）。

    `probe` 之后直接 END 也是有意的：一次 invoke 只产出一个问题，
    等候选人回答后再 invoke 一次。这样"暂停/恢复"的边界才清晰。
    """
    from langgraph.graph import END, StateGraph

    g = StateGraph(InterviewState)

    g.add_node("plan", node_plan)
    g.add_node("probe", node_probe)
    g.add_node("evaluate", node_evaluate)
    g.add_node("route", node_route)
    g.add_node("report", node_report)

    # 条件入口：没计划就跑 plan，有计划就直接评分
    g.set_conditional_entry_point(
        _entry_point,
        {"first": "plan", "next": "evaluate"},
    )

    g.add_edge("plan", "probe")
    # 第一问产出后停下（等候选人回答）
    g.add_edge("probe", END)

    g.add_edge("evaluate", "route")
    g.add_conditional_edges(
        "route",
        _should_continue,
        {"continue": "probe", "done": "report"},
    )
    g.add_edge("report", END)

    return g.compile(checkpointer=checkpointer)


def _entry_point(state: InterviewState) -> str:
    """首轮跑 plan，后续直接进 evaluate。"""
    return "first" if not state.get("projects") else "next"


def _should_continue(state: InterviewState) -> str:
    return "done" if state.get("done") else "continue"

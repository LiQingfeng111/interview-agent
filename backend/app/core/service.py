"""会话服务：图 + 注册表 + 持久化的编排层。

## 三层职责

```
API 层（api/sessions.py）  →  只管 HTTP、校验、错误码
服务层（本文件）            →  编排：建会话、跑一轮、恢复、收尾
图（graph/interview_graph） →  一轮内部的：检索→出题→评分→路由
```

## 一轮的数据流

```
POST /answer {answer}
  ↓
service.answer(sid, answer)
  ↓
_load_state(sid)           从 checkpointer 读当前状态（进程重启也能读到）
  ↓
_run_until_question()      invoke 图：evaluate → route → probe
  ↓
返回 {question, eval, progress}
```

## 为什么状态从 checkpointer 读而不是内存字典

内存字典在进程重启后是空的。checkpointer（PostgresSaver）把图状态存在 PG 里，
重启后 `graph.get_state(config)` 仍能拿到 turn/phase/projects 等。
这是"重启后能继续答"的基础。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core import errors, registry
from app.core.runtime import SessionRuntime
from app.graph import interview_graph
from app.schemas import StartRequest

log = logging.getLogger("interview.service")

#: LangGraph 编译后的图（单例，线程安全）
_GRAPH: Any = None

#: 内存状态缓存（checkpointer 不可用时的兜底）
_STATES: dict[str, dict] = {}


def get_graph() -> Any:
    """懒加载编译图，用 Postgres checkpointer（进程重启可恢复）。

    降级链：PostgresSaver → MemorySaver → 无 checkpointer。
    每一级都能用，只是持久性递减。
    """
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH

    from app.graph import checkpointer

    saver = checkpointer.get_saver()
    _GRAPH = interview_graph.build_graph(checkpointer=saver)
    log.info("状态图已编译（checkpointer=%s）", checkpointer.kind())
    return _GRAPH


# ============================================================================
# 开会话
# ============================================================================

def start(req: StartRequest, user: str) -> dict[str, Any]:
    """开一场面试：建运行时 → 跑 plan+probe → 返回第一问。"""
    sid = "s_" + uuid.uuid4().hex[:16]

    # 简历来源：优先已上传文档
    resume_text = req.resume or ""
    if req.document_id:
        # 注意：这里**不能**用宽泛的 except 吞掉 ImportError。
        #
        # 踩过的坑：`app.repos.documents` 模块当时不存在，import 失败被 except 吞掉，
        # 只打一行 warning，然后 resume_text 保持空 → 抛「简历里没识别到项目」。
        # 用户看到的现象是"上传成功了但点开始面试报错"，和真实的"模块缺失"完全对不上。
        #
        # 现在的处理：ImportError 直接抛（Fail Fast），其他异常才降级到 req.resume。
        from app.repos.documents import load_document_text  # noqa: PLC0415

        try:
            doc = load_document_text(req.document_id, user)
        except Exception as e:  # noqa: BLE001
            log.exception("读文档异常 doc_id=%s", req.document_id)
            doc = None
        if doc:
            resume_text = doc
        else:
            log.warning(
                "document_id=%s 读不出文本，回退到请求体里的 resume（%d 字）",
                req.document_id, len(req.resume or ""),
            )

    if not resume_text.strip():
        raise errors.resume_no_project()

    # 构造运行时（内部会建旧的 InterviewSession）
    try:
        rt = SessionRuntime(sid, user, req)
    except Exception as e:  # noqa: BLE001
        log.exception("构造运行时会话失败")
        raise errors.model_unreachable(str(e)) from e

    registry.register(sid, rt)

    # 落库（会话元信息，turns 表有外键依赖它）
    _persist_session_start(sid, user, req)

    initial: dict[str, Any] = {
        "session_id": sid,
        "user": user,
        "role": req.role,
        "persona": req.persona,
        "resume_text": resume_text,
        "jd_text": req.jd or "",
        "max_turns": req.max_turns,
        "mock": req.mock,
        "web": req.web,
        "coach": req.coach,
    }
    state = _run_until_question(sid, initial)

    return {
        "session_id": sid,
        "turn": 0,
        "question": state.get("pending_question", ""),
        "element": state.get("pending_element", ""),
        "phase": state.get("phase", "OPENING"),
        "projects": state.get("projects", []),
        "progress": _progress(state),
        "eval": None,
        "done": False,
        "engine": {
            "graph": "langgraph",
            "model": _model_name(),
            "mock": req.mock,
            "web": req.web,
            "coach": req.coach,
        },
    }


# ============================================================================
# 答一轮
# ============================================================================

def answer(sid: str, answer_text: str, user: str) -> dict[str, Any]:
    """交一轮答案：从断点恢复图 → 评分 → 下一问。"""
    if not (answer_text or "").strip():
        raise errors.answer_empty()

    _require_runtime(sid, user)

    prev = _load_state(sid)
    if prev.get("done"):
        raise errors.session_finished()

    patch: dict[str, Any] = {
        "session_id": sid,
        "user": user,
        "last_answer": answer_text,
        "pending_element": prev.get("pending_element", ""),
    }
    state = _run_until_question(sid, patch)

    ev = state.get("last_eval") or {}
    done = bool(state.get("done"))

    out: dict[str, Any] = {
        "session_id": sid,
        "turn": int(state.get("turn", 0)),
        "question": "" if done else state.get("pending_question", ""),
        "element": state.get("pending_element", ""),
        "phase": state.get("phase", ""),
        "eval": _eval_view(ev),
        "progress": _progress(state),
        "done": done,
        "card": state.get("last_card"),
    }
    if done:
        # 报告**总是**用 _build_report_now 重新生成，不信任 state 里的。
        #
        # 踩过的坑（两次）：
        # ① 第一版只读 state.get("report")：图里 report 节点因 registry 已注销
        #    而恢复失败 → report={"error":...} → 被 `or {}` 吞成空。
        # ② 第二版加了 fallback `if not report.get("markdown")`：但图确实
        #    产出了一份 markdown（只是内容错，说"0 轮、均分 0.0"），
        #    条件不成立 → 跳过重建 → 用户看到的是错报告。
        #
        # 教训：判断"报告是否可用"不能只看"有没有内容"，要看"内容对不对"。
        # 既然 _build_report_now 以 PG 的 turns 为权威（一定比内存状态准），
        # 就无条件用它。
        report = _build_report_now(sid, user)
        out["report"] = report
        registry.unregister(sid)
        _persist_session_finish(sid, "completed")
        _persist_report(sid, report)
        _trigger_memory(user, sid)
    return out


def _build_report_now(sid: str, user: str) -> dict[str, Any]:
    """生成报告。

    ## 数据来源的优先级（这条很重要）

    第一版先调 `rt.build_report()`（走旧 session.finish()），结果得到一份
    "均分 0.0、轮次 0、没有原话"的空报告 —— 因为那个 session 实例是
    **从 PG 恢复重建**的，内部 `turns` 列表是空的。

    而真正的问答记录在 PG 的 `turns` 表里（每轮都写穿了）。

    所以正确顺序是：
    **先看 PG 的 turns（权威），够了就用它生成；不够再问运行时。**

    这也解释了为什么"报告说没证据"但"接口返回了 6 轮评分"——
    两个数据源不一致，取了错的那个。
    """
    # ① 权威来源：PG 的 turns
    try:
        from app.rag.memory import _load_turns
        turns = _load_turns(sid)
    except Exception:  # noqa: BLE001
        turns = []

    if len(turns) >= 1:
        # 有 turns 就以它为权威，**不再用运行时覆盖**。
        #
        # 踩过的坑：第一版在这里"再问一下运行时，谁长用谁"，
        # 结果运行时的报告（基于恢复重建的空 session）说"0 轮、均分 0.0"，
        # 而 turns 里明明有 6 轮记录。两个数据源冲突时，
        # 应该信**写穿到库里的**，不是信内存状态。
        report = _report_from_turns(turns)

        # 运行时只能**补充**（要素覆盖等 turns 里没有的信息），不能替换主体
        try:
            rt = registry.get_runtime(sid)
            extra = rt.build_report()
            if extra:
                if extra.get("dims") and not report.get("dims"):
                    report["dims"] = extra["dims"]
                for k in ("element_coverage", "invariants", "finish_reason"):
                    if extra.get(k) and not report.get(k):
                        report[k] = extra[k]
        except Exception:  # noqa: BLE001
            pass

        report = _attach_cost(report, sid)
        return report

    # ② 兜底：运行时（可能压根没写过 turns，比如第一轮就崩了）
    try:
        rt = registry.get_runtime(sid)
        r = rt.build_report()
        if r and r.get("markdown"):
            return r
    except KeyError:
        pass
    except Exception as e:  # noqa: BLE001
        log.warning("运行时生成报告失败：%s", e)

    if turns:
        return _report_from_turns(turns)

    return {"markdown": "（本场没有产生问答记录）", "dims": {}}


def _attach_cost(report: dict[str, Any], sid: str) -> dict[str, Any]:
    """把本场用量挂到报告上。

    两个来源，优先用 RecordingLlm 的实时记账（按 session 归集，不会串账），
    没有再退回"读旧日志"（时间窗方案，并发会混）。

    为什么这么排：实测发现读日志方案拿不到数据（旧 LLMLogger 默认没启用），
    而实时记账是调用返回值的直接统计，一定准。
    """
    try:
        from app.core import usage
        u = usage.snapshot(sid)
    except Exception as e:  # noqa: BLE001
        log.warning("读 usage 快照失败：%s", e)
        u = {}

    if not u or not u.get("calls"):
        # 退回日志方案
        try:
            from app.core import cost as cost_mod
            created = _session_created_at(sid)
            if created:
                return cost_mod.attach_to_report(report, created)
        except Exception:  # noqa: BLE001
            pass
        return report

    report["cost"] = u
    md = str(report.get("markdown") or "")
    if md:
        tokens = u["tokens"]
        calls = u["calls"]
        money = u["cost_cny"]
        report["markdown"] = (
            md + "\n\n---\n本场用量：" + str(tokens) + " token"
            + "（" + str(calls) + " 次模型调用），约 " + f"{money:.4f}" + " 元"
        )
    return report


def _report_from_turns(turns: list[dict]) -> dict[str, Any]:
    """从 PG 的 turns 记录生成结构化报告。

    为什么以 turns 为权威：它是每轮结束时写穿的，不依赖内存状态，
    进程重启、会话恢复都不影响。运行时的内部状态反而可能因恢复而残缺。
    """
    dims_acc: dict[str, list[float]] = {}
    verdict_count: dict[str, int] = {}
    lines: list[str] = []

    for i, t in enumerate(turns, 1):
        verdict = str(t.get("verdict") or "")
        score = float(t.get("score") or 0)
        verdict_count[verdict] = verdict_count.get(verdict, 0) + 1

        q = str(t.get("question") or "").strip()
        a = str(t.get("answer") or "").strip()
        phase = str(t.get("phase") or "")

        lines.append(f"## 第 {i} 轮 · {verdict or '未评分'}（{score:.1f} 分）")
        if phase:
            lines.append(f"阶段：{phase}")
        if q:
            lines.append(f"**问**：{q[:200]}")
        if a:
            lines.append(f"**答**：{a[:300]}")
        lines.append("")

        # dims 存在 turns 表里没有单独列，从 score 反推不了维度；
        # 这里只在有的情况（未来加字段）才统计
        d = t.get("dims")
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, (int, float)):
                    dims_acc.setdefault(k, []).append(float(v))

    scores = [float(t.get("score") or 0) for t in turns if t.get("score")]
    avg = round(sum(scores) / len(scores), 2) if scores else 0.0

    weakest_rounds = sorted(
        [t for t in turns if t.get("verdict") == "fail"],
        key=lambda x: float(x.get("score") or 0),
    )
    strongest = sorted(
        [t for t in turns if t.get("verdict") == "pass"],
        key=lambda x: -float(x.get("score") or 0),
    )

    header = [
        "# 面试报告",
        "",
        f"- 总轮次：{len(turns)}",
        f"- 平均分：{avg}",
        "- 结论分布：" + "、".join(
            f"{k or '未评'} {v} 轮" for k, v in verdict_count.items()),
        "",
        "## 表现概览",
    ]

    if strongest:
        header.append(f"**答得最好的**：第 {turns.index(strongest[0]) + 1} 轮"
                      f"（{float(strongest[0].get('score') or 0):.1f} 分）"
                      f" —— {str(strongest[0].get('question') or '')[:60]}")
    if weakest_rounds:
        header.append(f"**最需要复盘的**：第 {turns.index(weakest_rounds[0]) + 1} 轮"
                      f"（{float(weakest_rounds[0].get('score') or 0):.1f} 分）"
                      f" —— {str(weakest_rounds[0].get('question') or '')[:60]}")
    header.append("")
    header.append("## 逐轮记录")

    return {
        "markdown": "\n".join(header + lines),
        "dims": {k: round(sum(v) / len(v), 2) for k, v in dims_acc.items()},
        "avg": avg,
        "turns": len(turns),
        "verdict_counts": verdict_count,
        "rebuilt_from_db": True,
    }


def _session_created_at(sid: str) -> str:
    """查会话创建时间（成本统计的时间窗起点）。"""
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute("SELECT created_at FROM sessions WHERE id = %s", (sid,))
            row = cur.fetchone()
            cur.close()
        if row and row[0]:
            return row[0].isoformat()
    except Exception:  # noqa: BLE001
        pass
    return ""


# ============================================================================
# 结束 / 报告
# ============================================================================

def finish(sid: str, reason: str, user: str) -> dict[str, Any]:
    """提前结束：生成报告，注销运行时。"""
    _require_runtime(sid, user)

    # 先算报告（此时运行时还在），再注销
    report = _build_report_now(sid, user)
    if not report or not report.get("markdown"):
        report = {"markdown": "（报告生成失败，请查看历史记录）", "dims": {}}

    registry.unregister(sid)
    _persist_session_finish(sid, reason)
    _persist_report(sid, report)
    _trigger_memory(user, sid)

    return {"session_id": sid, "done": True, "finish_reason": reason,
            "report": report}


def report(sid: str, user: str) -> dict[str, Any]:
    """取报告。

    ## 为什么"从 turns 重建"是默认路径，而不是从快照读

    踩过的坑：快照（reports 表）里可能存着**早期版本生成的错报告**。
    我在改完 `_report_from_turns` 之后，API 仍返回"0 轮、均分 0.0"的旧内容——
    因为 `report()` 优先读快照，而快照是前几天用旧逻辑写的。

    现在的策略：
    1. 只要 turns 有数据，就用 `_build_report_now()` 重新生成（**权威**）
    2. 只有在 turns 为空时，才退回快照
    3. 生成后覆盖快照（自愈）

    代价：每次取报告要查一次 turns 表（约 5ms），但换来"报告永远与数据一致"。
    """
    # ① 权威：从 turns 重建
    try:
        from app.rag.memory import _load_turns
        turns = _load_turns(sid)
    except Exception:  # noqa: BLE001
        turns = []

    if turns:
        r = _build_report_now(sid, user)
        if r and r.get("markdown"):
            _persist_report(sid, r)     # 覆盖旧快照（自愈）
            return {"session_id": sid, **r}

    # ② 兜底：快照（turns 被清理过或早期数据）
    try:
        import json

        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT r.markdown, r.structured, s.owner FROM reports r "
                "JOIN sessions s ON s.id = r.session_id WHERE r.session_id = %s",
                (sid,),
            )
            row = cur.fetchone()
            cur.close()
        if row and row[0]:
            owner = row[2]
            if owner != user:
                from app.auth.users import get_user
                u = get_user(user)
                if not u or u.get("role") != "admin":
                    raise errors.session_forbidden()
            structured = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
            return {"session_id": sid, "report": row[0], "markdown": row[0],
                    "dims": structured.get("dims", {}), **structured}
    except errors.AppError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("读报告快照失败：%s", e)

    raise errors.session_not_found()


def state_of(sid: str, user: str) -> dict[str, Any]:
    _require_runtime(sid, user)
    st = _load_state(sid)
    return {"session_id": sid, "progress": _progress(st)}


def pending_question(sid: str) -> str:
    """当前待回答的问题（SSE 用）。"""
    return str(_load_state(sid).get("pending_question", "") or "")


# ============================================================================
# 内部
# ============================================================================

def _require_runtime(sid: str, user: str) -> SessionRuntime:
    """取运行时，顺带做归属校验。"""
    if not sid:
        raise errors.session_missing_id()
    try:
        rt = registry.get_runtime(sid)
    except KeyError:
        from app.core import recover
        try:
            rt = recover.ensure_runtime(sid, _load_state(sid))
        except KeyError:
            raise errors.session_not_found() from None
    if getattr(rt, "user", "") not in ("", user):
        try:
            from app.auth.users import get_user
            u = get_user(user)
            if not u or u.get("role") != "admin":
                raise errors.session_forbidden()
        except errors.AppError:
            raise
        except Exception:  # noqa: BLE001
            raise errors.session_forbidden() from None
    return rt


def _load_state(sid: str) -> dict:
    """取图状态。

    优先从 checkpointer 读（进程重启也能拿到），内存缓存只作快速路径。
    这是"重启后能继续答"的关键——只靠内存字典的话重启就丢。
    """
    try:
        graph = get_graph()
        config = {"configurable": {"thread_id": sid}}
        snapshot = graph.get_state(config)
        if snapshot and getattr(snapshot, "values", None):
            values = dict(snapshot.values)
            _STATES[sid] = values
            return values
    except Exception as e:  # noqa: BLE001
        log.debug("从 checkpointer 读状态失败（用内存缓存）：%s", e)

    return _STATES.get(sid, {})


def _save_state(sid: str, state: dict) -> None:
    _STATES[sid] = state


def _run_until_question(sid: str, patch: dict[str, Any]) -> dict[str, Any]:
    """跑图，直到产出 pending_question 或 done。"""
    graph = get_graph()
    prev = _load_state(sid)
    merged = {**prev, **patch}

    try:
        config = {"configurable": {"thread_id": sid}}
        result = graph.invoke(merged, config=config)
        if not isinstance(result, dict):
            result = merged
    except Exception as e:  # noqa: BLE001
        log.exception("图执行失败，降级为直接调用运行时")
        result = _fallback_round(sid, merged, e)

    _save_state(sid, result)
    return result


def _fallback_round(sid: str, state: dict, err: Exception) -> dict:
    """图挂了时的降级：直接用运行时的方法跑一轮。

    为什么要有这条路：图是新引入的复杂度，
    但它不该成为"整个面试不可用"的单点。
    """
    try:
        rt = registry.get_runtime(sid)
    except KeyError:
        return {**state, "error": str(err)}

    try:
        q = rt.next_question(resume_context=state.get("resume_context", ""))
        return {**state, "pending_question": q, "degraded": True}
    except Exception:  # noqa: BLE001
        return {**state, "pending_question": "（系统繁忙）请继续讲你的项目。",
                "degraded": True, "error": str(err)}


def _trigger_memory(user: str, sid: str) -> None:
    """异步更新长期记忆（不阻塞响应）。"""
    try:
        from app.rag.memory import update_profile_async
        st = _load_state(sid)
        update_profile_async(user, {**st, "session_id": sid})
    except Exception as e:  # noqa: BLE001
        log.warning("触发长期记忆更新失败：%s", e)


def _progress(state: dict) -> dict:
    return {
        "turn": int(state.get("turn", 0)),
        "max_turns": int(state.get("max_turns", 16)),
        "phase": state.get("phase", ""),
        "ladder_level": int(state.get("ladder_level", 0)),
        "ladder_name": state.get("ladder_name", ""),
        "current_project": state.get("current_project", ""),
        "topic_counts": state.get("topic_counts", {}),
    }


def _eval_view(ev: dict) -> dict:
    """把内部 eval dict 转成前端视图。

    ## 为什么把五维**同时**放两处（`dims` 和顶层字段）

    - `dims`：前端雷达图按对象遍历，这个结构最方便
    - 顶层字段：方便直接读 `eval.accuracy`，也便于 CLI/脚本/日志

    之前只有 `dims`，导致外部调用方写 `ev.accuracy`
    拿到 undefined（前端和验证脚本都踩过）。
    冗余几个字段的成本远低于"字段找不到"的调试成本。

    另外把双轨评分的元信息也带出去：
    - `score_source`：每维来自模型还是规则（报告里要展示）
    - `rule_signals`：规则侧看到的信号（数字个数、边界词等）
    - `repeated` / `consistent`：上下文判断结果
    """
    if not ev:
        return {}
    dims = {k: float(ev.get(k, 0)) for k in
            ("accuracy", "depth", "tradeoff", "boundary", "clarity") if k in ev}
    avg = ev.get("avg")
    if avg is None and dims:
        avg = sum(dims.values()) / len(dims)

    view = {
        "verdict": str(ev.get("verdict", "")),
        "avg": round(float(avg or 0), 2),
        "weakest": str(ev.get("weakest", "")),
        "evidence": str(ev.get("evidence", "")),
        "dims": dims,
        "model_verdict": str(ev.get("model_verdict", "")),
        "verdict_conflict": bool(ev.get("verdict_conflict", False)),
        "degraded": bool(ev.get("degraded", False)),
        "covered": list(ev.get("covered") or []),
        # 上下文信号
        "repeated": bool(ev.get("repeated", False)),
        "consistent": bool(ev.get("consistent", True)),
        # 双轨评分元信息
        "score_source": dict(ev.get("score_source") or {}),
        "rule_signals": dict(ev.get("rule_signals") or {}),
    }

    # 五维展开到顶层（方便 ev.accuracy 这种直接读法）
    view.update(dims)
    return view


def _model_name() -> str:
    try:
        from app.settings import settings
        return settings.llm_model
    except Exception:  # noqa: BLE001
        return ""


def _persist_session_start(sid: str, user: str, req: StartRequest) -> None:
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """INSERT INTO sessions (id, owner, role, persona, phase, status, max_turns)
                   VALUES (%s, %s, %s, %s, 'OPENING', 'running', %s)
                   ON CONFLICT (id) DO NOTHING""",
                (sid, user, req.role, req.persona, req.max_turns),
            )
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.warning("写 sessions 失败：%s", e)


def _persist_session_finish(sid: str, reason: str) -> None:
    try:
        from app.repos.pg import get_conn
        st = _load_state(sid)
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """UPDATE sessions SET status = 'completed', finish_reason = %s,
                          phase = %s, total_turns = %s, updated_at = now()
                   WHERE id = %s""",
                (reason, st.get("phase", "FEEDBACK"),
                 int(st.get("turn", 0)), sid),
            )
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.warning("更新 sessions 失败：%s", e)


def _persist_report(sid: str, report: dict) -> None:
    if not report:
        return
    try:
        import json

        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """INSERT INTO reports (session_id, markdown, structured)
                   VALUES (%s, %s, %s::jsonb)
                   ON CONFLICT (session_id) DO UPDATE
                     SET markdown = EXCLUDED.markdown,
                         structured = EXCLUDED.structured""",
                (sid, report.get("markdown", ""),
                 json.dumps({k: v for k, v in report.items() if k != "markdown"},
                            ensure_ascii=False)),
            )
            cur.close()
    except Exception as e:  # noqa: BLE001
        log.warning("写报告失败：%s", e)

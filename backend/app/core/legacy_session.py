"""旧会话适配器：把 `InterviewSession` 包成新 backend 能用的形态。

## 为什么需要一层适配

旧 `InterviewSession` 的接口是 CLI 形态：

```python
session = InterviewSession(...)
q = session.start()          # 返回第一个问题字符串
q = session.step(answer)     # 喂答案，返回下一个问题字符串
report = session.finish()    # 返回 Markdown 报告
```

新 backend 需要的是结构化数据：

```python
{"question": ..., "eval": {...}, "progress": {...}, "done": bool}
```

**不能直接改旧类**：356 条测试依赖它的返回值（字符串）。
所以加一层适配，把"字符串"翻译成"结构化"，
**读取不修改**——旧类一行不动。

## 关键约束：eval 从哪来

`step()` 只返回下一个问题，评分藏在 `session.turns[-1].evaluation` 里。
适配器要从那个内部结构里取出评分，映射成 API 的 `EvalView`。

这部分必须**防御性读取**：`turns` 可能为空、`evaluation` 可能是 None、
字段名可能改过。读不到就返回空 eval，不能让整个请求 500。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import bridge, errors
from app.settings import settings

log = logging.getLogger("interview.legacy_session")

#: 内存会话表。旧实现也是这么做的（SESSIONS 字典）。
#: 换 LangGraph 后会由 checkpointer 接管，这里只是过渡。
_SESSIONS: dict[str, Any] = {}
_OWNER: dict[str, str] = {}


def session_count() -> int:
    return len(_SESSIONS)


def _import_session_class():
    bridge.ensure_legacy_importable()
    return bridge.legacy_attr("session", "InterviewSession")


def _safe_get(obj: Any, *names: str, default: Any = None) -> Any:
    """按多个候选名取属性（旧代码字段名可能带下划线/驼峰两种写法）。"""
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return default


def _eval_to_view(turn: Any) -> dict[str, Any] | None:
    """把旧的 Evaluation 对象转成 API 的 eval 结构。

    防御性读取的理由：旧类内部结构可能变（它是 CLI 用的，不是 API 契约）。
    读不到就返回 None，前端显示"本轮无评分"，而不是整个请求失败。
    """
    if turn is None:
        return None
    ev = _safe_get(turn, "evaluation", "eval")
    if ev is None:
        return None

    d = ev.to_dict() if hasattr(ev, "to_dict") else (ev if isinstance(ev, dict) else {})
    if not d:
        return None

    dims = {k: float(d.get(k, 0)) for k in
            ("accuracy", "depth", "tradeoff", "boundary", "clarity") if k in d}

    avg = d.get("avg")
    if avg is None and dims:
        avg = sum(dims.values()) / len(dims)

    return {
        "verdict": str(d.get("verdict", "")),
        "avg": round(float(avg or 0), 2),
        "weakest": str(d.get("weakest", "")),
        "evidence": str(d.get("evidence", "")),
        "dims": dims,
        "model_verdict": str(d.get("model_verdict", "")),
        "verdict_conflict": bool(d.get("verdict_conflict", False)),
        "degraded": bool(d.get("degraded", False)),
        "covered": list(d.get("covered") or []),
    }


def _progress_of(session: Any) -> dict[str, Any]:
    """阶段、轮次、木梯层级、要素覆盖。"""
    sm = _safe_get(session, "sm", "state_machine")
    out: dict[str, Any] = {
        "turn": 0, "max_turns": settings.default_max_turns,
        "phase": "", "phase_desc": "", "ladder_level": 0, "ladder_name": "",
        "element_coverage": {},
    }
    if sm is None:
        return out

    out["turn"] = int(_safe_get(sm, "total_turns", "turn", default=0) or 0)
    out["max_turns"] = int(_safe_get(sm, "max_turns", default=settings.default_max_turns) or 16)
    out["phase"] = str(_safe_get(sm, "phase", default="") or "")

    cfg = _safe_get(sm, "phase_cfg")
    if isinstance(cfg, dict):
        out["phase_desc"] = str(cfg.get("desc", ""))

    probe = _safe_get(sm, "probe")
    if probe is not None:
        out["ladder_level"] = int(_safe_get(probe, "ladder_level", default=0) or 0)
        out["ladder_name"] = str(_safe_get(sm, "ladder_name", default="") or "")
        # 要素覆盖：探针树内部有每个要素的状态
        proj = _safe_get(probe, "current", default="") or ""
        tree = _safe_get(session, "tree")
        if tree is not None and hasattr(tree, "element_status"):
            try:
                out["element_coverage"] = dict(tree.element_status(proj))
            except Exception:  # noqa: BLE001
                pass
    return out


def create(req: Any, user: str) -> dict[str, Any]:
    """开一场面试，返回 {session_id, question, progress, projects, ...}。

    参数校验已经在 Pydantic 层做过（role / persona / max_turns），
    这里只管"造会话"。
    """
    InterviewSession = _import_session_class()

    # 简历来源：优先已上传的文档，其次请求体里的文本
    resume_text = req.resume or ""
    if req.document_id:
        from app.repos.documents import load_document_text
        doc = load_document_text(req.document_id, user)
        if doc:
            resume_text = doc

    if not resume_text.strip():
        raise errors.resume_no_project()

    kwargs: dict[str, Any] = {
        "resume": resume_text,
        "role": req.role or settings.default_role,
        "persona": req.persona,
        "max_turns": req.max_turns,
        "web": req.web,
        "mock": req.mock,
        "coach": req.coach,
    }
    if req.jd:
        kwargs["jd"] = req.jd

    try:
        session = InterviewSession(**kwargs)
    except TypeError as e:
        # 旧类签名可能不含某些参数（如 jd），逐步降级而不是直接失败
        log.warning("InterviewSession 不接受部分参数，降级重试：%s", e)
        for drop in ("jd", "coach", "web", "mock"):
            kwargs.pop(drop, None)
            try:
                session = InterviewSession(**kwargs)
                break
            except TypeError:
                continue
        else:
            raise

    # 旧实现里"简历没解析出项目"是在 start() 时抛的
    try:
        question = session.start()
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "项目" in msg or "resume" in msg.lower():
            raise errors.resume_no_project() from e
        raise

    sid = _safe_get(session, "sid", "session_id", default="") or ""
    if not sid:
        import uuid
        sid = "s_" + uuid.uuid4().hex[:16]
        try:
            session.sid = sid
        except Exception:  # noqa: BLE001
            pass

    _SESSIONS[sid] = session
    _OWNER[sid] = user

    return {
        "session_id": sid,
        "turn": 0,
        "question": str(question or ""),
        "element": str(_safe_get(session, "last_target", default="") or ""),
        "eval": None,
        "progress": _progress_of(session),
        "done": False,
        "projects": list(_safe_get(session, "projects", default=[]) or []),
        "engine": {"model": settings.llm_model, "mock": bool(req.mock),
                   "web": bool(req.web), "coach": bool(req.coach)},
    }


def get(sid: str, user: str) -> Any:
    """取会话，顺带做归属校验。

    三种失败要分开（见 docs/ERROR-DESIGN.md）：
    - 没有这个 id  → 404 链接不完整
    - 有这个 id 但被别人拥有 → 403
    - 内存里没有（重启）→ 404 已过期
    """
    if not sid:
        raise errors.session_missing_id()
    if not sid.startswith("s_") and not sid.startswith("aud_"):
        raise errors.session_bad_id(sid)
    session = _SESSIONS.get(sid)
    if session is None:
        raise errors.session_not_found()
    owner = _OWNER.get(sid, "")
    if owner and owner != user:
        raise errors.session_forbidden()
    return session


def answer(sid: str, answer_text: str, user: str) -> dict[str, Any]:
    """交一轮答案，返回评分 + 下一问。"""
    if not (answer_text or "").strip():
        raise errors.answer_empty()

    session = get(sid, user)

    # 已结束就不能再答
    sm = _safe_get(session, "sm")
    phase = str(_safe_get(sm, "phase", default="") or "")
    if phase == "FEEDBACK":
        raise errors.session_finished()

    turns_before = len(_safe_get(session, "turns", default=[]) or [])

    try:
        next_q = session.step(answer_text)
    except Exception as e:  # noqa: BLE001
        # 模型不可用不该让接口 500——旧 core 内部已有兜底，
        # 走到这里说明是更底层的问题，如实报
        log.exception("step 失败 sid=%s", sid)
        raise errors.model_unreachable(str(e)) from e

    turns = _safe_get(session, "turns", default=[]) or []
    last = turns[-1] if len(turns) > turns_before and turns else (turns[-1] if turns else None)
    ev = _eval_to_view(last)

    done = not next_q or str(_safe_get(sm, "phase", default="")) == "FEEDBACK"

    out: dict[str, Any] = {
        "session_id": sid,
        "turn": int(_safe_get(sm, "total_turns", default=len(turns)) or len(turns)),
        "question": "" if done else str(next_q or ""),
        "element": str(_safe_get(session, "last_target", default="") or ""),
        "eval": ev,
        "progress": _progress_of(session),
        "done": done,
    }
    # 评估模式的点评卡
    cards = _safe_get(session, "cards", default=[]) or []
    if cards:
        card = cards[-1]
        out["card"] = card.to_dict() if hasattr(card, "to_dict") else card
    return out


def finish(sid: str, reason: str, user: str) -> dict[str, Any]:
    """结束面试，返回报告。

    注意：这里**不再返回报告全文**（旧实现 /api/end 和 /api/report 返回同一份，
    见 docs/API-CONTRACT.md 问题 2）。只返回状态，报告走 GET /report。
    """
    session = get(sid, user)
    try:
        if hasattr(session, "end_early"):
            session.end_early(reason=reason)
        else:
            session.finish()
    except Exception as e:  # noqa: BLE001
        log.warning("结束会话时出错（忽略）sid=%s: %s", sid, e)

    return {"session_id": sid, "done": True, "finish_reason": reason}


def report(sid: str, user: str) -> dict[str, Any]:
    """取报告：Markdown 全文 + 分维度分数 + 画像。"""
    session = get(sid, user)
    text = ""
    try:
        text = str(session.finish() or "")
    except Exception as e:  # noqa: BLE001
        log.warning("生成报告出错 sid=%s: %s", sid, e)

    dims: dict[str, float] = {}
    turns = _safe_get(session, "turns", default=[]) or []
    acc: dict[str, list[float]] = {}
    for t in turns:
        ev = _safe_get(t, "evaluation")
        if ev is None:
            continue
        d = ev.to_dict() if hasattr(ev, "to_dict") else (ev if isinstance(ev, dict) else {})
        for k in ("accuracy", "depth", "tradeoff", "boundary", "clarity"):
            if k in d and isinstance(d[k], (int, float)):
                acc.setdefault(k, []).append(float(d[k]))
    for k, vs in acc.items():
        dims[k] = round(sum(vs) / len(vs), 2)

    return {"session_id": sid, "report": text, "dims": dims,
            "profile": _safe_get(session, "profile", default={}) or {}}


def drop(sid: str) -> None:
    _SESSIONS.pop(sid, None)
    _OWNER.pop(sid, None)

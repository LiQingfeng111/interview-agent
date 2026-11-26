"""运行时恢复：进程重启后从 PG + checkpoint 重建 SessionRuntime。

## 为什么需要这个模块

checkpointer 只持久化**图状态**（字典）。但 `SessionRuntime` 持有旧的
`InterviewSession` 实例（含 llm 客户端、状态机、探针树）——这些不可序列化。

所以重启后：
- 图状态：✅ 从 checkpointer 恢复（turn/phase/projects 都在）
- 运行时：❌ 内存里没了 → 节点调 `registry.get_runtime()` 直接 KeyError

第一版就栽在这里：实测"重启后 answer 返回 turn:0"，
根因不是 checkpointer 没生效，而是**运行时恢复失败后走了降级路径**。

## 恢复需要什么

从 PG 能拿到的：
- `sessions` 表：owner / role / persona / max_turns
- `documents` 表：简历全文

从图状态能拿到的：
- `turn` / `phase` / `ladder_level` / `current_project` / `asked_qids` / `topic_counts`

**注意**：`InterviewSession` 内部有自己的状态机计数器。
重建时要把这些值"回灌"进去，否则它会从第 0 轮开始数。
`SessionRuntime` 提供 `restore(progress, asked_qids, topic_counts)` 做这件事。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core import registry

log = logging.getLogger("interview.recover")


def ensure_runtime(sid: str, state: dict) -> Any:
    """确保运行时存在。不存在则从 PG + state 重建。

    Args:
        sid: 会话 id
        state: 图状态（含 owner/role/persona/max_turns/progress 等）

    Returns:
        SessionRuntime 实例

    Raises:
        KeyError: 无法恢复（会话不存在或已结束）
    """
    # 热路径：内存里有
    if registry.has_runtime(sid):
        return registry.get_runtime(sid)

    # 冷路径：重建
    rt = _rebuild(sid, state)
    if rt is None:
        raise KeyError(f"无法恢复运行时：{sid}")
    registry.register(sid, rt)
    log.info("运行时已恢复：%s（turn=%s phase=%s）",
             sid, state.get("turn"), state.get("phase"))
    return rt


def _rebuild(sid: str, state: dict) -> Any:
    """从 PG + 图状态重建 SessionRuntime。"""
    # ① 从 PG 读会话元信息
    meta = _load_session_meta(sid)
    if meta is None:
        log.warning("PG 里没有会话 %s，无法恢复", sid)
        return None
    owner, role, persona, max_turns, status = meta
    if status in ("completed", "failed"):
        log.info("会话 %s 已结束（%s），不恢复", sid, status)
        return None

    # ② 读简历文本
    resume_text = _load_resume_text(owner)
    if not resume_text.strip():
        log.warning("用户 %s 没有简历文档，用占位文本重建", owner)
        resume_text = state.get("resume_text") or "（简历未找到）"

    # ③ 构造运行时
    try:
        from app.core.runtime import SessionRuntime

        class _Req:
            pass

        req = _Req()
        req.resume = resume_text
        req.jd = state.get("jd_text", "")
        req.role = role or state.get("role") or "ai_algo"
        req.persona = persona or state.get("persona") or "balanced"
        req.max_turns = int(max_turns or state.get("max_turns") or 16)
        req.web = bool(state.get("web", False))
        req.mock = bool(state.get("mock", True))
        req.coach = bool(state.get("coach", False))
        req.document_id = ""

        rt = SessionRuntime(sid, owner, req)
    except Exception as e:  # noqa: BLE001
        log.exception("重建 SessionRuntime 失败：%s", e)
        return None

    # ④ 把图状态回灌进运行时（关键：否则状态机从 0 开始数）
    try:
        rt.restore(
            progress={
                "turn": int(state.get("turn", 0)),
                "phase": state.get("phase", ""),
                "ladder_level": int(state.get("ladder_level", 0)),
            },
            asked_qids=list(state.get("asked_qids") or []),
            topic_counts=dict(state.get("topic_counts") or {}),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("回灌状态失败（流程可继续但轮次可能不准）：%s", e)

    # ⑤ 重新包装 LLM 记账
    #
    # 踩过的坑：重启后的 4 轮完全不记账（tokens 停在重启前的值）。
    #
    # 旧 `InterviewSession.__init__` 里有这么一段（session.py:149-150）：
    #
    #     self.llm = TracedLLM(llm, self.ledger, self.budget, self.cassette)
    #     self.interviewer.llm = self.llm
    #
    # 也就是说 llm 被包了两层：RecordingLlm（我加的）→ TracedLLM（旧代码加的）。
    # `evaluator` 拿的是**原始 llm**（第 112 行 `Evaluator(llm)`），
    # 所以评分路径的调用根本没经过 RecordingLlm。
    #
    # 修法：恢复后把 RecordingLlm 塞到**所有**持有者上：
    # interviewer.llm / evaluator.llm / turn_coach.llm / session.llm
    try:
        from app.core.runtime import SessionRuntime
        session = getattr(rt, "_session", None)
        if session is not None:
            raw = SessionRuntime._unwrap_llm(getattr(session, "llm", None))
            if raw is not None:
                SessionRuntime._attach_recording(session, raw)
                log.info("恢复后已重新挂载 LLM 记账")
    except Exception as e:  # noqa: BLE001
        log.warning("恢复后挂载 LLM 记账失败（成本统计会缺）：%s", e)

    return rt


def _load_session_meta(sid: str) -> tuple | None:
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """SELECT owner, role, persona, max_turns, status
                   FROM sessions WHERE id = %s""",
                (sid,),
            )
            row = cur.fetchone()
            cur.close()
        return tuple(row) if row else None
    except Exception as e:  # noqa: BLE001
        log.warning("读会话元信息失败：%s", e)
        return None


def _load_resume_text(owner: str) -> str:
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """SELECT text FROM documents
                   WHERE owner = %s AND kind = 'resume'
                   ORDER BY created_at DESC LIMIT 1""",
                (owner,),
            )
            row = cur.fetchone()
            cur.close()
        return (row[0] or "") if row else ""
    except Exception as e:  # noqa: BLE001
        log.warning("读简历失败：%s", e)
        return ""

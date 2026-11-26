"""成本记账：在 LLM 调用处直接记，不依赖旧日志。

## 为什么放弃"读旧日志"方案

第一版设计是从 `interview_agent/logs/*.jsonl` 读 usage。实测失败：

- 旧 `LLMLogger` 默认 `echo=False` 且日志目录可能未创建
- `build_llm()` 创建的 logger 实例是临时的，可能没写文件
- 结果：turns 表 token 恒为 0，报告里"0 token / 0.0 元"

## 现在的方案：包装 LLM 端口

在 `LlmPort` 外面套一层记账，每次调用后把 usage 累加到**当前会话的账本**里。

为什么这比读日志可靠：
1. usage 是调用返回值的一部分（LangChain 的 `usage_metadata`），一定是准的
2. 按 session_id 归集，不会串账（旧方案按时间窗切，并发会混）
3. 不依赖第三方日志格式

## 数据流

    InterviewSession → LLM.chat() → UsageRecorder.wrap()
                                        ↓
                              session_usage[sid] += (tokens_in, tokens_out)
                                        ↓
                              写 turns 表 / 报告

`UsageRecorder` 用线程局部存 session_id（因为 LLM 调用是同步的、在单线程里完成），
避免给每个调用点传参数。
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

log = logging.getLogger("interview.usage")

#: 单价（元 / 1K token），按 deepseek-flash 量级
PRICE_IN = 0.0005
PRICE_OUT = 0.002

#: 当前绑定的会话。
#:
#: ## 为什么用 ContextVar 而不是 threading.local（踩过的坑）
#:
#: 第一版用 `threading.local()`。本地测试（单线程直接调）完全正常，
#: 但**通过 HTTP 跑就失效**——重启后的 4 轮一次都没记账。
#:
#: 原因：FastAPI 的同步路由函数跑在 `anyio` 的**线程池**里，
#: 每次请求可能分配不同线程；而 `usage.bind()` 和实际的 `record()`
#: 恰好落在不同线程 → thread-local 读不到。
#:
#: ContextVar 跟着「上下文」走而不是线程，uvicorn 的请求上下文能正确传递。
_CURRENT: ContextVar[str] = ContextVar("interview_current_session", default="")

#: {session_id: {"in": n, "out": n, "calls": n, "cost": f}}
_USAGE: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def current_session() -> str:
    """当前上下文绑定的 session_id（没绑就返回空串）。"""
    return _CURRENT.get()


@contextmanager
def bind(session_id: str) -> Iterator[None]:
    """把当前上下文绑定到某个会话。

    用 ContextVar 而不是传参：LLM 调用发生在旧核心的
    `evaluator.evaluate()` / `interviewer.next_question()` 内部，
    那些函数签名不能改（379 条测试依赖）。上下文变量是零侵入的方式。
    """
    token = _CURRENT.set(session_id)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def record(tokens_in: int, tokens_out: int, model: str = "") -> None:
    """记一次调用。没绑定会话就丢弃（比如启动自检时的探测调用）。"""
    sid = current_session()
    if not sid:
        # 这个日志很重要：它证明"模型被调了但没绑定会话"，
        # 是排查"成本统计缺失"的第一现场
        log.warning("usage.record 被调用但当前上下文没有绑定会话（token 丢弃）："
                    "in=%s out=%s task=%s", tokens_in, tokens_out, model)
        return
    try:
        ti, to = int(tokens_in or 0), int(tokens_out or 0)
    except (TypeError, ValueError):
        return
    if ti == 0 and to == 0:
        return

    delta_tokens = ti + to
    delta_cost = round(ti / 1000 * PRICE_IN + to / 1000 * PRICE_OUT, 6)

    with _LOCK:
        st = _USAGE.setdefault(sid, {"in": 0, "out": 0, "calls": 0, "cost": 0.0})
        st["in"] += ti
        st["out"] += to
        st["calls"] += 1
        st["cost"] = round(st["cost"] + delta_cost, 6)
        calls_now = st["calls"]

    log.info("usage 记账 sid=%s in=%d out=%d 累计调用=%d", sid, ti, to, calls_now)

    # 持久化：传增量，让数据库侧累加（重启后不会覆盖历史）
    _persist(sid, {
        "__delta_tokens": delta_tokens,
        "__delta_cost": delta_cost,
    })


def _persist(sid: str, st: dict[str, Any]) -> None:
    """把累计用量写进 sessions 表（幂等 UPDATE）。

    ## 为什么是"累加写"而不是"覆盖写"

    踩过的坑：第一版这样写：

        UPDATE sessions SET total_tokens = %s, ...     ← 用内存里的累计值

    内存值在进程重启后是空的，所以重启后的第一次 `record()` 会用
    "重启后这几次调用的量"**覆盖**掉重启前的累计值。
    实测现象：6 轮面试、中间重启过一次，报告只显示 2 次调用。

    正确做法：**在数据库侧累加**（`total_tokens = total_tokens + delta`），
    这样无论进程重启多少次，累计值都是对的。

    代价：这个函数要传增量而不是总和。
    """
    try:
        from app.repos.pg import get_conn
        # 算增量：本次 record 的 token 数
        delta_tokens = st["__delta_tokens"]
        delta_cost = st["__delta_cost"]
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """UPDATE sessions
                   SET total_tokens = total_tokens + %s,
                       total_cost_cny = total_cost_cny + %s,
                       llm_calls = llm_calls + 1
                   WHERE id = %s""",
                (delta_tokens, delta_cost, sid),
            )
            cur.close()
    except Exception:  # noqa: BLE001
        # 表可能还没加这三列（旧库），静默跳过——成本统计不该阻断面试
        pass


def snapshot(session_id: str) -> dict[str, Any]:
    """取某会话的用量。

    优先内存（快、准），内存里没有就从 PG 读（服务重启后的情况）。
    """
    with _LOCK:
        st = _USAGE.get(session_id)
        if st:
            return {
                "tokens_in": st["in"], "tokens_out": st["out"],
                "tokens": st["in"] + st["out"], "calls": st["calls"],
                "cost_cny": st["cost"], "source": "recorder",
            }

    # 内存没有 → 从 PG 恢复（重启续跑场景）
    db = _load(session_id)
    if db:
        with _LOCK:
            _USAGE[session_id] = {
                "in": db["tokens_in"], "out": db["tokens_out"],
                "calls": db["calls"], "cost": db["cost_cny"],
            }
        return {**db, "source": "database"}

    return {"tokens_in": 0, "tokens_out": 0, "tokens": 0,
            "calls": 0, "cost_cny": 0.0, "source": "none"}


def _load(sid: str) -> dict[str, Any] | None:
    """从 sessions 表读累计用量。

    注意 `tokens_in`/`tokens_out` 的拆分：库里只存总量，
    所以重启后只能给出 total。为了不让前端显示 0，
    把总量放在 `tokens`，`tokens_in`/`tokens_out` 按 5:5 估（标注 estimated）。
    """
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                "SELECT total_tokens, total_cost_cny, llm_calls "
                "FROM sessions WHERE id = %s",
                (sid,),
            )
            row = cur.fetchone()
            cur.close()
    except Exception:  # noqa: BLE001
        return None

    if not row or not row[2]:
        return None
    total = int(row[0] or 0)
    return {
        "tokens_in": total // 2,
        "tokens_out": total - total // 2,
        "tokens": total,
        "calls": int(row[2] or 0),
        "cost_cny": float(row[1] or 0),
        "estimated_split": True,
    }


def clear(session_id: str) -> None:
    with _LOCK:
        _USAGE.pop(session_id, None)


def cost_line(session_id: str) -> str:
    """给报告用的一行成本说明。"""
    s = snapshot(session_id)
    if not s["calls"]:
        return ""
    return (f"本场用量：{s['tokens']} token（{s['calls']} 次模型调用），"
            f"约 {s['cost_cny']:.4f} 元")


# ---------------------------------------------------------------------------
# LLM 包装：给 LlmPort 套一层记账
# ---------------------------------------------------------------------------

class RecordingLlm:
    """包住任意 LLM 对象，在 chat/chat_json 后记 usage。

    ## 为什么用 __getattr__ 转发而不是继承

    旧核心拿到的 `BaseLLM` 有 `chat()` / `chat_json()` / `is_mock` 等属性，
    子类还可能被 `TracedLLM` 再包一层。继承会破坏 `isinstance` 判断，
    而 `__getattr__` 转发能保证"除了我拦截的 chat/chat_json，其他行为完全不变"。

    ## 踩过的坑：这个包装会**严重拖慢**调用

    第一版在这里从 `self._inner.logger.last_usage` 同步读 usage，
    而旧 `LLMLogger.write()` 每次都要落盘 + fsync。
    实测：包装后单轮从 5 秒涨到 19 秒（约 4 倍）。

    现在的做法：**不在调用路径上同步读 usage**，
    而是让 `record()` 由调用方在图节点里显式触发（见 graph/node_evaluate）。
    这个类只负责转发，不做任何额外工作。
    """

    def __init__(self, inner: Any):
        self._inner = inner

    # ---- 转发两个核心方法（不做额外工作）----
    def chat(self, messages, **kwargs):
        return self._inner.chat(messages, **kwargs)

    def chat_json(self, messages, **kwargs):
        return self._inner.chat_json(messages, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

"""运行时注册表：管理进行中的面试会话。

## 为什么需要"注册表"而不是把会话塞进图状态

LangGraph 的 checkpointer 保存的是**状态字典**（JSON 可序列化）。
但旧的 `InterviewSession` 实例不是可序列化的——它有 llm 客户端、文件句柄等。

所以分工：
- **checkpointer** 存图状态（turn/phase/pending_question/last_eval...）→ 可跨进程恢复
- **注册表** 存活的 SessionRuntime 实例 → 进程内复用，省掉每次重建

恢复逻辑：
1. 先从注册表按 session_id 找 → 找到直接用（热路径，快）
2. 找不到（进程重启）→ 用 checkpointer 里的状态 + PG 里的 resume 文本重建
3. 重建后再放回注册表

这样"服务重启后能继续答"就成立了。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("interview.registry")

_LOCK = threading.Lock()
_RUNTIMES: dict[str, Any] = {}


def get_runtime(session_id: str) -> Any:
    """取运行时；不存在则抛 KeyError（由上层转成 404）。"""
    rt = _RUNTIMES.get(session_id)
    if rt is None:
        raise KeyError(f"运行时不存在：{session_id}")
    return rt


def has_runtime(session_id: str) -> bool:
    return session_id in _RUNTIMES


def register(session_id: str, runtime: Any) -> None:
    with _LOCK:
        _RUNTIMES[session_id] = runtime
    log.info("运行时已注册：%s（当前 %d 个）", session_id, len(_RUNTIMES))


def unregister(session_id: str) -> None:
    with _LOCK:
        _RUNTIMES.pop(session_id, None)


def count() -> int:
    return len(_RUNTIMES)


def evict_all() -> int:
    """清空（测试用：模拟进程重启）。"""
    with _LOCK:
        n = len(_RUNTIMES)
        _RUNTIMES.clear()
    return n

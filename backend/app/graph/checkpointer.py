"""PostgreSQL checkpointer：图状态持久化，支持进程重启后续跑。

## 为什么必须换掉 MemorySaver

`MemorySaver` 把图状态存在进程内存里。后果：

- 服务重启 → 所有进行中的面试丢失，只能重来
- 多 worker 部署 → 请求打到不同进程，状态互相看不见

这正是旧实现最大的痛点（返回"会话不存在（服务重启）"）。
换 `PostgresSaver` 后，`thread_id`（即 session_id）对应的状态存在 PG 里。

## 连接必须保持打开（踩坑记录）

`PostgresSaver.from_conn_string()` 返回的是**上下文管理器**。
第一版这样写：

    saver_cm = PostgresSaver.from_conn_string(dsn)
    saver = saver_cm.__enter__()
    saver.setup()

表面能建表，`health()` 也报 ok，但一 `invoke` 就炸：

    File "langgraph/checkpoint/postgres/__init__.py", line 242, in get_tuple
        with self._cursor() as cur:
    ...

原因：`__enter__` 拿到的 saver 依赖那个上下文里的连接，
一旦生成器被垃圾回收，连接就断了。

正确做法：**自己持有连接**（`psycopg.connect(..., autocommit=True)`），
再用 `PostgresSaver(conn)` 构造。这样连接的生命周期由本模块控制。
"""

from __future__ import annotations

import logging
from typing import Any

from app.settings import settings

log = logging.getLogger("interview.checkpoint")

_SAVER: Any = None
_CONN: Any = None
_SAVER_KIND = "none"


def _conn_string() -> str:
    return settings.database_url


def get_saver() -> Any:
    """取 checkpointer。优先级：PostgresSaver → MemorySaver → None。"""
    global _SAVER, _CONN, _SAVER_KIND
    if _SAVER is not None:
        return _SAVER

    # ① 尝试 PostgresSaver（自己持有连接）
    try:
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver

        # autocommit=True 是 PostgresSaver 的要求（它自己管事务边界）
        _CONN = psycopg.connect(_conn_string(), autocommit=True)
        saver = PostgresSaver(_CONN)
        saver.setup()
        _SAVER = saver
        _SAVER_KIND = "postgres"
        log.info("checkpointer：PostgresSaver 已就绪")
        return _SAVER
    except Exception as e:  # noqa: BLE001
        log.warning("PostgresSaver 不可用：%s", e)
        try:
            if _CONN is not None:
                _CONN.close()
        except Exception:  # noqa: BLE001
            pass
        _CONN = None

    # ② 退回内存
    try:
        from langgraph.checkpoint.memory import MemorySaver
        _SAVER = MemorySaver()
        _SAVER_KIND = "memory"
        log.warning("checkpointer 降级为 MemorySaver（进程重启会丢状态）")
        return _SAVER
    except Exception as e:  # noqa: BLE001
        log.warning("MemorySaver 也不可用：%s", e)
        _SAVER_KIND = "none"
        return None


def kind() -> str:
    """当前用的是哪种 checkpointer（健康检查用）。"""
    return _SAVER_KIND


def health() -> dict[str, Any]:
    try:
        get_saver()
        out: dict[str, Any] = {"ok": _SAVER_KIND != "none", "kind": _SAVER_KIND}
        if _SAVER_KIND == "postgres":
            out["durable"] = True
            out["note"] = "进程重启后可恢复"
        elif _SAVER_KIND == "memory":
            out["durable"] = False
            out["note"] = "进程重启会丢，仅开发可用"
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "kind": _SAVER_KIND, "error": str(e)}


def clear_thread(thread_id: str) -> bool:
    """删掉某个会话的 checkpoint（会话结束后清理）。

    失败不抛异常——清理是"尽力而为"，不能影响主流程。
    """
    try:
        saver = get_saver()
        if hasattr(saver, "delete_thread"):
            saver.delete_thread(thread_id)
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("清理 checkpoint 失败 %s：%s", thread_id, e)
    return False

"""依赖注入：数据库连接、Redis、当前用户、旧核心会话。

## 为什么用 FastAPI 的 Depends

旧实现每个接口里自己连库、自己解析 Cookie，导致：
- 连接没有池化（每次都新建 sqlite3.connect）
- 鉴权逻辑重复写在 20 多个分支里
- 测试时要 mock 一堆全局变量

`Depends` 把这些变成"声明式"：函数签名写上需要什么，框架负责准备和清理。
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from fastapi import Depends, Header, Request

from app.core import bridge, errors
from app.settings import settings

log = logging.getLogger("interview.deps")


# ---------------------------------------------------------------- 健康快照
def health_snapshot() -> dict[str, Any]:
    """一眼看出哪个组件挂了：数据库 / Redis / 模型 / 向量模型。

    为什么不用只返回 `{"ok": true}`：
    面试进行到一半出错，第一个要回答的问题是"哪个环节挂了"。
    各部分状态全列出来，运维不用逐个探测。
    """
    from app.core.legacy_session import session_count

    components: dict[str, Any] = {}

    # 旧核心（规则层）
    try:
        bridge.ensure_legacy_importable()
        components["legacy_core"] = {"ok": True, "roles": len(bridge.roles())}
    except Exception as e:  # noqa: BLE001
        components["legacy_core"] = {"ok": False, "error": str(e)}

    # 内存会话
    try:
        components["sessions"] = {"ok": True, "active": session_count()}
    except Exception as e:  # noqa: BLE001
        components["sessions"] = {"ok": False, "error": str(e)}

    # 数据库（PostgreSQL）
    try:
        from app.repos.pg import pg_health
        components["postgres"] = pg_health()
    except Exception as e:  # noqa: BLE001
        components["postgres"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Redis
    try:
        from app.repos.cache import cache_health
        components["redis"] = cache_health()
    except Exception as e:  # noqa: BLE001
        components["redis"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 向量模型（Ollama）
    try:
        from app.rag.embedder import embedder_health
        components["embedding"] = embedder_health()
    except Exception as e:  # noqa: BLE001
        components["embedding"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    ok = all(c.get("ok") for c in components.values())
    return {"ok": ok, "version": "2.0.0", "components": components}


# ---------------------------------------------------------------- 鉴权
def current_user(authorization: str = Header(default="")) -> dict[str, str]:
    """从 Authorization: Bearer 解析当前用户。

    为什么用 header 而不是 Cookie（旧实现用 Cookie）：
    1. 前端 Vue 调 API 用 header 更自然，不用处理 CORS credentials；
    2. 以后接小程序/移动端，header 是唯一通用方式；
    3. Cookie 需要 CSRF 防护，header 不需要。

    代价：token 要前端自己存（内存变量），刷新页面要调 refresh。
    """
    if not authorization:
        raise errors.not_logged_in()
    if not authorization.lower().startswith("bearer "):
        raise errors.token_invalid()
    token = authorization[7:].strip()
    if not token:
        raise errors.token_invalid()

    from app.auth.jwt import decode_access
    payload = decode_access(token)
    if payload is None:
        raise errors.token_expired()
    return {"user": payload["sub"], "role": payload.get("role", "user")}


def admin_user(user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
    """要求管理员角色。

    踩过的坑：加 `current_user_optional` 时用整段替换，
    把本函数的定义连带删掉了 → 启动时报
    `cannot import name 'admin_user' from 'app.deps'`，
    整站 500。改这类文件要小步替换，别整块覆盖。
    """
    if user.get("role") != "admin":
        raise errors.admin_required()
    return user


def current_user_optional(
    authorization: str = Header(default=""),
    token: str = "",
) -> dict[str, str] | None:
    """宽松版鉴权：拿不到用户就返回 None，而不是抛 401。

    ## 为什么需要这个

    浏览器的 `EventSource` **无法自定义请求头**，所以 SSE 端点带不了
    `Authorization: Bearer ...`。这是 SSE 的固有限制，不是设计选择。

    所以 SSE 端点的 token 从两个地方取：
    1. `Authorization` header（普通 fetch / curl 调用时）
    2. `?token=` query 参数（浏览器 EventSource 时）

    ## 为什么不直接用 current_user

    `current_user` 没 token 就抛 401。但 SSE 端点如果直接 401，
    前端 `EventSource` 只会触发一个笼统的 `onerror`，**拿不到任何错误信息**，
    用户看到的是"什么都没发生"。

    用宽松版 + 在生成器里显式发 error 事件，前端才能拿到
    「请先登录」这种可读提示。

    ## 安全说明

    token 出现在 URL 里会进浏览器历史和访问日志。
    缓解措施：
    - access token 只有 15 分钟寿命
    - SSE 端点只**读**一个已生成的问题，不产生副作用
    - 生产环境应加 `Referrer-Policy: no-referrer`
    """
    raw = ""
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:].strip()
    elif token:
        raw = token.strip()

    if not raw:
        return None

    try:
        from app.auth.jwt import decode_access
        payload = decode_access(raw)
        if payload is None:
            return None
        return {"user": payload["sub"], "role": payload.get("role", "user")}
    except Exception:  # noqa: BLE001
        return None


def optional_user(authorization: str = Header(default="")) -> dict[str, str] | None:
    """可选登录：未登录返回 None 而不是报错。用于公开但可个性化的接口。"""
    if not authorization:
        return None
    try:
        return current_user(authorization)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 数据库
def db_conn() -> Iterator[Any]:
    """PostgreSQL 连接（带请求级事务）。

    每次请求一个连接，结束自动提交/回滚并归还池。
    不用全局连接的原因：一个长面试可能中途请求 20 次，
    全局连接会被并发请求互相踩。
    """
    from app.repos.pg import get_conn
    conn = get_conn()
    try:
        yield conn
        if getattr(conn, "autocommit", False) is False:
            conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- Redis
def cache() -> Any:
    """Redis 客户端；未启用时返回一个内存实现（接口相同）。"""
    from app.repos.cache import get_cache
    return get_cache()


# ---------------------------------------------------------------- 请求 id
def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")

"""JWT 鉴权：access + refresh 双 token。

## 为什么不用旧实现的方案

旧 `auth.py` 是手写 HMAC token：`user:role:exp:signature`。
能用，但缺两块：

1. **没有 refresh 机制** —— 过期只能重新登录
2. **没有标准格式** —— 无法接第三方、无法被前端库识别

PyJWT 是事实标准，代价是引入一个依赖。

## 双 token 的设计（每一处都有理由）

| token | 存哪 | 有效期 | 为什么 |
|---|---|---|---|
| access | 前端**内存变量** | 15 分钟 | 短到泄露也没大用；不落 localStorage 避免 XSS 偷走 |
| refresh | **HttpOnly Cookie** | 7 天 | JS 读不到（防 XSS）；`SameSite=Lax` 防 CSRF |

**用户感知**：15 分钟到了不会"被踢下线"——前端拦截器收到 42004
自动调 refresh 换新 access 再重发原请求，用户完全无感。

**代价**：要能踢人就得删 refresh 记录（存 Redis/DB），
这是 JWT 相比 session 唯一的真实劣势，用短有效期 + refresh 白名单缓解。
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import jwt

from app.settings import settings

log = logging.getLogger("interview.auth")


def _secret() -> str:
    """JWT 密钥：环境变量优先，缺失时自动生成并警告。

    自动生成的理由：开发时不想强制配环境变量。
    但**必须警告**——多进程部署下每进程生成的密钥不同，
    token 会互相验不过（这正是旧 auth.py 踩过的 TOCTOU 坑的变种）。
    """
    if settings.jwt_secret:
        return settings.jwt_secret
    if not hasattr(_secret, "_generated"):
        _secret._generated = secrets.token_urlsafe(48)  # type: ignore[attr-defined]
        log.warning(
            "IA_JWT_SECRET 未设置，已生成临时密钥（进程重启后 token 全部失效）。"
            "生产环境请显式配置。"
        )
    return _secret._generated  # type: ignore[attr-defined]


def create_access(user: str, role: str) -> tuple[str, int]:
    """签发 access token，返回 (token, 有效期秒)。"""
    ttl = settings.access_token_minutes * 60
    now = int(time.time())
    payload = {
        "sub": user,
        "role": role,
        "iat": now,
        "exp": now + ttl,
        "typ": "access",
    }
    token = jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)
    return token, ttl


def create_refresh(user: str, role: str) -> tuple[str, int]:
    """签发 refresh token，返回 (token, 有效期秒)。"""
    ttl = settings.refresh_token_days * 86400
    now = int(time.time())
    payload = {
        "sub": user,
        "role": role,
        "iat": now,
        "exp": now + ttl,
        "typ": "refresh",
        "jti": secrets.token_hex(8),      # 唯一 id，用于白名单/踢人
    }
    token = jwt.encode(payload, _secret(), algorithm=settings.jwt_algorithm)
    return token, ttl


def decode_access(token: str) -> dict[str, Any] | None:
    """验 access token。返回 payload 或 None（过期/篡改/类型不对）。"""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if payload.get("typ") != "access":
        return None
    return payload


def decode_refresh(token: str) -> dict[str, Any] | None:
    try:
        payload = jwt.decode(token, _secret(), algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if payload.get("typ") != "refresh":
        return None
    return payload


def refresh_cookie(token: str, max_age: int) -> str:
    """构造 refresh 的 Set-Cookie 头。

    三个属性各有用途：
    - HttpOnly：JS 读不到（防 XSS 偷票）
    - SameSite=Lax：跨站请求不带（防 CSRF），但顶层跳转带（不破坏体验）
    - Path=/api/auth：只在刷新接口上带，减少暴露面
    """
    return (
        f"ia_refresh={token}; Path=/api/auth; Max-Age={max_age}; "
        "HttpOnly; SameSite=Lax"
    )


def clear_refresh_cookie() -> str:
    return "ia_refresh=; Path=/api/auth; Max-Age=0; HttpOnly; SameSite=Lax"

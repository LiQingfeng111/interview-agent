"""鉴权路由：登录、注册、刷新、登出、当前用户。

## 双 token 的接口行为

    POST /api/auth/login
      → 响应体：{access_token, expires_in, user, role}
      → 响应头：Set-Cookie: ia_refresh=...; HttpOnly; SameSite=Lax

    POST /api/auth/refresh
      → 读 Cookie 里的 refresh
      → 换新 access（响应体）+ 轮换 refresh（防重放）

    POST /api/auth/logout
      → 清 Cookie + 把 refresh 的 jti 加黑名单

## 为什么 refresh 要轮换

如果 refresh 长期不变，一旦泄露就能用满 7 天。
每次 refresh 都签发新的、旧的立即失效，攻击者用旧票会被发现
（因为旧 jti 已在黑名单里）。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from app.auth import jwt as jwtmod
from app.auth import users
from app.core import errors
from app.deps import current_user
from app.repos.cache import get_cache
from app.schemas import ApiResponse, LoginRequest, RegisterRequest

log = logging.getLogger("interview.api.auth")

router = APIRouter()


def _revoked_key(jti: str) -> str:
    return f"ia:revoked:{jti}"


def _revoke(jti: str, ttl: int = 7 * 86400) -> None:
    get_cache().set(_revoked_key(jti), "1", ttl=ttl)


def _is_revoked(jti: str) -> bool:
    return get_cache().get(_revoked_key(jti)) is not None


@router.post("/register", response_model=ApiResponse)
def register(req: RegisterRequest) -> ApiResponse:
    """注册。第一个用户自动成为管理员。"""
    result = users.register(req.user, req.password)
    return ApiResponse(data=result)


@router.post("/login", response_model=ApiResponse)
def login(req: LoginRequest, response: Response) -> ApiResponse:
    who = users.authenticate(req.user, req.password)

    access, access_ttl = jwtmod.create_access(who["user"], who["role"])
    refresh, refresh_ttl = jwtmod.create_refresh(who["user"], who["role"])

    response.headers["Set-Cookie"] = jwtmod.refresh_cookie(refresh, refresh_ttl)

    return ApiResponse(data={
        "access_token": access,
        "token_type": "bearer",
        "expires_in": access_ttl,
        "user": who["user"],
        "role": who["role"],
    })


@router.post("/refresh", response_model=ApiResponse)
def refresh(request: Request, response: Response) -> ApiResponse:
    """用 refresh Cookie 换新 access。同时轮换 refresh。"""
    token = request.cookies.get("ia_refresh", "")
    if not token:
        raise errors.refresh_expired()

    payload = jwtmod.decode_refresh(token)
    if payload is None:
        raise errors.refresh_expired()

    jti = payload.get("jti", "")
    if jti and _is_revoked(jti):
        # 旧票被重复使用 —— 可能是盗用，直接作废
        log.warning("refresh 重放检测：user=%s jti=%s", payload.get("sub"), jti)
        raise errors.token_invalid()

    user = payload["sub"]
    role = payload.get("role", "user")

    # 用户可能已被删或降权，refresh 时重新确认
    fresh = users.get_user(user)
    if fresh is None:
        raise errors.refresh_expired()
    role = fresh["role"]

    if jti:
        _revoke(jti, ttl=jwtmod.settings.refresh_token_days * 86400)

    access, access_ttl = jwtmod.create_access(user, role)
    new_refresh, refresh_ttl = jwtmod.create_refresh(user, role)
    response.headers["Set-Cookie"] = jwtmod.refresh_cookie(new_refresh, refresh_ttl)

    return ApiResponse(data={
        "access_token": access,
        "token_type": "bearer",
        "expires_in": access_ttl,
        "user": user,
        "role": role,
    })


@router.post("/logout", response_model=ApiResponse)
def logout(request: Request, response: Response) -> ApiResponse:
    """登出：作废 refresh + 清 Cookie。

    注意：access token 无法主动作废（它是无状态的，这是 JWT 的固有代价）。
    但 access 只有 15 分钟，可接受。
    """
    token = request.cookies.get("ia_refresh", "")
    if token:
        payload = jwtmod.decode_refresh(token)
        if payload and payload.get("jti"):
            _revoke(payload["jti"], ttl=jwtmod.settings.refresh_token_days * 86400)
    response.headers["Set-Cookie"] = jwtmod.clear_refresh_cookie()
    return ApiResponse(data={"ok": True})


@router.get("/me", response_model=ApiResponse)
def me(user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    return ApiResponse(data=user)


@router.get("/users", response_model=ApiResponse)
def list_users(user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    """用户列表。仅管理员可见。"""
    if user.get("role") != "admin":
        raise errors.admin_required()
    return ApiResponse(data={"users": users.list_users()})


@router.post("/users/delete", response_model=ApiResponse)
def delete_user(payload: dict[str, Any], user: dict[str, str] = Depends(current_user)) -> ApiResponse:
    if user.get("role") != "admin":
        raise errors.admin_required()
    target = str(payload.get("user", ""))
    if not target:
        raise errors.missing_field(["user"])
    if target == user["user"]:
        return ApiResponse(data={"ok": False, "error": "不能删除自己"})
    return ApiResponse(data={"ok": users.delete_user(target), "user": target})

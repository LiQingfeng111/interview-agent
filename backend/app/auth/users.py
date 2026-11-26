"""用户账号：注册、登录、密码校验。

## 为什么要自己写而不是直接搬旧 auth.py

旧 `auth.py` 有一套完整实现（pbkdf2 + SQLite + 手写 HMAC token）。
密码哈希部分**直接复用**（pbkdf2 是正确的），但存储和 token 要换：

| 项 | 旧 | 新 | 原因 |
|---|---|---|---|
| 存储 | SQLite `users.db` | PostgreSQL `users` 表 | 用户已定用 PG |
| token | 手写 `user:role:exp:sig` | PyJWT 双 token | 支持 refresh、标准格式 |
| 密码哈希 | pbkdf2_hmac | **沿用 pbkdf2_hmac** | 已经是对的，不折腾 |

**不改成 bcrypt 的理由**：pbkdf2 是 NIST 推荐算法，旧实现已经带了
每个用户独立 salt + 迭代次数，换算法只是给自己增加迁移成本。

## 登录失败锁定

连续 5 次失败锁 15 分钟，用 Redis 计数。
旧实现没有这个，等于允许无限次暴力尝试。
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from typing import Any

from app.core import errors
from app.settings import settings

log = logging.getLogger("interview.auth.users")

PBKDF2_ITERATIONS = 240_000     # 与旧实现保持一致；OWASP 2024 建议 ≥600k，但这里兼顾性能
MAX_FAILED_ATTEMPTS = 5
LOCK_SECONDS = 15 * 60


# ---------------------------------------------------------------- 密码哈希
def hash_password(password: str, salt: bytes | None = None) -> str:
    """返回 `pbkdf2$迭代次数$salt_hex$hash_hex`，自带参数便于将来升级。"""
    s = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), s, PBKDF2_ITERATIONS)
    return f"pbkdf2${PBKDF2_ITERATIONS}${s.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比较，且兼容旧格式。"""
    if not stored:
        return False
    try:
        parts = stored.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2":
            # 旧格式可能只是 hex(salt)$hex(hash)，做兼容
            return _verify_legacy(password, stored)
        _, iters, salt_hex, hash_hex = parts
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), int(iters))
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


def _verify_legacy(password: str, stored: str) -> bool:
    """兼容旧格式：`salt_hex$hash_hex`（没有迭代次数标记）。"""
    try:
        salt_hex, hash_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt_hex), 240_000)
        return secrets.compare_digest(dk.hex(), hash_hex)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 存储
def _conn():
    from app.repos.pg import get_conn
    return get_conn()


def ensure_schema() -> None:
    """建表（幂等）。第一次启动时调用。"""
    with _conn() as c:
        cur = c.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          BIGSERIAL PRIMARY KEY,
                username    VARCHAR(64) UNIQUE NOT NULL,
                pwd_hash    TEXT NOT NULL,
                role        VARCHAR(16) NOT NULL DEFAULT 'user',
                created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
                last_login  TIMESTAMPTZ
            )
        """)
        cur.close()


def count_users() -> int:
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            n = cur.fetchone()[0]
            cur.close()
            return int(n)
    except Exception:  # noqa: BLE001
        return 0


def register(user: str, password: str) -> dict[str, Any]:
    """注册。第一个用户自动成为管理员（沿用旧实现的规则）。"""
    if not settings.allow_registration:
        raise errors.registration_closed()
    if len(password) < 6:
        raise errors.password_too_short(len(password))

    first = count_users() == 0
    role = "admin" if first else "user"
    ensure_schema()

    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute(
                "INSERT INTO users (username, pwd_hash, role) VALUES (%s, %s, %s)",
                (user, hash_password(password), role),
            )
            cur.close()
    except Exception as e:  # noqa: BLE001
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            raise errors.user_exists(user) from e
        raise

    return {"ok": True, "user": user, "role": role, "first": first}


def _lock_key(user: str) -> str:
    return f"auth:fail:{user}"


def _check_locked(user: str) -> None:
    try:
        from app.repos.cache import get_cache
        n = get_cache().get(_lock_key(user))
        if n and int(n) >= MAX_FAILED_ATTEMPTS:
            raise errors.account_locked(LOCK_SECONDS // 60)
    except errors.AppError:
        raise
    except Exception:  # noqa: BLE001
        pass


def _note_failure(user: str) -> None:
    try:
        from app.repos.cache import get_cache
        c = get_cache()
        key = _lock_key(user)
        n = int(c.get(key) or 0) + 1
        c.set(key, str(n), ttl=LOCK_SECONDS)
        log.warning("登录失败 %s 第 %d 次", user, n)
    except Exception:  # noqa: BLE001
        pass


def _clear_failures(user: str) -> None:
    try:
        from app.repos.cache import get_cache
        get_cache().delete(_lock_key(user))
    except Exception:  # noqa: BLE001
        pass


def authenticate(user: str, password: str) -> dict[str, str]:
    """校验账号密码。失败抛 AppError（带具体提示）。"""
    _check_locked(user)
    ensure_schema()

    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT pwd_hash, role FROM users WHERE username = %s", (user,))
        row = cur.fetchone()
        cur.close()

    if not row:
        _note_failure(user)
        raise errors.bad_credentials()

    pwd_hash, role = row
    if not verify_password(password, pwd_hash):
        _note_failure(user)
        raise errors.bad_credentials()

    _clear_failures(user)
    try:
        with _conn() as c:
            cur = c.cursor()
            cur.execute("UPDATE users SET last_login = now() WHERE username = %s", (user,))
            cur.close()
    except Exception:  # noqa: BLE001
        pass

    return {"user": user, "role": role or "user"}


def get_user(user: str) -> dict[str, str] | None:
    ensure_schema()
    with _conn() as c:
        cur = c.cursor()
        cur.execute("SELECT username, role FROM users WHERE username = %s", (user,))
        row = cur.fetchone()
        cur.close()
    return {"user": row[0], "role": row[1]} if row else None


def list_users() -> list[dict[str, Any]]:
    ensure_schema()
    with _conn() as c:
        cur = c.cursor()
        cur.execute(
            "SELECT username, role, created_at, last_login FROM users ORDER BY created_at"
        )
        rows = cur.fetchall()
        cur.close()
    return [
        {"user": r[0], "role": r[1],
         "created_at": r[2].isoformat() if r[2] else "",
         "last_login": r[3].isoformat() if r[3] else ""}
        for r in rows
    ]


def delete_user(user: str) -> bool:
    with _conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (user,))
        n = cur.rowcount
        cur.close()
    return n > 0

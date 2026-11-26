"""鉴权测试：JWT 双 token + 口令哈希。

## 为什么这批测试是补的

原来的 `tests/test_auth.py` 测的是**旧架构**（SQLite 用户表 + 手写 HMAC
签名 cookie），归档旧实现时它一起失效了。

而新架构（PostgreSQL + PyJWT）**一直没有专门的鉴权测试** ——
这是个真实缺口：鉴权错了，整个多用户隔离就是空的。

## 测什么（按"错了会怎样"排序）

1. **口令不能明文比较** —— 同一个口令给两个用户，hash 必须不同（salt 生效）
2. **token 改一个字符就失效** —— 防伪造，这是 JWT 的命门
3. **过期 token 不能用** —— 防"登录一次永久有效"
4. **access/refresh 不能混用** —— 拿 refresh 当 access 用是常见越权路径
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import pytest

# IA_JWT_SECRET 必须在 import jwt 模块前设好，否则会生成临时密钥
os.environ.setdefault("IA_JWT_SECRET", "test-secret-for-unit-tests-only-32bytes")

from app.auth import jwt as jwt_mod
from app.auth.users import hash_password, verify_password


# ============================================================================
# 口令哈希
# ============================================================================

def test_password_not_plaintext():
    """hash 结果里不能出现原文。"""
    stored = hash_password("hunter2")
    assert "hunter2" not in stored
    assert len(stored) > 20


def test_same_password_different_users_different_hash():
    """同一口令、不同用户 → hash 必须不同（每用户随机 salt）。

    这是防"彩虹表 + 撞库"的基础：如果同口令 hash 相同，
    攻击者脱库后一次破解就能打穿所有同密码账号。
    """
    a = hash_password("same-password")
    b = hash_password("same-password")
    assert a != b, "salt 没生效：同口令产生了相同 hash"
    # 但两个都能验证通过
    assert verify_password("same-password", a)
    assert verify_password("same-password", b)


def test_wrong_password_rejected():
    stored = hash_password("correct")
    assert not verify_password("wrong", stored)
    assert not verify_password("", stored)


def test_verify_survives_garbage_stored():
    """脏数据不能让 verify 抛异常（否则登录接口 500）。"""
    for bad in ("", "not-a-hash", "pbkdf2$bad", "$", "a$b$c$d"):
        assert verify_password("x", bad) is False


# ============================================================================
# JWT
# ============================================================================

def test_access_token_roundtrip():
    token, expires_in = jwt_mod.create_access("alice", "admin")
    assert expires_in > 0
    payload = jwt_mod.decode_access(token)
    assert payload is not None
    assert payload["sub"] == "alice"
    assert payload["role"] == "admin"


def test_tampered_token_rejected():
    """改一个字符必须失效 —— 这是 JWT 的命门。"""
    token, _ = jwt_mod.create_access("alice", "user")
    # 改最后一个字符
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert jwt_mod.decode_access(tampered) is None, "被篡改的 token 居然通过了"


def test_wrong_signature_rejected():
    """用别的密钥签的 token 不能通过。"""
    import jwt as pyjwt

    forged = pyjwt.encode(
        {"sub": "attacker", "role": "admin", "exp": int(time.time()) + 3600,
         "typ": "access"},
        "a-completely-different-secret-key",
        algorithm="HS256",
    )
    assert jwt_mod.decode_access(forged) is None


def test_expired_token_rejected():
    """过期的 token 不能用。

    直接构造一个 exp 在过去的 token（不 sleep，测试更快也更稳）。
    """
    import jwt as pyjwt

    expired = pyjwt.encode(
        {"sub": "alice", "role": "user", "exp": int(time.time()) - 10,
         "typ": "access"},
        jwt_mod._secret(),
        algorithm="HS256",
    )
    assert jwt_mod.decode_access(expired) is None, "过期 token 居然能通过"


def test_access_and_refresh_not_interchangeable():
    """access 不能当 refresh 用，反之亦然。

    混用是常见的越权路径：refresh 有效期 7 天，
    如果它能当 access 用，等于把 15 分钟的保护直接废掉。
    """
    access, _ = jwt_mod.create_access("alice", "user")
    refresh, _ = jwt_mod.create_refresh("alice", "user")

    assert jwt_mod.decode_refresh(access) is None, "access 被当成 refresh 接受了"
    assert jwt_mod.decode_access(refresh) is None, "refresh 被当成 access 接受了"


def test_refresh_has_longer_ttl():
    _, access_ttl = jwt_mod.create_access("alice", "user")
    _, refresh_ttl = jwt_mod.create_refresh("alice", "user")
    assert refresh_ttl > access_ttl * 10, (
        f"refresh({refresh_ttl}s) 应远长于 access({access_ttl}s)"
    )


def test_refresh_cookie_is_httponly():
    """refresh cookie 必须是 HttpOnly（防 XSS 偷 token）。"""
    header = jwt_mod.refresh_cookie("some-token", max_age=3600)
    low = header.lower()
    assert "httponly" in low, "refresh cookie 没有 HttpOnly，XSS 能偷走"
    assert "samesite" in low, "缺少 SameSite，无法防 CSRF"
    assert "path=/" in low


def test_clear_cookie_expires():
    header = jwt_mod.clear_refresh_cookie()
    assert "max-age=0" in header.lower() or "expires=" in header.lower()


def test_empty_token_rejected():
    assert jwt_mod.decode_access("") is None
    assert jwt_mod.decode_refresh("") is None

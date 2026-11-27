"""默认账号的幂等性测试。

## 要证明的三件事

1. **账号不存在 → 创建**（新人 clone 后能直接登录）
2. **账号已存在 → 不动密码**（用户改了密码不会被重置回默认）
3. **生产环境 → 不创建**（demo/demo123456 这种弱口令不该进生产）

## 为什么第 2 条最重要

"保证账号一直在"很容易写成"每次启动都把密码重置成默认值"——
那样用户自己改了密码，重启一次就失效，比没有账号更气人。

所以幂等的定义是：**只在不存在时创建**。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

import pytest

os.environ.setdefault("IA_JWT_SECRET", "test-secret-for-unit-tests-32bytes")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """每个测试前清掉相关环境变量，避免互相污染。"""
    for k in ("IA_DEMO_USER", "IA_DEMO_PASSWORD", "IA_DEMO_RESET", "IA_ENV"):
        monkeypatch.delenv(k, raising=False)
    yield


def _db_available() -> bool:
    try:
        from app.repos.pg import get_conn
        with get_conn() as c:
            c.cursor().execute("SELECT 1")
        return True
    except Exception:  # noqa: BLE001
        return False


needs_db = pytest.mark.skipif(not _db_available(), reason="需要 PostgreSQL")


@needs_db
def test_creates_when_missing():
    """账号不存在 → 创建。"""
    from app.auth.users import get_user
    from app.core import bootstrap
    from app.repos.pg import get_conn

    # 用一个不可能存在的测试账号
    os.environ["IA_DEMO_USER"] = "t_bootstrap_missing"
    os.environ["IA_DEMO_PASSWORD"] = "test123456"

    # 先确保它不存在
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", ("t_bootstrap_missing",))
        cur.close()

    r = bootstrap.ensure_demo_user()
    assert r["created"] is True, f"应创建账号，实际：{r}"
    assert get_user("t_bootstrap_missing") is not None

    # 清理
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", ("t_bootstrap_missing",))
        cur.close()


@needs_db
def test_does_not_reset_existing_password():
    """账号已存在 → **不动密码**（幂等的核心）。"""
    from app.auth.users import authenticate, hash_password
    from app.core import bootstrap
    from app.repos.pg import get_conn

    name = "t_bootstrap_existing"
    custom_pwd = "myOwnPassword999"

    # 造一个密码是 custom_pwd 的账号
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (name,))
        cur.execute(
            "INSERT INTO users (username, pwd_hash, role) VALUES (%s, %s, 'user')",
            (name, hash_password(custom_pwd)),
        )
        cur.close()

    # bootstrap 配的是默认密码，但不该覆盖
    os.environ["IA_DEMO_USER"] = name
    os.environ["IA_DEMO_PASSWORD"] = "demo123456"

    r = bootstrap.ensure_demo_user()
    assert r["created"] is False, f"不该创建，实际：{r}"

    # 原密码仍可用
    assert authenticate(name, custom_pwd)["user"] == name, "密码被覆盖了！"
    # 默认密码不该能用
    with __import__("contextlib").suppress(Exception):
        authenticate(name, "demo123456")
        pytest.fail("默认密码居然能登录 —— 说明密码被重置了")

    # 清理
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (name,))
        cur.close()


@needs_db
def test_explicit_reset_works():
    """IA_DEMO_RESET=1 时允许重置密码。"""
    from app.auth.users import authenticate, hash_password
    from app.core import bootstrap
    from app.repos.pg import get_conn

    name = "t_bootstrap_reset"

    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (name,))
        cur.execute(
            "INSERT INTO users (username, pwd_hash, role) VALUES (%s, %s, 'user')",
            (name, hash_password("oldPassword123")),
        )
        cur.close()

    os.environ["IA_DEMO_USER"] = name
    os.environ["IA_DEMO_PASSWORD"] = "newPassword456"
    os.environ["IA_DEMO_RESET"] = "1"

    r = bootstrap.ensure_demo_user()
    assert "重置" in str(r.get("skipped")), f"应执行重置，实际：{r}"

    assert authenticate(name, "newPassword456")["user"] == name, "新密码应可用"

    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (name,))
        cur.close()


def test_empty_user_skips():
    """配成空字符串 → 什么都不做。"""
    from app.core import bootstrap

    os.environ["IA_DEMO_USER"] = ""
    os.environ["IA_DEMO_PASSWORD"] = ""

    r = bootstrap.ensure_demo_user()
    assert r["created"] is False
    assert r["skipped"], "应该说明跳过原因"


def test_production_refuses():
    """生产环境拒绝创建演示账号。"""
    from app.core import bootstrap

    os.environ["IA_ENV"] = "production"
    os.environ["IA_DEMO_USER"] = "demo"
    os.environ["IA_DEMO_PASSWORD"] = "demo123456"

    r = bootstrap.ensure_demo_user()
    assert r["created"] is False
    assert "生产" in str(r.get("skipped")), f"应说明生产环境跳过，实际：{r}"


@needs_db
def test_role_is_not_always_admin():
    """已有其他用户时，新账号是 user 不是 admin。

    安全考虑：不能无条件给 admin ——
    那样任何能连数据库的人用 demo 账号就拿到管理员权限。
    """
    from app.auth.users import count_users, get_user
    from app.core import bootstrap
    from app.repos.pg import get_conn

    # 库里已经有用户（含测试造的那些）
    if count_users() == 0:
        pytest.skip("库里没用户，无法验证非首用户场景")

    name = "t_bootstrap_role"
    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (name,))
        cur.close()

    os.environ["IA_DEMO_USER"] = name
    os.environ["IA_DEMO_PASSWORD"] = "test123456"

    bootstrap.ensure_demo_user()
    u = get_user(name)
    assert u is not None
    assert u["role"] == "user", f"已有用户时新账号应为 user，实际 {u['role']}"

    with get_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM users WHERE username = %s", (name,))
        cur.close()

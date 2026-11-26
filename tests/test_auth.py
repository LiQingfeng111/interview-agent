"""账号与登录测试。

重点测三件事：
  1. 口令不能被明文比较（哈希+每用户 salt，同密码不同用户 hash 不同）
  2. 签名 cookie 改一个字符就失效（防伪造 user=admin）
  3. 过期 token 不能用（防"登录一次永久有效"）
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth


class TestPassword(unittest.TestCase):
    def test_hash_and_verify(self):
        h = auth.hash_password("S3cret!")
        self.assertTrue(h.startswith("pbkdf2$"))
        self.assertTrue(auth.verify_password("S3cret!", h))
        self.assertFalse(auth.verify_password("s3cret!", h))

    def test_same_password_different_salt(self):
        a = auth.hash_password("same")
        b = auth.hash_password("same")
        self.assertNotEqual(a, b, "两个用户用同一密码时 hash 必须不同（salt 随机）")

    def test_bad_hash_returns_false(self):
        for bad in ("", "plain", "pbkdf2$x$y$z", "md5$1$a$b"):
            self.assertFalse(auth.verify_password("x", bad))


class TestUsers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "u.db")

    def test_create_and_auth(self):
        self.assertTrue(auth.create_user("alice", "pass1234", path=self.db))
        u = auth.authenticate("alice", "pass1234", path=self.db)
        self.assertEqual(u, {"user": "alice", "role": "user"})
        self.assertIsNone(auth.authenticate("alice", "wrong", path=self.db))
        self.assertIsNone(auth.authenticate("nobody", "pass1234", path=self.db))

    def test_duplicate_and_short_password(self):
        self.assertTrue(auth.create_user("bob", "pass1234", path=self.db))
        self.assertFalse(auth.create_user("bob", "pass1234", path=self.db))
        self.assertFalse(auth.create_user("carol", "123", path=self.db))

    def test_default_admin(self):
        self.assertTrue(auth.ensure_default_admin(self.db))
        self.assertFalse(auth.ensure_default_admin(self.db), "已有 admin 时不应重建")
        u = auth.authenticate("admin", "admin123", path=self.db)
        self.assertEqual(u["role"], "admin")

    def test_set_password(self):
        auth.create_user("dave", "oldpass1", path=self.db)
        self.assertTrue(auth.set_password("dave", "newpass1", path=self.db))
        self.assertIsNone(auth.authenticate("dave", "oldpass1", path=self.db))
        self.assertIsNotNone(auth.authenticate("dave", "newpass1", path=self.db))
        self.assertFalse(auth.set_password("nobody", "newpass1", path=self.db))


class TestToken(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.secret = os.path.join(self.tmp, "secret.key")

    def test_roundtrip(self):
        tok = auth.make_token("alice", "user", path=self.secret)
        self.assertEqual(auth.parse_token(tok, path=self.secret),
                         {"user": "alice", "role": "user"})

    def test_tamper_rejected(self):
        tok = auth.make_token("alice", "user", path=self.secret)
        forged = tok.replace("alice", "admin")
        self.assertIsNone(auth.parse_token(forged, path=self.secret))
        # 改角色也要失效
        self.assertIsNone(auth.parse_token(tok.replace(":user:", ":admin:"),
                                           path=self.secret))

    def test_garbage_rejected(self):
        for bad in ("", "a:b", "a:b:c:d:e", "not-a-token"):
            self.assertIsNone(auth.parse_token(bad, path=self.secret))

    def test_expired_rejected(self):
        exp = int(time.time()) - 10
        data = f"alice:user:{exp}"
        tok = f"{data}:{auth._sign(data, auth._secret(self.secret))}"
        self.assertIsNone(auth.parse_token(tok, path=self.secret))

    def test_cookie_helpers(self):
        tok = auth.make_token("bob", "admin", path=self.secret)
        header = auth.cookie_header(tok)
        self.assertIn("HttpOnly", header)
        self.assertIn("SameSite=Lax", header)
        self.assertEqual(auth.token_from_cookie("x=1; " + header.split(";")[0]), tok)
        self.assertIn("Max-Age=0", auth.cookie_header("", clear=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestActorInjection(unittest.TestCase):
    """登录态注入：会话接口覆盖 user，账号管理接口不能覆盖。

    这条 bug 是实测出来的：admin 在后台新建用户时，请求里的 user 被登录态改成 admin，
    于是"新建 student1"实际在新建 admin → 永远返回"用户名已存在"，且界面毫无提示。
    """

    def test_session_endpoints_use_login_identity(self):
        import webui
        who = {"user": "student1", "role": "user"}
        for path in ("/api/start", "/api/answer", "/api/end"):
            got = webui.with_actor(path, {"user": "someone-else"}, who)
            self.assertEqual(got["user"], "student1", f"{path} 必须用登录态")

    def test_user_management_keeps_target(self):
        import webui
        admin = {"user": "admin", "role": "admin"}
        got = webui.with_actor("/api/users", {"user": "student1", "password": "pass1234"}, admin)
        self.assertEqual(got["user"], "student1", "新建用户时被操作对象不能被覆盖")
        changed = webui.with_actor("/api/password", {"target": "bob", "password": "x"}, admin)
        self.assertEqual(changed["user"], "bob")

    def test_actor_recorded(self):
        import webui
        got = webui.with_actor("/api/start", {}, {"user": "u1", "role": "user"})
        self.assertEqual(got["_actor"], "u1")
        self.assertEqual(got["_role"], "user")

class TestRegister(unittest.TestCase):
    """自助注册：第一个账号必须是管理员，否则没人能进后台。

    这是"注册入口"最容易做错的一条：如果所有注册者都是普通用户，
    那后台永远进不去——等于把自己锁在门外。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "r.db")

    def test_first_user_becomes_admin(self):
        out = auth.register("first", "pass1234", path=self.db)
        self.assertTrue(out["ok"])
        self.assertEqual(out["role"], "admin")
        self.assertTrue(out["first"])
        self.assertEqual(auth.authenticate("first", "pass1234", path=self.db)["role"], "admin")

    def test_second_user_is_normal(self):
        auth.register("first", "pass1234", path=self.db)
        out = auth.register("second", "pass1234", path=self.db)
        self.assertEqual(out["role"], "user")
        self.assertFalse(out["first"])

    def test_reject_bad_input(self):
        self.assertFalse(auth.register("", "pass1234", path=self.db)["ok"])
        self.assertFalse(auth.register("x", "123", path=self.db)["ok"], "少于 6 位应拒绝")

    def test_reject_duplicate(self):
        auth.register("dup", "pass1234", path=self.db)
        out = auth.register("dup", "pass1234", path=self.db)
        self.assertFalse(out["ok"])
        self.assertIn("已被占用", out["error"])

    def test_count_users(self):
        self.assertEqual(auth.count_users(self.db), 0)
        auth.register("a", "pass1234", path=self.db)
        self.assertEqual(auth.count_users(self.db), 1)

class TestAuthMode(unittest.TestCase):
    """鉴权模式的判定依据是"监听地址"，不是"有没有账号库"。

    这是本项目第二次纠正同一个设计错误：
    第一版强制所有人登录（连本机自己用也要注册），第二版改成按地址自动判断。
    判断逻辑放成纯函数，才能不启服务就测出来。
    """

    def test_loopback_needs_no_login(self):
        import webui
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertEqual(webui.resolve_auth_mode(host), "off", host)

    def test_exposed_requires_login(self):
        import webui
        for host in ("0.0.0.0", "192.168.1.5", "10.0.0.2"):
            self.assertEqual(webui.resolve_auth_mode(host), "on", host)

    def test_explicit_override_wins(self):
        import webui
        self.assertEqual(webui.resolve_auth_mode("0.0.0.0", "off"), "off")
        self.assertEqual(webui.resolve_auth_mode("127.0.0.1", "on"), "on")
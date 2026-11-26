"""账号与登录：SQLite 用户表 + PBKDF2 口令哈希 + HMAC 签名 cookie。

为什么现在才做：之前是"自托管单机工具，先要能用"，任何人打开都能看所有会话。
要把它讲成"平台"，第一件事就是**谁是谁**——没有这个，多用户只是标签，不是隔离。

设计取舍（都很保守，因为这个项目没有第三方依赖）：
  · 口令：PBKDF2-HMAC-SHA256 + 每用户随机 salt，迭代 20 万次，格式 `pbkdf2$iter$salt$hash`
  · 会话：签名 cookie（HMAC-SHA256，密钥存 data/secret.key，首次自动生成）
    —— 不引入 JWT 库，也不做服务端 session 表：单机场景下签名 cookie 足够且无状态
  · 角色：只有 user / admin 两级。admin 能看所有人的会话与后台
  · 默认账号 admin/admin123 **首次登录会强制提示改密**，并在启动日志里警告

不做的：找回密码、邮箱验证、第三方登录、限流。这些是"平台该有"的，
但不是这个阶段的目标——写在 docs/PLATFORM-GAP.md 里，不假装有。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("IA_USERS_DB") or os.path.join(ROOT, "data", "users.db")
SECRET_PATH = os.environ.get("IA_SECRET_FILE") or os.path.join(ROOT, "data", "secret.key")

ITERATIONS = 200_000
COOKIE_NAME = "ia_session"
COOKIE_TTL = 7 * 24 * 3600          # 7 天
# 密钥最小长度：短于此视为「没写成功」，重新生成（避免用空密钥签名）
MIN_SECRET_BYTES = 16
DEFAULT_ADMIN = ("admin", "admin123")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user        TEXT PRIMARY KEY,
    pwd         TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',
    created_at  TEXT NOT NULL,
    last_login  TEXT
);
"""


# ------------------------------------------------------------------ 口令
def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return "pbkdf2$%d$%s$%s" % (
        ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)   # 定长比较，防时序侧信道
    except Exception:
        return False


# ------------------------------------------------------------------ 存储
def _conn(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")   # 多进程同时读写时不要立刻报错
    conn.executescript(SCHEMA)
    return conn


def create_user(user: str, password: str, role: str = "user",
                path: str = DB_PATH) -> bool:
    user = (user or "").strip()[:40]
    if not user or len(password or "") < 6:
        return False
    try:
        with _conn(path) as conn:
            conn.execute(
                "INSERT INTO users (user, pwd, role, created_at) VALUES (?,?,?,?)",
                (user, hash_password(password), role, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        return True
    except sqlite3.IntegrityError:
        return False


def authenticate(user: str, password: str, path: str = DB_PATH) -> dict | None:
    with _conn(path) as conn:
        row = conn.execute("SELECT user, pwd, role FROM users WHERE user=?",
                           ((user or "").strip(),)).fetchone()
    if not row or not verify_password(password or "", row[1]):
        return None
    with _conn(path) as conn:
        conn.execute("UPDATE users SET last_login=? WHERE user=?",
                     (time.strftime("%Y-%m-%d %H:%M:%S"), row[0]))
    return {"user": row[0], "role": row[2] or "user"}


def get_user(user: str, path: str = DB_PATH) -> dict | None:
    with _conn(path) as conn:
        row = conn.execute("SELECT user, role FROM users WHERE user=?",
                           (user,)).fetchone()
    return {"user": row[0], "role": row[1] or "user"} if row else None


def list_users(path: str = DB_PATH) -> list[dict]:
    with _conn(path) as conn:
        rows = conn.execute(
            "SELECT user, role, created_at, last_login FROM users ORDER BY created_at"
        ).fetchall()
    return [{"user": r[0], "role": r[1], "created_at": r[2], "last_login": r[3]}
            for r in rows]


def set_password(user: str, password: str, path: str = DB_PATH) -> bool:
    if len(password or "") < 6:
        return False
    with _conn(path) as conn:
        cur = conn.execute("UPDATE users SET pwd=? WHERE user=?",
                           (hash_password(password), user))
    return cur.rowcount > 0


def ensure_default_admin(path: str = DB_PATH) -> bool:
    """没有 admin 时建默认账号。返回 True 表示用了默认口令（应提示改密）。"""
    with _conn(path) as conn:
        row = conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
    if row:
        return False
    create_user(DEFAULT_ADMIN[0], DEFAULT_ADMIN[1], role="admin", path=path)
    return True


# ------------------------------------------------------------------ 会话 cookie
def _secret(path: str = SECRET_PATH) -> bytes:
    """读取（必要时原子创建）签名密钥。

    第一版是"先看存在不存在，不存在就写"，这是典型的 TOCTOU 竞争：
    两个请求同时进来，都判断"文件不存在"，都生成了各自的密钥，
    后写的覆盖先写的 —— 于是先签发的那张票用自己的密钥验不过，
    表现为"偶发的登录失效"。全量测试跑两遍才复现（一次过、一次挂），
    靠的是 `test_auth.py::test_roundtrip` 那条用例。

    现在的做法：
    1. 读到长度足够的密钥就直接用；
    2. 否则写临时文件，再用 `os.link` 原子占位 —— 只有一个进程能成功，
       抢输的那个不报错，转而读赢家写下的内容；
    3. 无论谁赢，最后都从磁盘读一次，保证"签"和"验"永远用同一把密钥。
    空文件/短文件按"不可用"处理，否则会退化成用空密钥签名。
    """
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    def _read() -> bytes:
        try:
            with open(path, "rb") as f:
                return f.read().strip()
        except FileNotFoundError:
            return b""

    current = _read()
    if len(current) >= MIN_SECRET_BYTES:
        return current

    key = secrets.token_bytes(32)
    tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass

        placed = False
        try:
            os.link(tmp, path)          # 原子创建：只有一个能成功
            placed = True
        except FileExistsError:
            placed = False
        except OSError:
            os.replace(tmp, path)       # 不支持硬链接时退回覆盖
            placed = True

        if not placed:
            # 抢输了：先看赢家写的那份能不能用。
            # 能用就采用它（并发调用必须收敛到同一把密钥）；
            # 只有它确实是坏的（空/短文件）才覆盖修复。
            existing = _read()
            if len(existing) >= MIN_SECRET_BYTES:
                return existing
            os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    final = _read()
    if len(final) < MIN_SECRET_BYTES:
        # 极端情况（文件被外部清空）：本次直接用内存里的密钥，至少不留空密钥
        return key
    return final


def _sign(data: str, secret: bytes) -> str:
    return base64.urlsafe_b64encode(
        hmac.new(secret, data.encode(), hashlib.sha256).digest()).decode().rstrip("=")


def make_token(user: str, role: str = "user", path: str = SECRET_PATH) -> str:
    exp = int(time.time()) + COOKIE_TTL
    data = f"{user}:{role}:{exp}"
    return f"{data}:{_sign(data, _secret(path))}"


def parse_token(token: str, path: str = SECRET_PATH) -> dict | None:
    """验签 + 验过期。返回 {user, role} 或 None。"""
    if not token or token.count(":") < 3:
        return None
    try:
        user, role, exp_s, sig = token.rsplit(":", 3)
        exp = int(exp_s)
    except ValueError:
        return None
    data = f"{user}:{role}:{exp}"
    if not hmac.compare_digest(sig, _sign(data, _secret(path))):
        return None
    if exp < time.time():
        return None
    return {"user": user, "role": role}


def cookie_header(token: str, *, clear: bool = False) -> str:
    if clear:
        return f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"
    return (f"{COOKIE_NAME}={token}; Path=/; Max-Age={COOKIE_TTL}; "
            "HttpOnly; SameSite=Lax")


def token_from_cookie(cookie_header_value: str) -> str:
    for part in (cookie_header_value or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == COOKIE_NAME:
                return v.strip()
    return ""

def count_users(path: str = DB_PATH) -> int:
    with _conn(path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def register(user: str, password: str, path: str = DB_PATH) -> dict:
    """自助注册。第一个注册的人自动成为管理员。

    为什么第一个是管理员：这是单机工具，没有第二个"运维"来发权限。
    如果所有注册者都是普通用户，就永远没人能进后台——等于把自己锁在门外。
    这条规则同时也是一个安全边界：谁先装谁拥有，装好之后请立刻改密码。
    """
    if not (user or "").strip():
        return {"ok": False, "error": "用户名不能为空"}
    if len(password or "") < 6:
        return {"ok": False, "error": "密码至少 6 位"}
    first = count_users(path) == 0
    role = "admin" if first else "user"
    if not create_user(user, password, role=role, path=path):
        return {"ok": False, "error": "用户名已被占用"}
    return {"ok": True, "user": user.strip(), "role": role, "first": first}
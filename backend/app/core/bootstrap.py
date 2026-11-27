"""默认账号：启动时幂等保证存在。

## 为什么需要这个模块

用户反馈"账号密码都对却登不进去"，排查后发现是数据库卷被删，
注册的账号跟着没了。

**这类问题的根本解决方式不是"记得备份"，而是"把账号写进代码"。**

只要代码在，账号就在：

    别人 git clone → 起数据库 → 后端启动
                              └─→ 自动建 demo 账号
                                → 打开就能登

## 幂等语义（关键）

"保证存在"不能每次都覆盖密码——那样用户改了密码会被重置回默认。
所以规则是：

    账号不存在  → 创建
    账号已存在  → **什么都不做**（不动密码，不动角色）

这样第一次启动建好，之后无论重启多少次都不会改。

如果确实要重置密码，用 `tools/create_user.sh` 或环境变量
`IA_DEMO_RESET=1` 显式触发。

## 安全边界

默认账号是**给本地开发和演示用的**。生产环境必须关掉：

    IA_DEMO_USER=          # 留空 = 不创建
    IA_DEMO_PASSWORD=      # 留空 = 不创建

代码里也做了保护：如果检测到 `IA_ENV=production`，
即使配了账号也会拒绝创建，并打一条警告。
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("interview.bootstrap")

#: 默认演示账号。可通过 IA_DEMO_USER / IA_DEMO_PASSWORD 覆盖。
DEFAULT_USER = "demo"
DEFAULT_PASSWORD = "demo123456"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def ensure_demo_user() -> dict[str, object]:
    """确保默认账号存在（幂等）。

    Returns:
        {"created": bool, "user": str, "skipped": str}
        skipped 非空表示没做任何事，值是原因。
    """
    user = _env("IA_DEMO_USER", DEFAULT_USER)
    password = _env("IA_DEMO_PASSWORD", DEFAULT_PASSWORD)

    # 显式留空 = 用户不想要默认账号
    if not user or not password:
        return {"created": False, "user": "", "skipped": "未配置默认账号"}

    # 生产环境不建演示账号
    #
    # 这是硬保护：demo/demo123456 这种弱口令进生产等于开门。
    # 有些人可能 copy 了 .env 就上生产，光靠文档提醒不够。
    env_kind = _env("IA_ENV", "development").lower()
    if env_kind in ("production", "prod"):
        log.warning(
            "检测到 IA_ENV=%s，跳过默认账号创建（演示账号不该进生产）",
            env_kind,
        )
        return {"created": False, "user": user, "skipped": "生产环境跳过"}

    try:
        from app.auth.users import count_users, get_user, hash_password
        from app.repos.pg import get_conn
    except Exception as e:  # noqa: BLE001
        log.warning("无法导入鉴权模块，跳过默认账号：%s", e)
        return {"created": False, "user": user, "skipped": f"导入失败: {e}"}

    # 已存在 → 不动它（幂等的核心）
    try:
        existing = get_user(user)
    except Exception as e:  # noqa: BLE001
        log.warning("查询默认账号失败：%s", e)
        return {"created": False, "user": user, "skipped": f"查询失败: {e}"}

    if existing:
        # 显式重置
        if _env("IA_DEMO_RESET") in ("1", "true", "yes"):
            try:
                with get_conn() as c:
                    cur = c.cursor()
                    cur.execute(
                        "UPDATE users SET pwd_hash = %s WHERE username = %s",
                        (hash_password(password), user),
                    )
                    cur.close()
                log.warning("已按 IA_DEMO_RESET 重置 %s 的密码", user)
                return {"created": False, "user": user, "skipped": "已重置密码"}
            except Exception as e:  # noqa: BLE001
                log.warning("重置默认账号密码失败：%s", e)

        return {"created": False, "user": user, "skipped": "已存在"}

    # 不存在 → 创建
    #
    # 角色规则：库里一个人都没有时给 admin（首用户即管理员），
    # 否则给普通 user。不能无条件给 admin——那样任何能连数据库的人
    # 都能用 demo 账号拿到管理员权限。
    try:
        is_first = count_users() == 0
        role = "admin" if is_first else "user"

        with get_conn() as c:
            cur = c.cursor()
            cur.execute(
                """INSERT INTO users (username, pwd_hash, role)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (username) DO NOTHING""",
                (user, hash_password(password), role),
            )
            inserted = cur.rowcount
            cur.close()

        if inserted:
            log.info(
                "已创建默认账号 %s（角色 %s）—— 首次启动，u=账号 p=密码见 README",
                user, role,
            )
            return {"created": True, "user": user, "skipped": ""}
        return {"created": False, "user": user, "skipped": "并发创建冲突"}
    except Exception as e:  # noqa: BLE001
        log.warning("创建默认账号失败：%s", e)
        return {"created": False, "user": user, "skipped": f"创建失败: {e}"}

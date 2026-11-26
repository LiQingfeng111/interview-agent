# 从"一个脚本"到"像平台"：这一版做了什么、还差什么

目标不是把功能重写，而是让**形态**配得上"平台"这个词。
做法是三层工程：前端资源拆出去、加真正的账号与角色、加部署入口。

---

## 一、已完成（可验证）

### 1. 前端不再是 Python 字符串

| 之前 | 现在 |
|---|---|
| `webui.py` 里 433 行内嵌 HTML/CSS/JS | `web/index.html` + `web/app.css` + `web/app.js` |
| 改样式要在 Python 字符串里找位置 | 直接编辑 CSS，编辑器有补全、diff 是行级 |

`webui.py` 从 886 行降到 ~700 行，只留路由与接口。静态资源走 `/static/*`，
带路径穿越防护（`..` 与越界路径一律 404）。

### 2. 账号、角色、登录页

| 能力 | 实现 | 验证 |
|---|---|---|
| 口令存储 | PBKDF2-HMAC-SHA256，20 万次迭代，每用户随机 salt | 同密码两用户 hash 不同（单测） |
| 登录态 | HMAC-SHA256 签名 cookie（HttpOnly + SameSite=Lax），7 天 | 改一个字符即失效（单测） |
| 过期控制 | cookie 内含 exp，过期即无效 | 过期 token 被拒（单测） |
| 角色 | `user` / `admin` 两级 | 普通用户访问 `/admin` → 403（实测） |
| 服务端可信 | 会话接口的 `user` 一律来自登录态，前端传什么不算 | `with_actor()`（单测） |
| 接口级鉴权 | `/api/admin`、`/api/users`、改密需 admin | 普通用户实测返回 403 |

实测（curl）：

```
未登录 /            → 302（跳 /login）
未登录 /api/meta    → 401
/login              → 200
/static/app.css     → 200
admin 登录          → {"ok":true,"user":"admin","role":"admin"}
student1 访问 /admin        → 403
student1 访问 /api/admin    → {"error":"需要管理员权限"}
伪造 cookie（改 user）      → 401
```

### 3. 多页面路由

```
/login   登录页（独立页面，独立 CSS）
/        面试页（需登录，未登录 302 到 /login?next=/）
/admin   后台：成长趋势 + 按用户统计 + 最近会话（可删）+ 账号列表（需 admin）
/static/*  静态资源
/api/*     接口（除 /api/health、/api/login、/api/logout 外均需登录）
```

### 4. 数据分家

| 文件 | 存什么 |
|---|---|
| `data/users.db` | 账号（口令哈希、角色、最后登录） |
| `data/sessions.db` | 面试会话快照 |
| `data/secret.key` | cookie 签名密钥（首次自动生成，权限 600） |
| `runs/events_*.jsonl` | 每场面试的事件账本 |

---

## 二、这一版踩的两个坑（都是实测暴露的）

### 坑 1：页面挡住了，接口没挡

给 `/admin` 页面加了角色检查，但 `/api/admin` 忘了加。
普通用户直接请求接口就拿到了**所有人的会话列表**——页面级权限是装饰品。

**修法**：`/api/admin`、`/api/users` 各自再拦一次。凡是"页面 + 接口"成对出现的地方，两处都要挡。

### 坑 2：登录态把"被操作对象"覆盖了

后台新建用户时，请求里的 `user` 被登录态覆盖成 `admin`，
于是"新建 student1"实际在新建 admin → 永远返回"用户名已存在"，界面还毫无提示。

**修法**：只有会话类接口（`/api/start`、`/api/answer`、`/api/end`）才用登录态覆盖 `user`；
账号管理接口的 `user` 是**操作对象**，不能被覆盖（`with_actor()`，4 条单测锁住）。

---

## 三、还差什么（不假装有）

| 缺口 | 影响 | 说明 |
|---|---|---|
| **Docker 从未 build 过** | 不能声称"一键部署" | 本机没有 Docker。Dockerfile/compose 写好了，但没有验证过就是没验证 |
| 无密码找回 / 无邮箱验证 | 忘记密码只能靠管理员重置 | 单机工具够用 |
| 无登录限流 | 可以被暴力猜密码 | 本地 127.0.0.1 风险低；公网必须先加 |
| 无 CSRF token | 依赖 SameSite=Lax | 足以挡住常规跨站表单提交，但不算完备 |
| 会话仍存在进程内存 | 多进程部署会各存各的 | SQLite 有快照可恢复，但实时态不共享 |
| 前端无构建链 | 没有组件化、没有类型检查 | 有意为之：零依赖双击即跑 |

---

## 四、本地怎么看

```powershell
python webui.py --port 8770          # 正常启动（需要登录）
# 首次启动会自动建 admin / admin123，并在控制台打印警告

python webui.py --port 8788 --token-login
# 本地演示/截图用：控制台会打印一个带签名 token 的登录链接，点开即登录
# 默认关闭；token 是 HMAC 签名的，伪造无效
```

截图（本轮实测产出）：
- `reports/login.png` 登录页
- `reports/admin_platform.png` 后台（趋势 / 按用户 / 会话 / 账号）

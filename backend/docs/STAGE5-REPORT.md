# 阶段 5 完成报告：全部功能收口

> 日期：2026-11-26
> 状态：**后端 + 前端 + RAG + 记忆 + 断点续跑 全部完成**

---

## 一、总览

| 层 | 规模 | 状态 |
|---|---|---|
| 后端 `backend/app` | 39 文件 / 4,300+ 行 | ✅ |
| 前端 `frontend/src` | 14 文件 / 1,800+ 行 | ✅ |
| 旧核心 `interview_agent/*.py` | 74 文件 / 12,113 行 | ✅ 零改动复用 |
| 数据库 | 10 业务表 + 4 checkpoint 表 | ✅ |
| 测试 | 旧 379 + 新 37 = **416 条通过** | ✅ |

---

## 二、这一轮做完的四件事

### 1. Postgres checkpointer（真断点续跑）

**实测通过**：

```
=== 先答 2 轮 ===
第1轮 verdict=partial
第2轮 verdict=partial

=== 杀掉服务（模拟崩溃）===
=== 重启服务 ===
=== 重启后继续答 ===
turn=3, verdict=partial, avg=2.6, weakest=depth
✓ 重启后能继续答：第 3 轮（不是从 0 开始）
```

**踩到的坑（值得记住）**：

`PostgresSaver.from_conn_string()` 返回的是**上下文管理器**。
第一版这样写：

```python
saver_cm = PostgresSaver.from_conn_string(dsn)
saver = saver_cm.__enter__()   # ← 表面能建表
saver.setup()
```

`health()` 报 ok，但一 `invoke` 就炸：

```
File "langgraph/checkpoint/postgres/__init__.py", line 242, in get_tuple
    with self._cursor() as cur:
```

原因：`__enter__` 拿到的 saver 依赖上下文里的连接，**生成器被 GC 后连接就断了**。
修法：自己持有连接 `psycopg.connect(dsn, autocommit=True)`，再 `PostgresSaver(conn)`。

**第二个坑**：checkpointer 持久化了图状态，但**运行时不持久化**。
重启后 `turn=2` 在 checkpointer 里，但 `registry` 里没有 `SessionRuntime`
→ 节点拿不到会话 → 降级 → 从 0 开始。

修法：新增 `core/recover.py`，从 PG + 图状态重建运行时，并把
`turn/phase/ladder_level/asked_qids/topic_counts` **回灌**进旧 session 的内部计数器。

### 2. 三层记忆

| 层 | 实现 | 测试 |
|---|---|---|
| 短期 | `memory.push_recent()` 只留最近 6 轮 | ✅ |
| 工作 | 旧 `memory.py` 事实抽取（已有） | ✅ 沿用 |
| 长期 | PG `candidate_profile` + 累加更新 | ✅ 4 条 |

长期记忆的读写时机：
- **写**：面试结束时**异步**（`update_profile_async`），不阻塞响应
- **读**：开场时 `profile_digest()` 生成"这是第 N 场，反复没讲清的是「失败方案」…"注入 plan 节点

**为什么累加而不是覆盖**：单场结论噪声大（可能恰好问到不熟的），3–5 场才稳定。
答好了计数会减一（弱项会"康复"）。

### 3. 接口快照比对

```
接口                     旧状态     新状态  说明
health               200     200
bank_stats           200     200
register             200     200  路径变了 /api/register → /api/auth/register
login                200     200  新增 access_token/expires_in，改为 JWT
me                   200     200  路径变了 /api/me → /api/auth/me
sessions_list        200     200
≠ me_anon              200     401  旧实机免登录返回 local；新实现要求 JWT
≠ bad_role             500     422  旧实现 500+LLMError；新实现 422+可选值列表
≠ answer_no_sid        200     404  旧实现 200+error；新实现 404

合计 9 项：状态相同 6 · 有意变更 3 · 失败 0
```

**三项变更都是已批准的设计改进**，写进 `docs/API-CONTRACT.md`。

### 4. Vue 3 前端（6 个页面）

```
frontend/src/
  api/        client.ts（拦截器） types.ts（类型） index.ts（按领域分组）
  stores/     auth.ts（登录态） session.ts（面试状态）
  router/     index.ts（守卫）
  views/      LoginView  InterviewView  ReportView  HistoryView  BankView  AdminView
  styles/     main.css
```

**构建结果**：

```
✓ built in 869ms
dist/assets/index-CqjpgEFW.js         874.27 kB │ gzip: 278.83 kB
dist/assets/ReportView-CYU5RIgT.js  1,120.82 kB │ gzip: 372.34 kB   ← ECharts
```

**联调实测**：

```
前端 http://localhost:5173/          → 200
代理 /api/health                     → 200（5 组件全绿）
代理 POST /api/auth/login            → 200 + JWT
```

---

## 三、前端三个关键设计

### ① access token 存内存，refresh 存 HttpOnly Cookie

```typescript
let accessToken = ''   // 内存变量，不落 localStorage
```

- **防 XSS**：页面注入脚本读不到内存变量（localStorage 能读）
- **刷新页面不丢**：`bootstrap()` 调 `/auth/refresh`，Cookie 自动带上，静默换新 access
- **代价**：要自己实现 refresh 逻辑（已做）

### ② 42004 静默刷新，用户无感

```typescript
if (code === 42004 && cfg && !cfg._retried) {
  cfg._retried = true
  await refreshToken()
  return http.request(cfg)   // 重发原请求
}
```

access 只有 15 分钟。如果每次过期都弹"登录已过期"，用户会疯。
并发请求同时过期时只发一次 refresh（`refreshing` Promise 去重），
否则 refresh 会被轮换多次而互相失效。

### ③ 路由守卫先恢复登录态再判断

```typescript
if (!auth.ready) await auth.bootstrap()
```

不这么做的话，用户直接访问 `/interview` 会看到"先渲染空白页 → 跳登录"的闪烁。

---

## 四、这一轮抓到并修掉的问题

| # | 问题 | 现象 | 性质 |
|---|---|---|---|
| 1 | `PostgresSaver.from_conn_string` 的连接被 GC | `invoke` 报 `_cursor()` 异常 | **库的用法陷阱** |
| 2 | checkpointer 只存图状态，不存运行时 | 重启后从第 0 轮开始 | **设计缺陷** |
| 3 | `_load_state` 只读内存字典 | 重启后读不到状态 | 实现错误 |
| 4 | TS 缺 `@/*` 别名 | 全部 import 报 TS2307 | 配置缺失 |
| 5 | 缺 `.vue` 类型声明 | `import X from './X.vue'` 报错 | 配置缺失 |
| 6 | placeholder 里用双引号 | TS1117 重复属性 | 语法细节 |
| 7 | PowerShell 字符串替换弄坏中文 | 22 行乱码 + 语法错误 | **工具陷阱** |

**第 7 条最浪费时间**：我用 PowerShell 的 `.Replace()` 改 Python 文件里的中文，
结果编码被破坏，`log.info("状态图已编译（checkpointer=%s）")` 变成
`log.info("鐘舵€佸浘宸茬紪璇戯紙checkpointer=%s锛?)`。

**教训**：改含中文的源文件**必须用 edit 工具**，不能用 PowerShell 字符串替换。
这不是第一次踩，之前就记录过，这次又忘了。

---

## 五、完整启动方式

```bash
# 1. 数据库（PostgreSQL + pgvector）
wsl -d Ubuntu -e bash -c "cd '/mnt/e/桌面/实习项目/interview_agent' && bash ops.sh db"

# 2. 题库（首次或换模型后）
bash ops.sh bank

# 3. 后端（detached，不随 shell 退出）
bash ops.sh api
#   → http://127.0.0.1:8080
#   → http://127.0.0.1:8080/docs  （自动生成的 OpenAPI 文档）

# 4. 前端
cd frontend && npm run dev
#   → http://localhost:5173

# 5. Ollama（宿主机，GPU 加速）
#   已配脚本，双击或：
OLLAMA_HOST=0.0.0.0:11800 ollama serve
```

**一屏看状态**：

```bash
bash ops.sh status
```

```
=== 组件状态 ===
  PostgreSQL : ok
  Ollama     : ok
  FastAPI    : ok

  数据：
     bank_questions  722 行（有向量 722）
     documents       1
     doc_chunks      2
     users           1
```

---

## 六、全量测试

```
旧测试（规则层，零依赖）     379 passed, 1 skipped
backend 测试（需 PG/Ollama）  37 passed
  ├─ 切片器                  9
  ├─ 检索阈值                 9
  ├─ 语义选题                 7
  ├─ 题库配额                 5
  └─ 三层记忆                 7
                            ─────
                            416 passed
```

**测试隔离**：`pytest.ini` 把 `testpaths` 限定为 `tests/`，
`backend/tests` 单独跑（它依赖 PG/Ollama，和规则层"零依赖单测"的要求冲突）。

---

## 七、与旧实现的最终对比

| 维度 | 旧版（11-24） | 新版（现在） |
|---|---|---|
| Web 框架 | 标准库 `http.server` | FastAPI + uvicorn |
| 前端 | 657 行原生 JS | Vue 3 + TS + Element Plus（6 页面） |
| 数据库 | SQLite（2 表） | PostgreSQL + pgvector（14 表） |
| 鉴权 | 手写 HMAC token | JWT 双 token + refresh 轮换 + 失败锁定 |
| 检索 | 无 | 纯语义 topK（4 索引，阈值分用途） |
| 记忆 | 单场内存 | 三层（短期/工作/长期） |
| 断点续跑 | ❌ 重启即丢 | ✅ Postgres checkpointer |
| 状态推进 | `session.step()` 同步函数 | LangGraph 状态图（5 节点） |
| 接口 | 23 个 | 30 个（+鉴权/文档/诊断） |
| 错误提示 | 200 + `{error}` | 40+ 条分类码 + 中文人话 |
| 测试 | 356 | 416 |

---

## 八、诚实清单：还没做的

| 项 | 说明 | 预估 |
|---|---|---|
| **语音（ASR/TTS）** | 你未确认要做，我也没排进计划 | +3 天 |
| **技术问答阶段的 RAG 上下文** | TECH_QA 阶段目前用题库题干，没用简历检索结果拼上下文 | 0.5 天 |
| **真实模型下的端到端** | 全程用 mock 测的（省 token）。真实模型路径之前验证过，但没跑完整场 | 0.5 天 |
| **SSE 真流式** | 现在是把整句切 3 字一帧推，不是模型逐 token | 1 天 |
| **Docker Compose 全栈** | 现在 PG 是容器，后端/前端还是本机进程 | 1 天 |
| **前端单测** | 只有构建验证，没写 vitest | 1 天 |
| **弱项话题的针对性出题** | 长期画像存了 weak_topics，但选题时还没用它加权 | 0.5 天 |

---

## 九、下一步建议（按价值排序）

1. **真实模型跑一场完整面试**（0.5 天）—— 验证端到端在真实模型下没问题，这是最重要的
2. **Docker Compose 全栈**（1 天）—— 简历里"一条命令起完整栈"才成立
3. **弱项话题加权**（0.5 天）—— 长期记忆真正产生价值
4. **前端 vitest**（1 天）—— 补前端测试
5. **语音**（3 天）—— 你之前没确认要做

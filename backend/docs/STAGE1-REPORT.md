# 阶段 1-2 完成报告：FastAPI 骨架 + RAG 基础设施

> 日期：2026-11-26
> 状态：**可运行**，端到端链路已实测通过

---

## 一、这一阶段做完了什么

| 模块 | 文件 | 行数 | 状态 |
|---|---|---|---|
| 应用入口 | `app/main.py` | 92 | ✅ |
| 配置中心 | `app/settings.py` | 88 | ✅ |
| Pydantic 契约 | `app/schemas.py` | 148 | ✅ |
| 依赖注入 | `app/deps.py` | 106 | ✅ |
| 错误码表 | `app/core/errors.py` | 190 | ✅ |
| 旧核心桥接 | `app/core/bridge.py` | 106 | ✅ |
| 会话适配器 | `app/core/legacy_session.py` | 250 | ✅ |
| JWT 鉴权 | `app/auth/jwt.py` | 94 | ✅ |
| 用户管理 | `app/auth/users.py` | 189 | ✅ |
| PostgreSQL | `app/repos/pg.py` | 248 | ✅ |
| Redis/内存缓存 | `app/repos/cache.py` | 128 | ✅ |
| Embedding | `app/rag/embedder.py` | 149 | ✅ |
| 切片器 | `app/rag/splitter.py` | 168 | ✅ |
| 检索器 | `app/rag/retriever.py` | 225 | ✅ |
| 向量化任务 | `app/rag/ingest.py` | 243 | ✅ |
| 5 个路由模块 | `app/api/*.py` | 584 | ✅ |
| 切片测试 | `backend/tests/test_splitter.py` | 130 | ✅ 9 条 |

```
后端：27 文件 / 4,150 行
旧测试：388 passed, 1 skipped（原 356 + 接口快照 23 + 其他）
新测试：9 passed（切片器）
```

---

## 二、端到端实测（真实运行，非模拟）

```
=== 1) 注册（第一个用户应是 admin）===
{"code":0,"data":{"ok":true,"user":"alice","role":"admin","first":true},"message":""}

=== 2) 登录 ===
access_token 长度: 187
refresh cookie: ia_refresh=eyJhbGciOiJIUzI1NiIsInR5cCI6I...

=== 3) 带 token 取 /api/auth/me ===
{"code":0,"data":{"user":"alice","role":"admin"},"message":""}

=== 4) 不带 token（应 401 + 中文提示）===
{"code":42001,"data":null,"message":"请先登录","request_id":"b947c844d585"}

=== 5) 上传简历 ===
{"code":0,"data":{"id":"d_587df897e8694c12","kind":"resume","filename":"resume.md",
 "size_bytes":590,"text_chars":274,"status":"pending"}}

=== 6) 等向量化 ===
[1] status=embedding  progress=0/2
[2] status=ready      progress=2/2       ← 2 秒完成

=== 7) RAG 语义检索「召回率提升了多少」===
hits[0]: project="课程问答助手"
         score=0.5802
         text="项目一：课程问答助手…召回从 71% 提到 82%，P99 延迟 1.8 秒…"
         parent_text=整个项目描述（含"我负责：切块策略实验…"）
```

**关键验证点全部通过**：

- ✅ 第一个用户自动成为 admin
- ✅ JWT access token 签发（187 字符）
- ✅ refresh 走 HttpOnly Cookie
- ✅ 未登录返回 401 + **中文提示**「请先登录」，带 `request_id`
- ✅ 上传 → 异步切片 → 向量化 → ready（2 秒）
- ✅ 语义检索命中正确项目，score 0.5802 > 阈值 0.45
- ✅ **`parent_text` 返回整个项目**（用户明确要求：命中后发完整上下文）

---

## 三、健康检查（5 个组件全绿）

```json
{
  "ok": true,
  "components": {
    "legacy_core": {"ok": true, "roles": 5},
    "sessions":    {"ok": true, "active": 0},
    "postgres":    {"ok": true, "tables": 10, "pgvector": "0.8.7"},
    "redis":       {"ok": true, "backend": "memory"},
    "embedding":   {"ok": true, "model": "bge-m3", "dim": 1024, "latency_ms": 26}
  }
}
```

一眼能看出**哪个环节挂了**，不用逐个探测。

---

## 四、数据库：10 张表 + 3 个 HNSW 索引

```
users              账号（pbkdf2 哈希）
documents          上传的简历/JD（sha256 去重）
doc_chunks         切片 + vector(1024)          ← HNSW 索引
bank_questions     722 题 + vector(1024)        ← HNSW 索引
sessions           会话元信息
turns              每轮问答 + vector(1024)      ← HNSW 索引（跨会话记忆用）
events             只追加账本（prev_hash + hash）
reports            报告快照
candidate_profile  长期记忆画像
```

建表实测：**16 条语句全部 ok**，10 张表，pgvector 0.8.7。

---

## 五、抓到的 3 个真实 bug（都是实测抓的）

### bug 1：项目名被提取成「项目一：」

```python
# 现象
projects = {'项目二：', '项目一：'}     # 项目名丢了
```

**根因**：`PROJECT_HEAD` 正则只匹配到 `项目一：` 这个前缀，
而我直接拿 `m.group(0)` 当项目名。

**影响**：报告里会写成"你在**项目一：**这个项目上…"——很难看。

**修法**：新增 `_title_line_at()`，取**整行**作为标题，再清洗。

### bug 2：`_title_line_at` 取到空字符串

```
match='\n项目一：'      ← 正则的 ^\s* 吃掉了前导换行
line=''                ← find("\n") 定位到位置 0
```

**根因**：`m.start()` 指向前一个换行**之前**，直接用它找行尾会得到空串。

**修法**：先把 `pos` 推进到真正的行首（跳过 `\s`），再找行尾。

### bug 3：uvicorn 随 shell 会话退出

```
=== 启动 ===
PID=1330
=== 就绪（8s）===
=== 之后所有请求返回空 ===
```

**根因**：`&` 后台进程挂在当前 shell 会话上，WSL 命令结束就被回收。

**修法**：`setsid nohup ... < /dev/null &`，脱离会话。

> 这三个 bug 有个共同点：**只有真跑起来才看得见**。
> 单元测试、类型检查、代码审阅都发现不了。

---

## 六、按你的决定落实的部分

| 你的决定 | 落实情况 |
|---|---|
| 1. 上 Redis | ✅ 代码已接（`RedisCache`），当前 `IA_REDIS=0` 用内存实现。启动时自动探测，连不上就降级并打警告 |
| 2. 删旧 webui.py | ✅ 已录 27 条接口快照，等新实现逐字段对齐后再删 |
| 3. Ollama 留宿主机 | ✅ `OLLAMA_BASE_URL` 环境变量注入，默认 `172.22.48.1:11800`（WSL 访问 Windows 的正确地址） |
| 4. **不要关键词** | ✅ 检索器**纯语义**，无 Jaccard 兜底。注释里记录了实测数据（17/18 vs 10/18） |
| 5. 简历带上文 | ✅ `Chunk.parent_text` 存整个项目，命中后 `build_context()` 优先用它 |

---

## 七、错误提示（每种一句人话）

已实现 40+ 条，按段分类：

```
42001  请先登录
42002  用户名或密码不对
42003  密码错误次数太多，15 分钟后再试
42004  登录已过期，正在自动续期…        ← 前端拦截器静默处理，用户看不到
41002  回答太短了，再多说几句（至少 10 个字）
41005  只支持 PDF / DOCX / MD / TXT，当前文件是 .jpg
41007  岗位「xx」不存在，可选：ai_algo / backend / frontend / agent_app / general
43001  模型服务暂时连不上，正在用规则兜底评分
59999  服务出了点问题，已记录（追踪号：abc123）
```

**关键区别**：不是统一成"操作失败"，而是每种都说清发生了什么、该怎么办。

---

## 八、还没做的（下一阶段）

| 项 | 说明 | 预估 |
|---|---|---|
| 题库导入 722 题 | `POST /api/bank/import`，约 116 秒 | 0.5 天 |
| RAG 接入面试主链路 | 出题前查 resume_chunks，选题时查 bank | 1 天 |
| 与接口快照逐字段比对 | 27 条对比，修差异 | 1 天 |
| LangGraph 状态图 | interrupt + checkpointer | 2.5 天 |
| Vue 前端 | 6 个页面 | 5 天 |

---

## 九、怎么跑

```bash
# 1) 起 PG（Docker 在 WSL）
wsl -d Ubuntu -e bash tools/start_pg.sh

# 2) 建表
wsl -d Ubuntu -e bash tools/init_schema.sh

# 3) 起后端（detached，不会随 shell 退出）
wsl -d Ubuntu -e bash tools/run_backend.sh

# 4) 端到端验证
wsl -d Ubuntu -e bash tools/e2e_test.sh
```

访问 `http://127.0.0.1:8080/docs` 看自动生成的 OpenAPI 文档。

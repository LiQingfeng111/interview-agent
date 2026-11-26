# 完整产品化改造计划（待过目）

> 编制日期：2026-11-26
> 原则：**现有 356 条测试与判定语义不动**；新能力以"新增层"方式叠加，可整体回滚。

---

## 零、先纠正我上一轮的说法

上一轮我答"为什么不做 RAG"时，讲的是**现状**（项目确实没有），
但没讲**这是不是缺陷**。结论是：

| 场景 | 需要 RAG 吗 | 说明 |
|---|---|---|
| 出题与判定的主链路 | **不需要** | 判定靠阈值+不变量，比向量检索更可审计 |
| 简历 / JD 定位到"该问哪个项目哪个点" | **需要** | 现在靠关键词匹配，长简历会漏 |
| 722 题题库选题 | **需要** | 现在是字符二元组 Jaccard（0.72），语义重复抓不到 |
| 跨会话"这个人上次哪里弱" | **需要** | 现在只有单场内存，无长期记忆 |
| 联网事实核对 | 已有，但是关键词搜索 | 可升级为"搜索 + 段落向量重排" |

所以 RAG 不是"漏了"，是**当时为了可审计性刻意不用**；
要做成产品，这四处是真正该补的。

---

## 一、现状基线（实测数字）

| 项 | 现状 | 位置 |
|---|---|---|
| 后端规模 | 12,113 行 / 74 文件 | `interview_agent/` |
| Web 框架 | 无（`http.server` 标准库） | `webui.py` |
| 前端 | 无框架，657 行原生 JS | `web/` |
| 接口 | 23 个 | `webui.py` |
| 数据库 | SQLite 两个文件（sessions 10 行 / users 7 行） | `data/` |
| 向量检索 | **无** | — |
| 记忆 | 单场内存 + `memory.py` 事实抽取 | `memory.py` |
| 测试 | 356 passed, 1 skipped | `tests/` |
| 可用 embedding API | **无（实测 relay `/v1/embeddings` 返回 404）** | 见 §4.1 |

### 环境实测（决定选型的硬约束）

```
Node      v24.16.0 / npm 11.13.0    ✔ 可装 Vue（npm registry 可达，vue 最新 3.5.46）
WSL       Python 3.12.3 / venv 可用  ✔
Ollama    已安装 0.40.18，未运行，无模型  ⚠
relay     649 个模型，但无 embedding 模型，/v1/embeddings → 404  ✘
pypi      连通  ✔（langgraph 1.2.24 / fastapi 0.165.0 / chromadb 1.5.9 / pgvector 0.5.1）
磁盘      C: 73.5 GB 可用，E: 170.1 GB 可用  ✔
```

---

## 二、目标架构

```
┌─────────────────────────────────────────────────────────┐
│  前端  Vue 3 + Vite + TS                                │
│  Pinia 状态 · Vue Router · Element Plus · SSE 流式       │
│  页面：登录 / 面试对话 / 报告 / 历史 / 题库诊断 / 后台     │
└───────────────────────┬─────────────────────────────────┘
                        │ HTTP + JSON   /   SSE(text/event-stream)
┌───────────────────────▼─────────────────────────────────┐
│  接入层  FastAPI + uvicorn                              │
│  JWT 鉴权 · RBAC · 限流 · CORS · 统一异常 · OpenAPI      │
├─────────────────────────────────────────────────────────┤
│  编排层  LangGraph                                      │
│  StateGraph：plan→probe→(interrupt)answer→evaluate→route│
│  Checkpointer（Postgres/Sqlite）→ 断点续跑、可回放        │
├─────────────────────────────────────────────────────────┤
│  能力层  LangChain                                       │
│  ChatPromptTemplate · OutputParser · Retriever · Memory  │
│  ┌──────────┬──────────┬──────────┬──────────┐          │
│  │ 向量检索  │ 联网检索  │ 记忆库    │ 模型网关  │          │
│  └──────────┴──────────┴──────────┴──────────┘          │
├─────────────────────────────────────────────────────────┤
│  规则层（不动）  core/                                  │
│  state_machine · probe_tree · invariants · ledger        │
│  evaluator(阈值) · phases(预算)                         │
├─────────────────────────────────────────────────────────┤
│  存储层                                                  │
│  PostgreSQL + pgvector（主）  |  SQLite + sqlite-vec（lite）│
│  Redis：会话缓存 / 限流 / 检索缓存 / 分布式锁              │
└─────────────────────────────────────────────────────────┘
```

**关键分界**：编排层与能力层可以换；**规则层一行不改**。
判定结果必须仍然可复现、可审计，这是这个项目唯一不可让渡的东西。

---

## 三、技术选型（含备选与不选的理由）

### 3.1 前端

| 组件 | 选择 | 版本 | 为什么 | 备选 |
|---|---|---|---|---|
| 框架 | **Vue 3** | 3.5.46 | 组合式 API + `<script setup>`，中文资料多 | React 19（不选：你已定 Vue） |
| 构建 | **Vite** | 7.x | 冷启动 <1s，HMR 快 | webpack（不选：慢） |
| 语言 | **TypeScript** | 5.x | 接口契约有类型，前后端对不齐时编译期报错 | JS（不选：接口 30+ 个，易错） |
| 状态 | **Pinia** | 3.x | Vue 官方推荐，比 Vuex 简洁 | 无状态库（不选：面试会话状态复杂） |
| 路由 | **Vue Router** | 4.x | 必备 | — |
| UI | **Element Plus** | 2.x | 表格/表单/上传/消息齐全，省 60% 样式工作 | Naive UI（同样可选，无强偏好） |
| 图表 | **ECharts** | 5.x | 报告页雷达图/趋势图 | Chart.js（不选：雷达图弱） |
| 流式 | **原生 EventSource / fetch-stream** | — | 逐字显示面试官提问 | socket.io（不选：过重） |
| HTTP | **axios** | 1.x | 拦截器统一加 JWT、统一错误提示 | fetch（不选：拦截器要手写） |

### 3.2 后端

| 组件 | 选择 | 版本 | 为什么 | 备选 |
|---|---|---|---|---|
| Web 框架 | **FastAPI** | 0.165.0 | 原生 async、SSE 友好、自动 OpenAPI | 保留 stdlib（不选：做不了 SSE 与并发） |
| ASGI | **uvicorn** | 0.54.0 | 标准 | — |
| 编排 | **LangGraph** | 1.2.24 | 状态图 + interrupt + checkpointer，正好对上"多轮面试+断点续跑" | 手写状态机（不选：要自己实现检查点） |
| 模型抽象 | **LangChain** | 1.5.2 + langchain-openai 1.8.5 | 已在用，扩到 Retriever/Memory | LlamaIndex（不选：重复） |
| 向量库（full） | **PostgreSQL 16 + pgvector** | pgvector 0.5.1 | 生产标准、和业务表同库、HNSW 索引 | Qdrant（不选：多一个服务） |
| 向量库（lite） | **sqlite-vec** | 0.1.9 | 零服务、单文件，保住"双击即跑" | ChromaDB（不选：又一套嵌入存储） |
| ORM | **SQLAlchemy 2.0 + Alembic** | — | 迁移可版本化（现在手写建表无法演进） | 裸 SQL（不选：30+ 表后难维护） |
| 缓存/中间件 | **Redis 7** | redis-py 8.2.0 | 会话缓存、限流、检索结果缓存、分布式锁 | 内存字典（不选：多进程失效） |
| 鉴权 | **PyJWT + passlib[bcrypt]** | — | 替换现在手写 HMAC token，支持过期/刷新 | 保留手写（不选：没有 refresh 机制） |
| 校验 | **Pydantic v2** | — | 请求/响应模型，和前端 TS 类型对齐 | — |
| 测试 | **pytest + httpx.AsyncClient** | — | 现有 356 条继续跑 | — |

### 3.3 嵌入模型（**这一项需要你拍板**，见 §4.1）

| 方案 | 成本 | 离线 | 质量 | 推荐 |
|---|---|---|---|---|
| **A. Ollama + bge-m3** | 磁盘 ~1.2 GB，无 API 费用 | ✔ | 中文最强开源之一 | **推荐** |
| B. Ollama + nomic-embed-text | 磁盘 ~300 MB | ✔ | 英文好，中文一般 | 轻量可选 |
| C. sentence-transformers 进程内 | 磁盘 ~2.5 GB（torch） | ✔ | 好，但要装 torch | 不选（重） |
| D. 远程 embedding API | 按量付费 | ✘ | 最好 | 不选（relay 没有） |

---

## 四、四个新增能力的详细设计

### 4.1 向量检索（RAG）——实测修订版

> **本节已按 2026-11-26 实测结果重写。原计划的阈值 0.80 是错的，见下。**

#### 实测：pgvector 跑通了

```
PostgreSQL 16.16 + pgvector 0.8.7（Docker: pgvector/pgvector:pg16，端口 5432）
CREATE EXTENSION vector          → OK
vector(4) 列 + HNSW cosine_ops   → OK
topK 检索实测：
  查询 [1,0,0,0]（"召回71%→82%"方向）
    → 召回从71%提到82%           sim = 1.0000
    → 召回率由 0.71 提升至 0.82  sim = 0.9948
    → 其余两条                    sim ≈ 0
  查询 [0,0,0,1]（谁都不像）
    WHERE sim >= 0.80            → 0 rows   ← 阈值过滤生效
```

#### 实测：bge-m3 装好了

```
模型    bge-m3:latest（Ollama，F16，566.70M 参数）
大小    1,157,672,605 字节 ≈ 1.16 GB
维度    1024（context 8192）
能力    ["embedding"]
端点    http://172.22.48.1:11800/api/embed   ← WSL 必须走宿主 IP
L2 模长 1.0000（已归一化，pgvector 内积=余弦）
```

**环境坑（已解决，写进文档备查）**：

| 坑 | 现象 | 解法 |
|---|---|---|
| 端口 11434 被 Windows 保留 | `bind: An attempt was made to access a socket in a way forbidden by its access permissions` | 落在系统排除段 11430–11529 内，改用 **11800** |
| WSL 访问 `127.0.0.1:11800` 不通 | `000`（连接被拒） | 必须用宿主 IP **172.22.48.1:11800** |
| PowerShell `Start-Job` 里跑 ollama | job 回收后进程被杀 | 用 `cmd /c set OLLAMA_HOST=... && ollama serve` 独立进程 |

#### 实测：bge-m3 的相似度分布（**这是最重要的发现**）

用 9 条人工构造的中英文样例实测：

| 类型 | 样例 | 相似度 |
|---|---|---|
| 正 · 同义不同表述 | 召回71%→82% ↔ recall 0.71→0.82 | **0.701** |
| 正 · 中英对照 | 多头注意力每头64维 ↔ d_k = 64 | **0.722** |
| 正 · 近义词 | 缓存击穿 ↔ 缓存穿透 | **0.724** |
| 正 · 同意味道 | baseline 是什么 ↔ 改造前指标是多少 | **0.469** |
| 正 · 同义不同术语 | 切块500字怎么定 ↔ chunk size 选 512 | **0.633** |
| 负 · 完全不同话题 | 召回 ↔ Redis 击穿 | 0.393 |
| 负 · 同话题不同考点 | 多头头数 ↔ 位置编码 sin/cos | 0.378 |
| 负 · 字面相近语义远 | 向量数据库选型 ↔ 数据清洗 | 0.445 |
| 负 · 同项目不同要素 | 失败方案 ↔ 上线时间 | 0.447 |

```
正样本 n=5  min=0.469  中位=0.701  max=0.724
负样本 n=4  min=0.378  中位=0.445  max=0.447
正最低 0.469 > 负最高 0.447  ← 可分，但间隔只有 0.022
阈值扫描最优 = 0.45（F1=1.000，正样本全召回、负样本全挡住）
```

**结论：我原计划的 `resume: 0.62 / bank: 0.80 / history: 0.55` 全部作废。**
bge-m3 的分布整体偏保守（同义句只有 0.70，不是直觉上的 0.9），按 0.80 写会把正样本全丢掉。

#### 修正后的阈值（按索引分别标定）

| 索引 | 阈值 | 理由 | 证据强度 |
|---|---|---|---|
| `resume_chunks` | **0.45** | 宁可宽：漏了会问错项目，多问几个点无伤 | 9 条样例标定 |
| `bank_questions` | **0.55** | 宁可严：误判会误杀题目 | 需补标定（见下） |
| `qa_history` | **0.45** | 只做提示，不参与判定 | 沿用 resume |
| 联网段落重排 | **0.50** | 只取最相关 3 段 | 需补标定 |

⚠️ **诚实说明**：9 条样例太少，且正负间隔只有 0.022，统计上不牢。
落地时必须先补标定集：

```
目标：每个索引 50 对人工标注（正 25 / 负 25）
方法：从 722 题 + 3 份真实简历里抽，人工判"是否同一考点"
验收：正样本召回 ≥ 0.90 时，负样本误放 ≤ 0.10
```

#### 要建几个索引（4 个，各自独立）

| 索引 | 文档单位 | 查什么 | 用在哪一步 |
|---|---|---|---|
| `resume_chunks` | 简历按"项目/经历"切块 | "这份简历里哪些段落和某考点相关" | 出题前定位项目 |
| `jd_chunks` | JD 按职责条目切块 | "这份岗位要求里哪些点还没问到" | 阶段推进决策 |
| `bank_questions` | 722 题（题干+评分要点） | "语义上最接近但考的不是同一件事的题" | 选题 & 去重 |
| `qa_history` | 历史问答对（跨会话） | "这人上次在哪些要素上答崩了" | 长期记忆 |

#### 切片策略（不同文档不同切法）

```
简历：按"项目标题/时间区间"切，每块 300-500 字，overlap 80 字
      项目内的 bullet 合并成一块，不按行切（按行切会丢上下文）
JD ：按"职责/要求"条目切，每条一块（天然就是语义单元）
题库：一题一块（题干 + 该题 3 个 points 拼接后向量化）
问答：一问一答一块，附 verdict 元数据
```

#### 检索时机的精确位置

```
一轮面试：
  ① 出题前  ─→ 查 resume_chunks + jd_chunks   ─→ 决定"问哪个项目哪个点"
  ② 选题时  ─→ 查 bank_questions             ─→ 排除语义重复题
  ③ 评分时  ─→ 查 qa_history（跨会话）        ─→ 知道这人历史弱点
  ④ 联网时  ─→ 搜索结果先关键词过滤，再用向量重排段落
```

#### 检索逻辑：实测结论 vs 你的判断（**结论：这轮数据不支持混合**）

你要求"用向量和关键词混合检索"。我做了两轮实测，如实汇报：

**第一轮**（9 对样例 + 200 题干扰，等权 RRF）：

```
vector    正样本 3/5 = 0.60   负样本误召 0/4   → 净收益 +0.60
keyword   正样本 1/5 = 0.20   负样本误召 1/4   → 净收益 -0.05
rrf       正样本 1/5 = 0.20   负样本误召 0/4   → 净收益 +0.20
```

**第二轮**（18 条查询分三类，1 目标 + 16 干扰）：

```
类型                向量      关键词     RRF     自适应加权
A 字面/数字/术语     6/6      6/6      6/6     6/6
B 同义改写          5/6      1/6      3/6     5/6
C 中英对照          6/6      0/6      1/6     6/6
─────────────────────────────────────────────────────
合计               17/18     7/18     10/18   17/18
```

**三个实测事实**：

1. **关键词在"字面/数字"类并没有赢**（6/6 vs 6/6）——bge-m3 对数字和术语也不差
2. **关键词在"中英对照"类完全失效**（0/6）——字符二元组跨语言没有共享 n-gram
3. **等权 RRF 比纯向量差**（10/18 vs 17/18）——用一个弱信号（7/18）去污染强信号（17/18）
4. 自适应加权能做到 17/18，**与纯向量持平**，但没有超过

**所以我不采纳等权混合。** 理由不是我保守，而是数据：在这批语料上，
关键词检索是净负担。如果硬上，会把原本能召回的 B/C 类查询压下去。

**但你的判断在工程上有一个真实价值，我准备这样用**：

```
主链路：纯语义 topK（17/18）
关键词只用在两个地方：
  ① 精确匹配兜底：查询里的数字/术语/代码标识符（"71%"、"d_k=64"、"P99"）
     直接按字符串命中过滤 → 分支增益，不参与统一排序
  ② embedding 服务不可用时降级（否则整个 RAG 挂掉）
```

这不是"混合检索"，是**"语义为主 + 字面精确匹配做旁路"**。
区别在于：旁路只增加、不参与打分，不会把向量排名压下去。

**如果你坚持要全量混合**，可以，但我们得先补标定集：
现在只有 18 条查询，统计上不足以否定一个方案。给我 100+ 条人工标注，
我按真实分布重新算权重。**现在的 18 条我如实说：不支持。**

#### 阈值（按索引分别标定）

| 索引 | 阈值 | 理由 |
|---|---|---|
| `resume_chunks` | **0.45** | 宁可宽：漏了会问错项目 |
| `bank_questions` | **0.55** | 宁可严：误判会误杀题目 |
| `qa_history` | **0.45** | 只做提示，不参与判定 |
| 联网段落重排 | **0.50** | 只取最相关 3 段 |

#### 简历上下文连贯

按你说的：**切片时带上文，检索到某块时把它所属项目的完整段落一起发给模型**。
不是只发命中那一块。

```
切片结构：
  chunk = { project: "课程问答助手", seq: 3, text: "切块 500 字…",
            parent_text: "整个「课程问答助手」项目的完整描述（约 800 字）" }

检索命中某 chunk 后：
  prompt 里插入 parent_text（不是 chunk.text 单独）
  → 模型看到的是完整项目上下文，不会因为切片而断章
```


### 4.2 记忆库（三层）

| 层 | 存什么 | 生命周期 | 存储 |
|---|---|---|---|
| **短期** | 最近 N 轮原始问答 | 单场面试内 | 内存 + LangGraph checkpointer |
| **工作** | 事实锚点（"召回 71%→82%"）、矛盾记录、要素覆盖 | 单场面试内 | `memory.py`（已有）+ 向量化后的 `qa_history` |
| **长期** | 跨会话：这个人在哪些话题反复失败、哪些要素总是回避 | 永久 | PostgreSQL `candidate_profile` + 向量 |

长期记忆的写入时机：**一场面试结束时**（batch 写，不打断主链路），
读取时机：**开场时**投影成一段"上次的薄弱点"塞进 planner 的 prompt。

```python
# 长期记忆的结构（不是原始对话，是提炼后的结论）
{
  "user": "alice",
  "weak_elements": {"失败方案": 4, "边界": 3},      # 出现次数
  "strong_topics": ["RAG 切块", "向量检索"],
  "weak_topics":   ["缓存一致性", "分布式锁"],
  "last_sessions": ["s_001", "s_007"],             # 可追溯到原始记录
}
```

### 4.3 联网检索（升级现有）

| 现状 | 升级后 |
|---|---|
| 关键词拼查询 | 查询改写（LLM 生成 2-3 个查询变体） |
| Bing/Serper 搜索 | 不变 |
| 三道质量闸 | 保留（junk / off_domain / quality） |
| 结果整段截 800 字塞 prompt | **按段落向量重排，只取最相关 3 段** |
| 无缓存 | Redis 缓存（TTL 24h，key=查询归一化后 hash） |

### 4.4 LangGraph 状态图

```python
class InterviewState(TypedDict):
    session_id: str
    user: str
    phase: str
    ladder_level: int
    project: str
    pending_question: str
    pending_element: str
    last_eval: dict
    facts: dict            # 事实锚点
    budget: dict           # 轮次/token/成本
    report: dict | None
```

```
        ┌──────────┐
        │  plan    │  Planner：从简历+JD 生成项目/要素计划
        └────┬─────┘
             ▼
        ┌──────────┐
        │  probe   │  Interviewer：出题（含 RAG 检索、记忆注入）
        └────┬─────┘
             ▼
      ╔══════════╗
      ║ interrupt ║  ← LangGraph interrupt()：等候选人回答（HTTP 请求到）
      ╚════┬═════╝
           ▼
        ┌──────────┐
        │ evaluate │  Evaluator：调模型评分 → 规则规整 → 不变量
        └────┬─────┘
             ▼
        ┌──────────┐
        │  route   │  StateMachine：升木梯/换阶段/收尾（纯规则，不调模型）
        └────┬─────┘
       ┌─────┴─────┐
       ▼           ▼
   (继续)       (结束)
       │           ▼
       │      ┌──────────┐
       │      │  report  │ 生成报告 + 写长期记忆
       │      └──────────┘
       └──→ probe
```

**为什么用 `interrupt()` 而不是自己存状态**：
候选人可能在两轮之间关掉浏览器、隔天再来。LangGraph 的 checkpointer 把整个 state 落库，
下次 `Command(resume=answer)` 就能从断点继续——这正是现在 Python 版缺的
（现在重启服务会话就丢，返回"会话不存在"）。

**规则层不搬进图里**：`route` 节点内部调 `state_machine` 的现有方法，
一行逻辑都不重写。这样 356 条测试仍然有效。

---

## 五、数据库设计（新增/变更）

### 5.1 迁移到 PostgreSQL（full profile）

现有 SQLite 两张表 → 拆成规范化结构：

| 表 | 用途 | 关键列 |
|---|---|---|
| `users` | 账号 | `id, email, pwd_hash, role, created_at` |
| `sessions` | 面试场次 | `id, user_id, role, phase, status, created_at, finished_at` |
| `turns` | 每轮问答 | `id, session_id, seq, question, answer, verdict, score, tokens, cost` |
| `events` | 只追加账本（**已有，保留**） | `id, session_id, seq, type, payload, prev_hash, hash` |
| `reports` | 报告快照 | `id, session_id, json, created_at` |
| `documents` | 上传的简历/JD | `id, user_id, kind, filename, sha256, text, created_at` |
| `doc_chunks` | 切片 | `id, document_id, seq, text, tsv`（tsv 供 BM25） |
| `bank_questions` | 题库（从 JSONL 导入） | `id, role, topic, level, question, points, tags` |
| `candidate_profile` | 长期记忆 | `user_id, weak_elements jsonb, weak_topics jsonb, updated_at` |

### 5.2 向量列（pgvector）

```sql
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE doc_chunks       ADD COLUMN embedding vector(1024);
ALTER TABLE bank_questions   ADD COLUMN embedding vector(1024);
ALTER TABLE turns            ADD COLUMN embedding vector(1024);  -- 问答历史

-- HNSW：查询快，写入稍慢；109 万条以内都够用
CREATE INDEX ON doc_chunks     USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON bank_questions USING hnsw (embedding vector_cosine_ops);
```

维度 1024 对应 bge-m3；换模型要改维度并重建索引（写进迁移脚本注释）。

### 5.3 lite profile —— **已取消（按你的决定）**

你已明确「数据库用 PostgreSQL，不是支持向量吗」。
所以：**只做 PostgreSQL + pgvector，不做 SQLite 双 profile。**

代价要认：`docker compose up` 成为硬前置，简历里"零依赖、双击即跑"这句话要改。
改成：**"一条 docker compose 命令起完整栈（PG+pgvector / Redis / 后端 / 前端）"**。

唯一保留的降级：embedding 服务不可用时，检索退化为关键词（见 §4.1 兜底），
数据库不做降级。


### 5.4 中间件（Redis）

| 用途 | key 设计 | TTL |
|---|---|---|
| 会话热态缓存 | `sess:{session_id}` | 30 min（滑动） |
| 检索结果缓存 | `rag:{index}:{sha1(query)}` | 24 h |
| 联网搜索结果 | `web:{sha1(query)}` | 24 h |
| 限流 | `rl:{user}:{window}` | 60 s |
| 分布式锁（防重复提交） | `lock:turn:{session_id}` | 30 s |

---

## 六、接口契约变更（30 个）

### 6.1 新增

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/auth/login` | 登录 → access(15m) + refresh(HttpOnly Cookie) |
| POST | `/api/auth/refresh` | 换 access |
| POST | `/api/auth/logout` | 作废 refresh |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/documents/upload` | 上传简历/JD → 解析 → 切片 → 向量化（异步任务，返回 task_id） |
| GET | `/api/documents/{id}/status` | 切片/向量化进度 |
| POST | `/api/sessions` | 开一场（返回 session_id + 第一问） |
| POST | `/api/sessions/{id}/answer` | 交一轮 |
| GET | `/api/sessions/{id}/stream` | **SSE**：逐字推送下一问 |
| POST | `/api/sessions/{id}/finish` | 结束 |
| GET | `/api/sessions/{id}/report` | 报告 |
| GET | `/api/sessions` | 历史列表（分页/筛选） |
| GET | `/api/profile` | 长期记忆画像 |
| GET | `/api/bank/search` | 题库语义检索（诊断用） |
| GET | `/api/rag/search` | RAG 诊断：给定 query 看召回了什么、分数多少 |

### 6.2 保留但重构

`/api/meta` `/api/bank` `/api/health` `/api/history` `/api/trend` `/api/admin*` `/api/users`

### 6.3 破坏性变更（要提前告诉你）

| 变化 | 影响 |
|---|---|
| 请求体 `sid` → 路径参数 `{id}` | 前端全部改写（本来就要重写） |
| 返回结构统一包 `{code, data, message}` | Python 旧前端失效（已废弃） |
| 鉴权从 Cookie 改为 `Authorization: Bearer` | 需要前端拦截器 |

---

## 七、目录结构（改造后）

```
interview_agent/
  backend/
    app/
      main.py                 FastAPI 入口
      api/                    v1 路由（auth/sessions/documents/bank/diagnostics）
      core/                   【不动】state_machine, probe_tree, invariants, ledger, phases, evaluator
      graph/                  LangGraph：state.py, nodes/, builder.py, checkpointer.py
      chains/                 LangChain：prompts/, parsers/, memory/
      rag/                    embedder.py, splitter.py, indexer.py, retriever.py, rerank.py
      retrieve/               web.py（升级版联网）, quality.py（三道闸）
      repos/                  Repository 接口 + pg/sqlite 两套实现
      models/                 SQLAlchemy 模型 + Alembic 迁移
      auth/                   jwt.py, rbac.py, deps.py
      tasks/                  异步任务（文档向量化）
    tests/                    现有 356 条 + 新增
    alembic/                  迁移脚本
    pyproject.toml
  frontend/
    src/
      views/                  Login, Interview, Report, History, Bank, Admin
      components/             ChatBubble, ScoreCard, RadarChart, UploadResume
      stores/                 auth.ts, session.ts, report.ts
      api/                    client.ts, sse.ts, types.ts
      router/
    vite.config.ts
  infra/
    docker-compose.yml        postgres:16 + pgvector, redis:7
    init.sql
  docs/
```

---

## 八、分阶段实施（每阶段可独立验收与回滚）

| # | 阶段 | 内容 | 验收标准 | 预估 |
|---|---|---|---|---|
| **1** | 地基 | FastAPI 骨架、Pydantic 模型、现有规则层挂进来、356 条测试跑通 | `pytest` 全绿；新旧接口响应逐字段一致 | 2 天 |
| **2** | 存储 | PG + Alembic 迁移、Repository 抽象、lite/full 双 profile、Redis 接入 | 同一套测试在 lite 与 full 下都过 | 2 天 |
| **3** | 鉴权 | JWT + refresh、RBAC、限流、CORS | 无 token → 401；越权 → 403；单测覆盖 | 1.5 天 |
| **4** | Embedding | Ollama 装 bge-m3、`Embedder` 端口、批量向量化脚本、维度校验 | "召回 71%→82%" 与 "recall 从 0.71 提到 0.82" 余弦 > 0.85 | 2 天 |
| **5** | RAG | 切片器、四索引、混合检索（BM25+向量+RRF+重排）、四道阈值、诊断接口 | 4 个索引各 20 条人工标注用例，recall@5 ≥ 0.8 | 3 天 |
| **6** | LangGraph | StateGraph、interrupt、checkpointer、SSE 流式 | 杀掉进程重启，`Command(resume)` 能继续答；同轮 state 一致 | 2.5 天 |
| **7** | 记忆 | 三层记忆、长期画像写入/读取 | 第二场面试能读到第一场的薄弱点 | 1.5 天 |
| **8** | 前端 | Vue 3 + TS 全套页面、SSE、上传、图表 | 端到端走通：登录→上传→面试→报告→历史 | 5 天 |
| **9** | 收口 | 文档、OpenAPI、Docker Compose 一键起、演示脚本 | `docker compose up` 后浏览器能跑 | 1.5 天 |

**合计 ≈ 21 个工作日**（不含调模型效果的时间）。

### 每阶段的回滚方式

| 阶段 | 回滚 |
|---|---|
| 1-3 | 旧 `webui.py` 保留不动，新 `backend/` 独立目录，不行就删 |
| 4-5 | RAG 能力开关 `IA_RAG=on/off`，off 时退回关键词 |
| 6 | `IA_GRAPH=on/off`，off 时用现有 `session.step()` |
| 7 | 记忆开关，off 时无跨会话 |
| 8 | 前端独立构建，旧 `web/` 保留 |

---

## 九、风险与明确不做的事

### 风险

| 风险 | 影响 | 对策 |
|---|---|---|
| **relay 无 embedding** | RAG 无法用 API 实现 | 已定方案：Ollama + bge-m3（需先装，约 1.2 GB） |
| 迁移到 PG 后"零依赖"卖点受损 | 简历那句"零依赖"要改 | 保留 lite profile，表述改为"双 profile" |
| LangGraph 版本较新（1.2.24） | API 可能不稳定 | 锁版本；先做 spike 验证 interrupt+checkpointer |
| 21 天工作量 | 时间压力 | 每阶段独立可交付，可随时停在某阶段 |
| 向量化 722 题耗时 | 首次建索引慢 | 批量脚本 + 断点续跑 + 缓存 |
| 判定语义被稀释 | 项目核心价值下降 | **规则层零修改**，用回归测试锁死 |

### 明确不做（这轮）

| 项 | 理由 |
|---|---|
| 语音（ASR/TTS） | 你未确认，且需要额外模型；可放二期 |
| 多租户 / 计费 | 产品化后期才需要 |
| K8s 部署 | Docker Compose 足够 |
| 移动端 App | 响应式 Web 已覆盖 |

---

## 十、已定事项与仍需你确认

### 已定（你已拍板，不再询问）

| # | 决策 | 结论 | 实测影响 |
|---|---|---|---|
| 1 | **Embedding** | **加入，用 bge-m3（Ollama）** | 已装好，1024 维，1.16 GB |
| 2 | **检索引擎** | **一律语义检索取 topK**，不做 BM25+向量混合 | 阈值改为 0.45（实测标定） |
| 3 | **简历连贯性** | **切片带上文，命中后发完整项目段落** | 切片结构含 `parent_text` |
| 4 | **数据库** | **PostgreSQL + pgvector**，不做 SQLite 双 profile | Docker 成硬前置 |
| 5 | **Redis** | **上**（进 compose，`redis:7` :6379） | +0.5 天 |
| 6 | **旧 `webui.py`** | **删**（先录接口快照，比对通过后再删） | 无退路，靠快照兜行为一致 |
| 7 | **Ollama 部署** | **留宿主机，不进 compose**（不装 nvidia-container-toolkit） | 保住 GPU 3–5 倍加速 |

### 实测已完成的部分（不是计划，是已跑通）

```
✔ Docker 可用（WSL，daemon OK）
✔ pgvector/pgvector:pg16 镜像已拉取
✔ PostgreSQL 16.16 + pgvector 0.8.7 容器已起（端口 5432，库 interview_agent）
✔ vector 扩展 + vector(1024) 列 + HNSW cosine 索引实测可用
✔ topK 语义检索实测：同义句 sim=0.9948，无关句 sim≈0，阈值过滤生效
✔ bge-m3 已下载（1.16 GB），1024 维，L2 已归一化
✔ 阈值标定跑完：建议 0.45（正样本全召回、负样本全挡住）
✔ 性能实测：单条 0.277s / 批量 10 条 1.608s（161ms/条）/ 722 题约 116s
✔ Ollama 跑在显存里（size_vram 689MB = 模型总量），未退化到 CPU
```

### Ollama 留宿主机的连通性设计

三个视角三个地址，**不硬编码，用一个环境变量注入**：

| 谁访问 Ollama | 用哪个地址 | 说明 |
|---|---|---|
| Windows 侧脚本 / 浏览器 | `http://127.0.0.1:11800` | 同机 |
| WSL 侧进程 | `http://172.22.48.1:11800` | 宿主 IP，`127.0.0.1` 不通（实测 000） |
| Docker 容器内 | `http://host.docker.internal:11800` | 现有 compose 已有 `extra_hosts` 映射 |

```yaml
# docker-compose.yml 片段（复用现有的 extra_hosts）
extra_hosts:
  - "host.docker.internal:host-gateway"
environment:
  OLLAMA_BASE_URL: "${OLLAMA_BASE_URL:-http://host.docker.internal:11800}"
```

### 删除旧 webui.py 的安全流程

```
1. 录快照：现有 23 个接口的真实响应 → tests/golden/api_responses.json
2. 写新实现：FastAPI，逐字段对齐快照
3. 比对通过：pytest 断言全绿
4. 才删 webui.py
```

一并清理的依赖（删除前给你清单确认）：

| 项 | 说明 |
|---|---|
| `webui.py` | 本体 |
| `web/`（6 文件） | 前端换成 Vue |
| `6-start-webui.bat` / `7-start-background.bat` / `8-stop-background.bat` | 启动脚本 |
| 测试里引用 `webui` 的用例 | 需改指向新接口 |

### 目标启动方式（5 条手动命令 → 2 条）

```bash
docker compose up -d       # postgres + redis + backend + frontend
ollama serve               # 宿主机，GPU 加速（会用脚本包好，双击即起）
```

### 仍需你确认：无

所有决策已明确，可以开工。

---

## 十一、下一步（等你确认 A/B/C 后立即开工）

**阶段 1**（2 天）：FastAPI 骨架 + Pydantic 模型 + 现有规则层挂进来
**验收标准**：`pytest` 356 条全绿；新 `/api/sessions` 与旧 `/api/start` 响应逐字段一致

开工前我会先做一件事：把现有 23 个接口的**真实响应**录成快照（像提示词黄金样本那样），
后面每改一处都与快照比对，确保重构不改变行为。

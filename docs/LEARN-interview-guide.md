# 从零学 interview-guide（3543★）：小白版完整手册

写给"完全不懂 Java / TypeScript / YAML / Lua / Redis / 数据库"的人。
按顺序读，每章都能独立看懂。所有代码片段都是**仓库里的真实内容**。

---

## 第 0 章 先说清楚：学它你能得到什么

| 你想要的 | 这个项目能给你 |
|---|---|
| 看懂一个"真实项目"长什么样 | 687 个文件、280 个 Java 文件、51+51 个前端文件、10 个数据库迁移脚本 |
| 学会"平台"是怎么搭的 | 前端 + 后端 + 数据库 + 缓存队列 + 对象存储 + 向量库，六个组件协同 |
| 面试能讲的素材 | Skill 驱动出题、统一评估引擎、Redis Lua 限流、任务恢复与 fencing、SSE/WebSocket |
| 判断它哪里不如别的项目 | 它没有"判定可信度"机制（阈值/校准/可复现），这是我们的差异点 |

### 五天的学习路线（每天 2 小时）

```
第 1 天：名词 + 全景（第 1、2 章）——不写代码，只建立地图
第 2 天：看数据长什么样（第 3.1 章 12 张表）——理解"平台=围绕数据的流程"
第 3 天：看 AI 怎么被约束（第 3.2/3.3 章 Lua 与提示词）——理解"提示词是配置，评分靠规则"
第 4 天：看六条业务流（第 4 章）+ 概念小课堂（第 5 章）
第 5 天：读代码（第 7 章）+ 自测 20 题（第 8 章）
```

---

## 第 1 章 二十个名词，每个两行

| 名词 | 是什么 | 在他项目里干什么 |
|---|---|---|
| **Java** | 编程语言（编译型、强类型） | 全部后端逻辑，280 个 `.java` 文件 |
| **Spring Boot** | Java 的 Web 框架 | 让 Java 能接收 HTTP 请求、连数据库 |
| **Spring AI** | Spring 的 AI 集成层 | 统一调各家大模型 + 强制结构化输出 |
| **TypeScript** | JavaScript 的加强版（有类型） | 前端页面逻辑，要编译成 JS 才能在浏览器跑 |
| **React** | 前端界面库 | 把页面拆成组件，数据变化自动重绘 |
| **YAML** | 配置文件格式（缩进表层级） | `application.yml`、`docker-compose.yml` |
| **Markdown** | 纯文本 + 少量符号的排版格式 | `skills/*.md` 定义面试方向、`prompts/*.st` 提示词 |
| **Lua** | 很小的嵌入式脚本语言 | `rate_limit_single.lua` 做原子限流 |
| **Redis** | 内存键值数据库 | 缓存 + 队列（Stream）+ 限流计数 |
| **PostgreSQL** | 关系型数据库（C 写的服务进程） | 存简历、会话、题库、日程；带 pgvector 扩展 |
| **pgvector** | PostgreSQL 的向量扩展 | 存文本向量，支持"按语义找相似" |
| **Flyway** | 数据库迁移工具 | 用带编号的 SQL 文件管理表结构版本 |
| **Docker** | 容器平台 | 一条命令起 PG/Redis/存储/后端/前端 |
| **REST API** | 一种"用 HTTP 操作数据"的约定 | 前端 `/api/interview/...` 调后端 |
| **JSON** | 数据交换格式（键值对 + 数组） | 前后端传数据、模型输出结构 |
| **SSE** | 服务端单向流式推送（HTTP 长响应） | 知识库问答的打字机效果 |
| **WebSocket** | 双向长连接 | 语音面试实时传音频 |
| **Redis Stream** | Redis 里的消息队列 | 简历分析、评估等长任务异步化 |
| **限流** | 限制单位时间请求次数 | 防刷接口 |
| **异步** | 请求先返回、任务后台做 | 简历解析/评估不阻塞用户 |

---

## 第 2 章 系统全景：一个请求怎么走完

```
┌──────────────┐        HTTP/JSON        ┌───────────────────────────────┐
│  浏览器       │ ──────────────────────> │  Spring Boot 后端              │
│  React+TS    │ <────────────────────── │  Controller → Service → Repo   │
└──────────────┘                         └───────┬───────────┬───────────┘
      │  WebSocket（语音）                       │           │
      └─────────────────────────────────────────>│           │
                                                 │           │
                        ┌────────────────────────┘           └──────────────┐
                        ▼                                                   ▼
              ┌──────────────────┐                              ┌────────────────────┐
              │ PostgreSQL       │                              │ Redis              │
              │ 12 张表 + 向量   │                              │ 缓存/队列/限流计数  │
              └──────────────────┘                              └────────────────────┘
                        ▲                                                   │
                        │ 读写                                              │ 拉取任务
                        │                                                   ▼
              ┌──────────────────┐                              ┌────────────────────┐
              │ RustFS / MinIO   │                              │ 消费者线程          │
              │ 简历/知识库文件   │                              │ 解析、评估、向量化   │
              └──────────────────┘                              └─────────┬──────────┘
                                                                          │ 调模型
                                                                          ▼
                                                             ┌─────────────────────┐
                                                             │ 大模型（千问/DeepSeek │
                                                             │ /Kimi/本地 LM Studio）│
                                                             └─────────────────────┘
```

**读法**：实线是同步（用户等结果），虚线后面那段是异步（用户不等，后台慢慢算）。

---

## 第 3 章 六个真实文件精读

### 3.1 数据库里有哪 12 张表（真实 schema）

来自 `db/migration/V1__init_schema.sql`，先建三个扩展：

```sql
CREATE EXTENSION IF NOT EXISTS hstore;        -- 键值类型
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- 生成 UUID
CREATE EXTENSION IF NOT EXISTS vector;        -- pgvector：向量列
```

然后 12 张表，按业务分组：

| 表 | 干什么 | 关键列（真实） |
|---|---|---|
| `resumes` | 简历文件 | `file_hash`（唯一，去重用）、`analyze_status`（PENDING/PROCESSING/COMPLETED/FAILED） |
| `resume_analyses` | 简历分析结果 | 六个分项分：`content_score`/`structure_score`/`project_score`/`skill_match_score`/`expression_score`/`overall_score` |
| `interview_sessions` | 文字面试会话 | `status`（CREATED/IN_PROGRESS/COMPLETED/EVALUATED）、`evaluate_status`、`questions_json`、`skill_id` |
| `interview_answers` | 每道题的回答 | `session_id + question_index` 唯一约束、`score`、`key_points_json` |
| `knowledge_bases` | 知识库文件 | `vector_status`、`chunk_count`、`file_hash` |
| `knowledge_base_questions` | 题库 | `status`（DRAFT/ACTIVE/ARCHIVED/**STALE**）、`scoring_rubric`、`follow_ups_json` |
| `vector_store` | 向量表 | `embedding vector(1024)` + HNSW 索引 |
| `rag_chat_sessions` / `rag_chat_messages` | 知识库问答会话 | `message_order`、`type`（USER/ASSISTANT） |
| `rag_session_knowledge_bases` | 会话↔知识库多对多 | 联合主键 |
| `llm_provider_config` | 模型配置 | **`api_key_ciphertext` + `api_key_nonce`（加密存）** |
| `llm_global_setting` | 默认用哪个模型 | `default_chat_provider_id`、`default_embedding_provider_id` |
| `interview_schedule` | 面试日程 | `status`（PENDING/COMPLETED/CANCELLED/RESCHEDULED） |
| `voice_interview_*` 三张 | 语音面试 | `phase`（INTRO/TECH/PROJECT/HR/COMPLETED）、`paused_at`/`resumed_at` |

**两个值得学的细节**：

```sql
-- ① 唯一约束防重复简历（同一个文件不重复分析）
CONSTRAINT idx_resume_hash UNIQUE (file_hash)

-- ② 向量检索索引（HNSW 是近似最近邻算法，比全表扫描快几个数量级）
CREATE INDEX spring_ai_vector_index
  ON vector_store USING hnsw (embedding public.vector_cosine_ops);
```

**为什么表里塞 JSON 文本**（`questions_json`、`follow_ups_json`）：
题目结构复杂又常变，拆成关系表要很多 join，塞成 JSON 字段更省事。代价是查不了内部字段。

### 3.2 限流脚本：为什么"先检查、后扣减"（真实 Lua 全文精读）

```lua
-- 第一阶段：只读取并计算可用令牌。任一维度不满足时，不修改任何限流状态。
for i = 1, rule_count do
    local current_val = tonumber(redis.call("get", value_key)) or max_tokens
    ...
    if current_val < permits then
        return -i          -- ★ i 表示"第 i 条规则不够"，直接返回，不扣减
    end
end

-- 第二阶段：所有维度都通过后再统一扣减。
for i = 1, rule_count do
    redis.call("zadd", permits_key, now_ms, permit_record)
    redis.call("set", value_key, current_val - permits)
end
return 1
```

**它解决什么**：一个接口可能同时挂几条规则（比如"每 IP 60 次/分钟" + "每用户 10 次/分钟"）。
如果逐条检查逐条扣减，第二条不通过时第一条已经被扣掉了——用户没被放行，配额却少了。

**两阶段提交**：先全部检查，全部通过才扣减。**这就是"原子"的实际含义**。
返回值也有讲究：`-i` 告诉上层是哪条规则拒绝的（便于返回不同提示），`1` 表示放行。

### 3.3 提示词：评分不是"感觉"，是写死的权重表（真实内容）

`prompts/interview-evaluation-system.st`：

```
# Evaluation Dimensions (评估维度)
| 维度 | 权重 | 评估标准 |
| 准确性 | 40% | 技术概念是否描述正确，无事实性错误 |
| 完整性 | 20% | 是否覆盖核心知识点 |
| 深度   | 25% | 是否触及底层源码、并发模型、性能优化 |
| 表达   | 15% | 逻辑是否严密、条理 |

# Scoring Rubric (评分标准)
| 90-100 | 优秀 | 源码级理解… |
| 75-89  | 良好 | 概念正确完整… |
| 60-74  | 及格 | 停留在表面… |
| 40-59  | 不及格 | 存在明显技术错误… |
| 0-39   | 较差 | 答非所问… |

# Constraints
- `overallScore` 应为各题得分的加权平均值
- **无效回答必须给 0 分**：回答"不知道""忘记了""跳过"等，该题分数必须为 0
- **追问评分规则**：标注（追问，针对 questionIndex=N）的题要结合主问题判断一致性
- **questionIndex 必须原样返回**，不得重新编号
```

**这三条约束解决的是真实的老毛病**：
① 模型爱给"不知道"打同情分 → 明确写 0 分；
② 追问被当独立题乱评 → 规定要与主问题比对；
③ 模型重排题号导致对不上答案 → 要求原样返回。

`prompts/interview-question-skill-system.st`（出题提示词）：

```
1. 技术链条追问："使用经验 → 核心原理 → 边界/优化"
2. 难度分布：基础 30% / 进阶 50% / 专家 20%
- **数量硬性要求**：questions 数组长度必须严格等于 questionCount，一个不能少一个不能多
```

**为什么要写"一个不能少"**：模型经常少生成一道题，导致面试轮次不够。写死数量是最省事的修法。

### 3.4 Skill：面试方向就是一个 Markdown 目录（真实 11 个）

```
resources/skills/
├── _shared/references/        ← 共用参考（algorithm-data-structures、browser、database、
│                                db-design、design-pattern、distributed …）
├── java-backend/
├── java-backend-tencent/
├── ali-backend/
├── bytedance-backend/
├── python-backend/
├── frontend/
├── algorithm/
├── system-design/
├── test-development/
└── ai-agent-dev/
```

每个目录里是 `SKILL.md`：考察范围、难度分布、参考题库。
**加一个新方向 = 加一个文件夹，不写一行 Java。** 这是"用文件扩展系统"的典型做法。

### 3.5 后端分层：一个模块里有什么（真实文件名）

```
modules/interview/
├── InterviewController.java       ← 接 HTTP
├── service/
│   ├── InterviewSessionService.java      ← 开会话/交卷
│   ├── InterviewQuestionService.java     ← 出题/去重/追问
│   ├── AnswerEvaluationService.java      ← 单题评估
│   ├── InterviewHistoryService.java      ← 历史与趋势
│   ├── InterviewPersistenceService.java  ← 落库
│   └── InterviewQuestionProperties.java  ← 配置项
├── listener/
│   ├── EvaluateStreamProducer.java       ← 投任务
│   └── EvaluateStreamConsumer.java       ← 跑任务
├── model/     ← 实体（Entity）与传输对象（DTO）分开放
├── repository/← 数据访问
└── skill/     ← 方向的加载与查询
```

**分层的好处**：换数据库只改 `repository`；换出题逻辑只改 `service`；接口字段变更只改 `model/*DTO`。

### 3.6 前端页面 ↔ 后端接口（真实 16 个页面）

| 页面文件 | 干什么 | 对应后端 |
|---|---|---|
| `UploadPage.tsx` | 上传简历 | 简历上传 + 分析状态查询 |
| `ResumeDetailPage.tsx` | 看分析报告 | 简历详情 |
| `InterviewHubPage.tsx` | 面试中心（选文字/语音） | 会话列表 |
| `InterviewPage.tsx` | 文字面试答题 | 开始/交卷/下一题 |
| `InterviewHistoryPage.tsx` | 历史与趋势 | 历史统计 |
| `InterviewSchedulePage.tsx` | 面试日程 | 日程 CRUD |
| `KnowledgeBaseUploadPage.tsx` | 传知识库 | 上传 + 向量化状态 |
| `KnowledgeBaseQueryPage.tsx` | 知识库问答（**SSE**） | 流式问答 |
| `KnowledgeBaseInterviewQuestionsPage.tsx` | 题库维护 | 题目 CRUD |
| `VoiceInterviewPage.tsx` | 语音面试（**WebSocket**） | 实时对话 |
| `SettingsPage.tsx` | 模型与语音配置 | Provider 配置 |

---

## 第 4 章 六条业务流程（初学版）

### 流程 A：上传简历 → 得到分析

```
你点上传 → 后端存文件到对象存储 → 记一条 resumes（状态 PENDING）
→ 立刻返回"已收到"（不等分析！）
→ 后台消费者拉任务 → Tika 解析文本 → 调模型 → 写 resume_analyses
→ 状态改 COMPLETED → 前端轮询看到"分析完成"
```

### 流程 B：开始一场文字面试

```
你选方向（java-backend）+ 难度 + 总时长 + 追问数
→ 后端读 skills/java-backend/SKILL.md（考察范围、难度分布）
→ 按总时长比例分配各阶段（自我介绍/技术/项目深挖/反问）
→ 排除你历史上做过的题
→ 校验题库容量够不够（不够直接禁止开局）
→ 建 interview_sessions（状态 CREATED）→ 生成第一题
```

### 流程 C：答一题 → 被判分 → 决定下一题

```
提交答案 → 存 interview_answers（session_id+question_index 唯一，防重复提交）
→ 模型评估这道题（按 40/20/25/15 权重）
→ 判断是否追问（追问也算独立记录，但标注针对哪道主问题）
→ 若阶段结束 → 进入下一阶段；若全部结束 → 状态 COMPLETED
→ 后台异步生成整场报告（总分/优势/改进/参考答案）
```

### 流程 D：语音面试

```
浏览器开麦克风 → WebSocket 上行音频
→ 服务端 VAD 判断"说完了" → ASR 转文字
→ 模型生成回答 → 句子级 TTS 合成 → 下行音频边收边播
→ 每句话写 voice_interview_messages（含 phase: INTRO/TECH/PROJECT/HR）
```

### 流程 E：知识库问答（RAG）

```
上传文档 → 切块 → 向量化 → 存 vector_store(embedding vector(1024))
→ 你提问 → 先把问题改写 → 按余弦相似度取 TopK 片段
→ 片段+问题拼 prompt → 模型回答 → SSE 流式吐字
```

### 流程 F：从知识库生成题库，再做专项面试

```
选知识库 → 异步生成题目（主问题+参考答案+关键点+评分标准+追问）
→ 存 DRAFT 草稿（生成不足时保留草稿，显示"实际/目标"）
→ 人工启用 → ACTIVE
→ 开始专项面试 → 抽题 → 作答 → 交卷 → 异步评估 → 趋势与报告
```

---

## 第 5 章 概念小课堂（每个 3 行）

**HTTP 请求**：浏览器发一段文本给服务器（方法 + 路径 + 头 + 体），服务器回一段文本。
`POST /api/interview/start` + JSON 体 = "我要开一场面试"。

**REST**：用"名词 + 标准方法"表达操作。`GET` 查、`POST` 建、`PUT` 改、`DELETE` 删。

**JSON**：`{"score": 82, "dims": {"accuracy": 40}}`。前后端和模型都用它传结构。

**鉴权**：服务器怎么知道"你是谁"。常见两种：Cookie（浏览器自动带）和 Token（请求头里带）。

**队列（Redis Stream）**：生产者把任务写进队列，消费者按序取出。
好处：请求立刻返回；坏处：要处理重复、失败、进程崩溃（见下条）。

**幂等**：同一个操作执行多次，结果和执行一次一样。
他项目里 `session_id + question_index` 唯一约束就是防"网络重试导致同一题交两次"。

**fencing（隔离令牌）**：多个工作进程同时处理同一任务时，用递增令牌让"旧进程"的写入失效。
他的迁移文件 `V20260906__task_attempt_fencing.sql` 就是干这个——**这是分布式系统的真功夫**。

**SSE**：一次 HTTP 请求，服务器持续往回写，直到结束。适合"打字机效果"。

**WebSocket**：一次握手后双向随时发。适合语音这种实时流。

**embedding（向量化）**：把一段文字变成 1024 个数字。语义相近的文字，数字也相近。
所以"找相似段落"变成"算向量距离"。

**RAG**：先在资料里检索出相关片段，再让模型"基于片段回答"。防止模型瞎编。

**事务**：一组数据库操作，要么全成功要么全回滚。转账扣钱加钱必须在一个事务里。

**连接池**：数据库连接很贵，提前建一批复用，避免每个请求都新建。

**埋点/监控**：在关键路径记耗时与数量（他的 `Micrometer`），用来发现"哪里变慢了"。

---

## 第 6 章 想自己跑起来：前置与步骤

| 需要 | 版本 | 本机现状（实测） |
|---|---|---|
| JDK | **25** | 有 JDK **21**（版本不够，需要 25） |
| Node.js | 18+ | ✅ 已装 |
| pnpm | 10+ | ❌ 没有（可 `npm i -g pnpm`） |
| Docker | 任意 | ❌ Windows 侧没有（但 WSL 里有 docker） |

**结论：本机现在跑不起来**，因为 JDK 是 21 而项目要 25。要跑得先装 JDK 25。

真要跑，步骤是：

```bash
git clone https://github.com/Snailclimb/interview-guide.git
cd interview-guide
cp .env.example .env          # 填 AI_BAILIAN_API_KEY（阿里云百炼）
docker compose -f docker-compose.dev.yml up -d    # 起 PG + Redis + RustFS
./gradlew :app:bootRun        # 起后端（首次会下载依赖，几分钟）
cd frontend && pnpm i && pnpm dev                  # 起前端
```

**注意 AGPL-3.0**：你可以学习、自用、修改，但如果对外提供服务，必须开源你的修改。

---

## 第 7 章 读代码的五天计划（具体到文件）

| 天 | 读什么 | 目标 |
|---|---|---|
| 1 | `skills/java-backend/SKILL.md`、`prompts/*.st` | 不用懂 Java，先看懂"给模型的话" |
| 2 | `db/migration/V1__init_schema.sql` + 10 个后续迁移 | 看懂数据模型和演进方式 |
| 3 | `InterviewController.java` → `InterviewSessionService.java` | 看懂一个请求怎么被处理 |
| 4 | `UnifiedEvaluationService.java`、`EvaluateStreamProducer/Consumer.java` | 看懂评估与异步 |
| 5 | `rate_limit_single.lua`、`AbstractStreamConsumer.java` | 看懂限流与任务恢复 |

---

## 第 8 章 自测 20 题（答案在心里过一遍）

1. 项目一共几个模块？分别是什么？
2. 为什么简历分析要异步？
3. `file_hash` 唯一约束解决什么问题？
4. 为什么简历分析结果表要把六个分数拆成六列，而题目塞成 JSON？
5. `vector(1024)` 里的 1024 是什么？
6. HNSW 索引是干什么的？
7. Skills 目录为什么用 Markdown？
8. 出题提示词里"数量硬性要求"防的是什么？
9. 评分提示词里"无效回答必须给 0 分"防的是什么？
10. 为什么要"分批评估 + 二次汇总"？
11. `session_id + question_index` 唯一约束防什么？
12. Redis Stream 相比"直接开线程"有什么好处？
13. fencing 令牌解决什么问题？
14. 限流脚本为什么分两阶段？
15. 返回值 `-i` 有什么用？
16. 为什么 RAG 用 SSE 而语音用 WebSocket？
17. `api_key_ciphertext` + `api_key_nonce` 为什么要两个字段？
18. 题库状态里的 `STALE` 是什么意思？
19. 为什么"容量校验"要前端禁用 + 后端兜底两道？
20. **它没有解决的问题是什么？**（提示：分数怎么保证可信）

---

## 第 9 章 它 vs 我们：学完之后你能讲什么

| 维度 | interview-guide | 我们 |
|---|---|---|
| 规模 | 687 文件 / 280 Java / 51+51 前端 | 39 py + 6 页面 / 9,612 行 |
| 数据 | PostgreSQL 12 表 + pgvector | SQLite 2 表 |
| 异步 | Redis Stream + 恢复 + fencing | 同步 |
| 输入 | 简历 + 知识库 + 语音 | 简历 + 题库 |
| 输出 | 报告 + PDF + 趋势 | 报告 + 账本 + 校准 |
| **可信度** | 提示词里写权重与 rubric | **阈值判结论 + 8 条不变量 + cassette 重放 + 57 标注校准** |
| 可复现 | 未提 | 同输入逐字段比对 |
| 自检 | 未提 | fatal 计数进报告 |

**学完它你能得到什么**：知道"平台"是怎么由六个组件拼出来的，能在面试里讲清楚 Skill 驱动、异步队列、两阶段限流、SSE/WebSocket 的区别。

**看完它你该看出什么**：它的评分是"提示词里写权重 + 模型输出"，**没有"分数与判定不一致时听谁的"这道保险**。这正是我们这个项目存在的理由。

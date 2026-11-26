# interview-guide（3543★）逐层拆解：每一层用什么语言、做什么、为什么

> 依据：仓库 README（2026-11 抓取）+ GitHub API 拿到的真实目录树/文件名。
> **没有克隆运行**。带【推断】标记的是"从文件名与模块职责合理推出"，不是 README 原文。

---

## 0. 先记住语言分工（这张表能解决你一半困惑）

| 文件/目录 | 语言 | 干什么 | 为什么是它 |
|---|---|---|---|
| `app/**/*.java` | **Java 25** | 全部后端业务逻辑 | 强类型、生态成熟、虚拟线程能扛 IO |
| `frontend/**/*.tsx` `*.ts` | **TypeScript** | 浏览器里的界面 | 类型检查 + React 生态 |
| `frontend/**/*.css` | CSS | 样式 | — |
| `app/src/main/resources/*.yml` | **YAML** | 配置（端口、数据库、模型参数） | 结构化且可读 |
| `app/src/main/resources/prompts/*` | **Markdown/文本模板** | 提示词 | 改 prompt 不用改代码 |
| `app/src/main/resources/skills/*` | **Markdown（SKILL.md）** | 面试方向定义（考什么、难度分布） | 加方向＝加文件，不写 Java |
| `app/src/main/resources/scripts/*` | **Lua** | Redis 里的原子限流脚本 | 在 Redis 内执行，天然原子 |
| 数据库 | **SQL**（PostgreSQL 方言） | 存数据；Flyway 管理版本 | 关系型 + 向量扩展 |
| `docker-compose*.yml` | YAML | 编排容器 | — |
| `gradlew` | **Shell/Bat** | 构建与运行 | — |

**一句话**：Java 干活，TS 画界面，YAML 做配置，MD 放"给模型看的话"，Lua 做原子限流，SQL 存数据。

---

## 1. 启动链：从零到能打开页面

```
① docker compose up -d
   ├─ 起 PostgreSQL 16（含 pgvector 扩展）
   ├─ 起 Redis 6
   └─ 起 RustFS / MinIO（S3 兼容对象存储，存简历文件）

② ./gradlew :app:bootRun   （或容器里的 java -jar）
   ├─ Spring Boot 启动，读 application.yml 与根目录 .env
   ├─ Flyway 执行 db/migration/*.sql → 建表/迁移（vector_store 表也由它建）
   ├─ Spring AI 初始化：读 api key、注册 LlmProviderRegistry
   └─ 启动 Redis Stream 消费者（简历分析队列、评估队列）
       └─ AbstractStreamConsumer 起来后开始轮询消费
   └─ ★ AsyncTaskRecoveryProperties + recovery
       └─ 进程重启时把"上次没跑完的任务"捞回来（这是成熟度的标志）

③ 前端
   ├─ pnpm build → 静态文件
   └─ 浏览器访问 → 调后端 REST 接口（/api/...）
```

**关键点**：异步任务不是"发出去就不管"，有一个 **recovery（恢复）** 机制处理"进程崩溃时正在跑的任务"。
这一条比很多玩具项目成熟得多。

---

## 2. 一个请求进来后，经过哪些层（Java 侧的分层）

以后端 `modules/interview` 为例，真实文件如下：

```
InterviewController.java          ← ① 接 HTTP：参数校验、返回 Result<T>
  └─ InterviewSessionService.java ← ② 业务编排：开面试/交卷/查历史
  └─ InterviewQuestionService.java← ② 出题：选题、去重、追问
  └─ AnswerEvaluationService.java ← ② 评估：调统一评估引擎
  └─ InterviewPersistenceService   ← ② 落库：会话、答案、报告
  └─ InterviewHistoryService.java ← ② 历史与趋势

  ├─ repository/                  ← ③ 数据访问（JPA/Spring Data）
  ├─ model/*Entity.java           ← ③ 数据库实体
  ├─ model/*DTO.java              ← ③ 对外数据结构（CreateInterviewRequest / SubmitAnswerResponse / InterviewReportDTO …）
  └─ listener/
       ├─ EvaluateStreamProducer.java  ← ④ 投递异步任务（写进 Redis Stream）
       └─ EvaluateStreamConsumer.java  ← ④ 消费异步任务（跑评估、写结果）
```

**分层规矩**：Controller 不写业务，Service 不拼 SQL，DTO 与 Entity 分离（用 MapStruct 转换）。
这就是"平台"和"脚本"的第一道差别——**每个文件只干一件事**。

---

## 3. 六条核心流程，逐步拆

### 3.1 简历上传 → 分析 → PDF 报告

| 步 | 谁做 | 用什么 | 做什么 | 产物 |
|---|---|---|---|---|
| 1 | 浏览器 | TS + fetch | 选文件上传（multipart） | 文件流 |
| 2 | `ResumeController` | Spring MVC | 校验大小/类型 | 请求对象 |
| 3 | `resume/service` | **Apache Tika** | 解析 PDF/DOCX/DOC/TXT 成纯文本 | 文本 |
| 4 | 同上 | Java 字符串处理 | 清洗（去页眉页脚/乱码） | 干净文本 |
| 5 | 同上 | **SHA-256** | 算内容哈希 | 去重键 |
| 6 | 同上 | JPA | 查是否已分析过（同哈希直接复用） | 命中/未命中 |
| 7 | 同上 | **S3 客户端** | 原文件存 RustFS/MinIO | object key |
| 8 | 同上 | JPA | 建 `resume` 记录，状态=`待分析` | DB 行 |
| 9 | `ResumeStreamProducer` | **Redis Stream XADD** | 把 `resumeId` 投进队列 | 队列消息 |
| 10 | `ResumeStreamConsumer` | `AbstractStreamConsumer` | 拉消息、ACK、失败计数 | 执行权 |
| 11 | `service` | **Spring AI**（`StructuredOutputInvoker`） | 把简历文本喂模型，要求输出**结构化 JSON**（技能/项目/亮点/风险） | JSON |
| 12 | 同上 | Java | 校验 JSON 结构、失败重试（≤3 次） | 稳定结果 |
| 13 | 同上 | JPA | 写分析结果，状态=`已完成`/`失败` | DB 行 |
| 14 | 浏览器 | TS 轮询 | 拉状态，展示进度 | 页面 |
| 15 | 导出 | **iText 8** | 生成 PDF 报告 | PDF 字节流 |

**为什么异步**：简历解析 + 模型调用是秒级到几十秒，同步会让请求超时、连接池被打满。
Redis Stream 让它"立刻返回、后台慢慢算"。

### 3.2 创建面试会话（Skill 驱动）

| 步 | 谁做 | 用什么 | 做什么 |
|---|---|---|---|
| 1 | 前端 | React 表单 | 选方向、难度、总时长、追问数 |
| 2 | `InterviewController` | Spring MVC | 组装 `CreateInterviewRequest` |
| 3 | `InterviewSkillService` | 读 `resources/skills/*`（**Markdown**） | 载入该方向：考察范围、难度分布、参考知识库 |
| 4 | `InterviewSessionService` | Java 计算 | **总时长 → 各阶段时长按比例分配**（自我介绍/技术/项目深挖/反问） |
| 5 | `InterviewQuestionService` | SQL 查询历史 | **历史题目去重**：排除该用户已问过的题 |
| 6 | 容量校验 | Java + DB count | 按"方向+难度+主问题数+每题追问数"算可用容量，不够就禁用选项；后端再兜底校验 |
| 7 | `service` | **Spring AI** | 生成第一问（或从题库抽） |
| 8 | `InterviewPersistenceService` | JPA + Redis | 会话写 DB；热数据（当前会话上下文）进 Redis |
| 9 | 返回 | `Result<InterviewSessionDTO>` | 前端拿到第一问 |

**Skill 用 Markdown 的意义**：新增"测试开发方向"只需加一个 `.md`，不用改 Java。
这是"可扩展性"的廉价做法，也是我们能直接借鉴的。

### 3.3 单轮作答 → 评估 → 追问 → 报告

| 步 | 谁做 | 用什么 | 做什么 |
|---|---|---|---|
| 1 | 前端 | fetch | 提交 `SubmitAnswerRequest`（会话 id + 答案文本） |
| 2 | `InterviewController` | — | 校验会话归属（防越权） |
| 3 | `InterviewPersistenceService` | JPA | 存 `InterviewAnswerEntity` |
| 4 | `AnswerEvaluationService` | **`UnifiedEvaluationService`** | 调统一评估引擎（文字与语音共用） |
| 5 | 评估引擎 | Spring AI + `QaRecord` | **分批评估**：把问答对分批喂模型，输出结构化评分 |
| 6 | 同上 | Java | **二次汇总**：分批结果合成总分/优势/改进 |
| 7 | 同上 | try/catch + 降级 | 模型失败时走兜底（不静默丢分） |
| 8 | `InterviewQuestionService` | Java + AI | 判断是否追问；追问数受配置约束（默认 1） |
| 9 | 同上 | 去重逻辑 | 下一题需排除历史题目 |
| 10 | `InterviewSessionService` | Java | 判断是否进入下一阶段（阶段时长联动） |
| 11 | 前端 | React | 渲染下一问 + 当前阶段进度 |

**评估引擎的三件事**（README 明写）：分批评估 → 结构化输出 → 二次汇总，加**降级兜底**。
这套结构比"一次让模型给个分"稳，但**它没有说阈值怎么定、模型判定与分数冲突怎么办**——这正是我们的切入点。

### 3.4 语音面试（最复杂的一条）

| 步 | 谁做 | 用什么 | 做什么 |
|---|---|---|---|
| 1 | 浏览器 | WebSocket API | 建长连接（`/ws/...`） |
| 2 | 浏览器 | WebRTC getUserMedia | 采集麦克风音频 |
| 3 | 服务端 `voiceinterview/handler` | Java WebSocket | 收音频帧 |
| 4 | `service` | **服务端 VAD** | 检测说话/停顿，自动断句 |
| 5 | 同上 | 千问3 **ASR** | 语音→文字（含中间结果，做实时字幕） |
| 6 | 同上 | **LLM** | 生成回答文本 |
| 7 | 同上 | **TTS（句子级并发）** | 文本→音频，边生成边合成边播，首包 200ms |
| 8 | 服务端 → 浏览器 | WebSocket 下行 | 音频帧 + 字幕 |
| 9 | 浏览器 | AudioContext | 播放；回声防护避免把自己的声音录回去 |
| 10 | 后端 | **Micrometer** | TTS/ASR 延迟、会话时长埋点 |

**他们自己承认的问题**：端到端延迟偏高（服务端中转音频）、无耳机时回声泄漏、TTS 音色单一、弱网断续。

### 3.5 知识库 RAG

| 步 | 谁做 | 用什么 | 做什么 |
|---|---|---|---|
| 1 | 上传 | Tika | 解析 PDF/DOCX/MD |
| 2 | 分块 | Java | 文本切块（chunk） |
| 3 | 投递 | Redis Stream | 异步向量化 |
| 4 | 向量化 | **Spring AI Embedding** | 文本→向量 |
| 5 | 存储 | **PostgreSQL + pgvector** | 写入 `vector_store` 表（由 Flyway 建） |
| 6 | 提问 | Java | **查询改写**（把用户问句改写成检索友好形式） |
| 7 | 检索 | pgvector | 相似度 + 阈值 + TopK 取片段 |
| 8 | 组装 | StringTemplate | 片段 + 问题拼成 prompt |
| 9 | 生成 | LLM | 回答 |
| 10 | 输出 | **SSE**（`text/event-stream`） | 打字机式流式返回 |

**为什么 SSE 不用 WebSocket**：问答只需要"服务端单向持续推送"，SSE 更简单（就是 HTTP 长响应），断线自动重连。

### 3.6 题库生成 + 专项面试

```
文档（已向量化）
  → [异步] 模型生成：主问题 + 参考答案 + 关键点 + 评分标准 + 追问
  → 存草稿态（生成不足时保留草稿，并显示"实际追问数 vs 目标数"，避免静默丢题）
  → 人工启用/归档（单条或批量）
  → 开始面试：容量校验（追问数是硬约束）
  → 抽题 → 作答记录 → 交卷
  → [异步] 评估：总分 + 逐题评价 + 优势 + 改进建议
  → 趋势统计 + PDF 导出
```

### 3.7 面试日程

```
粘贴邀请文本（飞书/腾讯会议/Zoom 格式）
  → 规则引擎先抽（正则匹配时间/链接/公司）
  → 抽不全时用 AI 兜底解析
  → 存日程（日/周/月视图，拖拽调整）
  → 定时任务扫描：过期自动置为"已过期"
  → 提醒（可配置）
```

---

## 4. 横切机制（决定"工程感"的六件事）

| 机制 | 实现 | 作用 |
|---|---|---|
| **限流** | `@RateLimit` 注解 + `RateLimitAspect` + **Redis Lua** | 原子计数，避免并发下超限 |
| **异步任务模板** | `AbstractStreamProducer` / `AbstractStreamConsumer` | 新队列只需继承，不用重复写 ACK/重试 |
| **任务恢复** | `AsyncTaskRecoveryProperties` + `recovery/` | 进程重启后捞回未完成任务 |
| **Prompt 安全** | `PromptSanitizer` + `PromptSecurityConstants` | 清掉用户输入里的注入片段 |
| **结构化输出** | `StructuredOutputInvoker` + `StructuredOutputProperties` | 强制模型输出可解析 JSON，带重试 |
| **统一响应/异常** | `common/result/Result<T>` + `common/exception/` | 前端只需处理一种格式 |
| **DB 迁移** | Flyway | 版本化建表，不靠手工 SQL |
| **对象映射** | MapStruct | Entity ↔ DTO 编译期生成映射代码 |
| **配置加密** | 写 `~/.interview-guide/`，API Key 加密 | 不入库、不进 Git |

---

## 5. 数据模型（从文件名与模块职责推断）

| 表（推断） | 关键字段 | 依据 |
|---|---|---|
| `resume` | id, 文件key, 内容哈希, 状态, 分析JSON | `ResumeController` + 状态枚举 |
| `interview_session` | id, 用户, skill方向, 难度, 总时长, 阶段, 状态 | `InterviewSessionEntity` |
| `interview_question` | id, session_id, 题干, 类型(主/追问), 顺序 | `InterviewQuestionDTO` |
| `interview_answer` | id, question_id, 答案文本, 评分JSON | `InterviewAnswerEntity` |
| `interview_report` | session_id, 总分, 分项, 优势, 改进 | `InterviewReportDTO` |
| `knowledge_base` / `document_chunk` | kb_id, chunk文本, 元数据 | 知识库模块 |
| `vector_store` | chunk_id, embedding(vector) | README FAQ 明确提到此表名 |
| `question_bank` | 题干, 参考答案, 关键点, 评分标准, 状态(草稿/启用/归档) | 题库模块 |
| `interview_schedule` | 公司, 岗位, 时间, 会议链接, 状态 | 日程模块 |

---

## 6. 为什么选这些技术（选型逻辑）

| 选择 | 理由 | 代价 |
|---|---|---|
| Java 25 虚拟线程 | 大量 IO 等待（DB/Redis/模型）时线程便宜，不必写响应式 | JVM 内存占用大、启动慢 |
| PostgreSQL + pgvector | 一个库同时做关系数据与向量检索，少一个组件 | 向量规模大时不如专用库 |
| Redis Stream | 比 Kafka 轻，够用；原生 ACK + 消费组 | 海量消息不如 Kafka |
| SSE 而非 WebSocket（RAG） | 单向流式，简单，自动重连 | 不能双向 |
| WebSocket（语音） | 必须双向实时 | 需要处理断线、心跳、背压 |
| Markdown 做 Skill/Prompt | 非程序员也能改；diff 可读 | 需要校验与版本管理 |
| MapStruct | 编译期生成映射，零反射开销 | 多一层构建步骤 |
| Flyway | schema 有版本、可回滚式演进 | 要写迁移脚本 |

---

## 7. 读代码的推荐顺序（真要细看时按这个走）

```
1. app/src/main/resources/skills/*.md             ← 先看"面试方向"长什么样（不用懂 Java）
2. app/src/main/resources/prompts/*               ← 再看提示词怎么写的
3. modules/interview/InterviewController.java     ← 接口有哪些
4. modules/interview/service/InterviewSessionService.java   ← 主流程编排
5. modules/interview/service/InterviewQuestionService.java  ← 出题与去重
6. common/evaluation/UnifiedEvaluationService.java          ← 评估引擎（核心）
7. modules/interview/listener/*StreamProducer/Consumer.java ← 异步怎么接
8. common/async/AbstractStreamConsumer.java                 ← 队列模板与恢复
9. common/ai/StructuredOutputInvoker.java                   ← 怎么强制结构化输出
10. modules/voiceinterview/handler/*                        ← 语音（最难，最后看）
11. frontend/src/api/* + pages/*                            ← 前端怎么调
```

---

## 8. 同一步骤：他们 vs 我们

| 步骤 | 他们（Java 平台） | 我们（Python 单进程） |
|---|---|---|
| 接请求 | Spring MVC Controller + DTO 校验 | `webui.Handler` 手写路由 + `payload` 校验 |
| 存数据 | PostgreSQL + JPA + Flyway | SQLite + 手写 SQL + ALTER 迁移 |
| 出题方向 | `skills/*.md` | `config.PHASE_CONFIG` + role 配置（写死在代码） |
| 历史去重 | SQL 查历史题目 | 探针树只防"要素重复"，**没有题目级去重**（P0 待补） |
| 生成题目 | Spring AI + 结构化输出 + 重试 | `interviewer.next_question()` + JSON 解析 + 失败降级 |
| 评分 | 分批评估 + 二次汇总 + 兜底 | 五维打分 + **阈值判结论** + 证据必须原文可查 |
| 长任务 | Redis Stream 异步 + 恢复 | 同步（无队列） |
| 流式 | SSE（RAG）/ WebSocket（语音） | 无（一次性返回） |
| 可复现 | 未提 | **cassette 重放 + 快照逐字段比对** |
| 自检 | 未提 | **8 条运行时不变量，fatal 不采纳** |
| 校准 | 未提 | **57 标注 / 5 折 CV 80% / CI [70, 91]** |
| 规模 | 多模块 + 前端构建链 + 5 个中间件 | 39 个 py 文件 + 零依赖 + 单容器 |
| 交付 | Compose 全家桶 | Compose 单服务 + 镜像 tar 45MB |

---

## 9. 看完这份你应该能回答的三个问题

1. **为什么他们需要 Redis？** 简历解析/评估是长任务，必须异步；异步任务需要队列、重试、恢复。
2. **为什么他们需要 pgvector？** 知识库要"按语义找片段"，向量检索是唯一可行方式。
3. **为什么他们需要 WebSocket、而我们不需要？** 语音是双向实时流；文字面试是一次请求一次响应。

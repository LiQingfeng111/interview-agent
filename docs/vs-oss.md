# 与 GitHub 同类项目对标（2026-11-24，第二版：读了代码/文档）

第一版只读仓库描述字段，写的是"它介绍里说了什么"。这一版**读了三个项目的 README 与架构文档原文**
（Hazenix/interview-prep-skills、Snailclimb/interview-guide、daftpunkwav/real-mock、
Samcoderg78/AI-mock-interview-agent），写的是"它具体怎么实现的"。

证据等级：`README 原文` > `仓库描述` > `我在本项目里的实测`。没有 clone 下来跑过它们，
所以下面是"文档声称 + 目录/接口设计可佐证"的深度，仍不等于我逐行读过源码。

---

## 一、别人具体怎么做的（可核对的机制）

### 1. Snailclimb/interview-guide（3540★，平台型，Java 25 + Spring Boot 4.1 + Spring AI）

| 机制 | 具体做法 |
|---|---|
| 简历解析 | Apache Tika 多格式；**Redis Stream 异步流水线**，进度状态机（待分析/分析中/已完成/失败），失败自动重试 3 次，**内容哈希去重** |
| 出题 | **Skill 驱动**：10+ 方向各一个 `SKILL.md`，定义考察范围、难度分布、参考知识库 |
| 去重 | 出题时**查历史会话，排除问过的题** |
| 时长联动 | 总时长滑块拖动后，**各阶段（自我介绍/技术/项目深挖/反问）按时比自动分配题量** |
| 评估 | 文字与语音**共用同一套评估引擎**：分批评估 → 结构化输出 → 二次汇总 → 降级兜底 |
| 交付形态 | PG + pgvector、Redis、WebSocket 实时语音（DashScope ASR/TTS）、PDF 报告导出、面试日历、飞书/腾讯/Zoom 邀请解析、多模型配置 |
| 许可 | AGPL-3.0（网络提供服务即须开源） |

**它比我们强在哪**：语音是真实时双向（我们只有浏览器 STT）；题量分配是产品级（我们固定 max_turns）；
岗位专项多（我们有 5 个岗位）。

### 2. daftpunkwav/real-mock（FastAPI 模块化单体 + Next.js，最像"工程规范"的一份）

| 机制 | 具体做法 |
|---|---|
| 阶段定义 | **SSOT**：后端 `workflows.py` 的 `PhaseDef`（含 `min_questions`/`max_questions`）→ 前端 `phases.ts` 镜像 → **守卫测试** `test_phase_ssot.py` 保证 id/顺序/label 三处一致 |
| 多套流程 | `technical` / `hr` / `management` 三套 workflow，每套 8~9 个阶段，各阶段独立题量区间 |
| 计划与降级 | 建会话时由 planner 生成 5~30 步计划（深挖步 6~15 问），**planner 失败就降级到静态 workflow**，且两者 duck-type 同一接口、走同一状态机 |
| 位置提示 | 每轮在 LLM 消息尾部追加一行 `[Position]`（第 k 步/共 n 步、第 i 问），**不写进历史** |
| 缺口回填 | `plan_ops.insert_after_current`，每次回复最多插 3 个补充步骤 |
| 上下文压缩 | **步骤边界压缩**：≤20k token 保留原文，超出则压缩成结构化 briefing（topics / evidence / verified / suspicious / weak_points / agreed_facts / probes_pending + reflections），失败重试 3 次后退回原文 |
| 报告 | 两阶段 ReAct：分批 turn notes（12 轮/批）→ 汇总；状态机 `pending/generating/ready/failed`，CAS 抢占，**480 秒墙钟预算** |
| 契约 | `export_openapi.py` → `openapi.json` → 前端 `api.d.ts` 自动生成类型 |

**它比我们强在哪**：上下文压缩（我们只抽 facts，没有 verified/suspicious 分类）；
阶段 SSOT + 守卫测试（我们 phase 文案与后端硬编码，会漂移）；报告状态机与预算。

### 3. Samcoderg78/AI-mock-interview-agent（LangGraph，最像"教学范本"的一份）

- LangGraph 状态机：Config → Resume → Job → Planner → Generate → **human-in-the-loop interrupt** → Evaluator → Feedback → Memory → 自适应 → Report
- **控制逻辑用纯 Python**（打分算术、难度调整、追问规则、停止条件），语言任务才交给 LLM——这点和我们同构
- 四种题型（技术/行为 STAR/情景/案例），任意职业，不强绑定程序员
- 自适应：弱答一次针对性追问，强答升难度
- **无固定题数**：计划覆盖完即结束
- 每答一次给：分数 + 反馈 + 一条改进建议 + **model answer**
- 报告：总分/分主题/优势/改进/学习计划/岗位匹配度/逐题回顾
- **跨会话记住 per-topic 分数**，下次优先补弱项

它的 README **自己写了三条限制**（值得尊重）：
> The LLM judge has **not** been validated against human ratings…
> The interview state is held in memory, so closing the app mid-interview loses it.
> Only the first 12,000 characters… Tested mainly with English-language input.

它的 Roadmap 头三条正是我们**已经做完**的：judge–human 一致性测量与调参、SQLite 持久化、语音模式。

### 4. Hazenix/interview-prep-skills（252★，Skills 形态，最像"能力包"的一份）

- 形态是 Agent Skills（`SKILL.md` + `references/` 按需加载），装进 Claude Code / Codex 里用，不是独立服务
- 5 个 skill：简历条目打磨、简历批量出题、前端/后端/AI 应用模拟面试
- 多身份（日常实习/暑期/校招/社招 1–3 年）、**六种面试官风格**（严厉拷打/温和鼓励/专业高效/深挖学术/工程实践/综合平衡）
- **实时状态跟踪**：已考察主题 + 待追问清单
- **两种纠错模式**：严格模式（模拟压力）/ 即时引导（答错给提示）
- 支持"暂停、恢复、提前结束、换个风格"
- 简历条目打磨会要求"量化指标的依据与验证方法"，缺数据时标"待验证"——**这点很专业**

**它比我们强在哪**：六种风格 + 两种纠错模式（我们只有 3 种 persona、无严格/引导切换）；
"待验证估算"的诚实标注；分发形态极轻（不需要起服务）。

---

## 二、我们相对别人的优势（只列能拿出证据的）

| # | 优势 | 证据 | 别人到哪一步 |
|---|---|---|---|
| 1 | **判定可审计**：append-only 事件账本 + 哈希链 + `fold()` 幂等 + **8 条运行时不变量**（fatal → 输出不采纳） | `ledger.py` / `invariants.py`；`python -m eval.audit_check` | real-mock 有 ledger 冻结与报告 CAS（最接近），但**无运行时不变量体检**；LangGraph 那份状态在内存 |
| 2 | **判定可复现**：cassette 重放，同指纹不重复调模型，快照逐字段比对 | `--replay`，`eval.audit_replay` 门禁 | 四个项目都没提 |
| 3 | **尺子经过校准且报了不确定性** | 57 条标注、5 折 CV 80%、**bootstrap 95% CI 81% [70%, 91%]**、双模式两把尺子 | interview-guide 未提；LangGraph 那份 roadmap 第一项就是"要做"；cje 类项目做得比我们深 |
| 4 | **判定权在阈值，不在模型**（已修） | `normalize_eval` 用 `VERDICT_THRESHOLDS`，冲突记 `verdict_conflict`，INV-05 报警 | 未见于上述四份文档 |
| 5 | **证据可核验**：评分 evidence 必须能在回答原文里找到，否则 fatal | INV-04；`evidence ⊆ answer` | 只有 LangGraph 那份提到"verify strongest claims"，未提校验评分证据 |
| 6 | **成本/延迟有账**：每次调用记 token/成本/延迟/指纹，三级降级，`finish_reason` 五值闭集 | `budget.py`、`tracing.py`；实测 2 轮 ¥0.0017 / +coach ¥0.0043 | 都未提 |
| 7 | **深挖状态是代码结构，不是提示词记忆** | `probe_tree.py`：要素覆盖/回避/未答出、每要素最多问 2 次、木梯 6 层 | Hazenix 的"实时状态跟踪"在 SKILL.md 层面；我们是结构与队列 |
| 8 | **零依赖可跑**：CLI / Web / 一键脚本 / 自检（测试不过不起服务） | `bootstrap.py`、`check.py` 7 门禁 | interview-guide 需 PG+Redis+MinIO；real-mock 需前后端两套 |

### 一句话定位

> 别人做的是**面试产品**（功能多、体验顺、能上手）。
> 我们做的是**面试判定的可审计实现**（能回答"这个分数凭什么"、"换个模型还准不准"、"同样的回答会不会今天 pass 明天 fail"）。
> 对实习简历，"支持语音面试"是加分项；"我能证明评分过程的每一步"才是差异项。

---

## 三、我们还缺什么（按"抄谁"归类）

| 缺口 | 抄谁 | 代价 | 价值 |
|---|---|---|---|
| 步骤边界上下文压缩（verified/suspicious/agreed_facts 分类） | real-mock | 1~2 天 | 高：长会话不丢信息也不撑爆上下文 |
| 阶段 SSOT + 守卫测试（后端 phase ↔ 前端顺序/label 一致性） | real-mock | 半天 | 中：防漂移，正好是"可审计"的延伸 |
| 总时长 → 各阶段题量联动 | interview-guide | 半天 | 中：体验明显变好 |
| 跨会话 per-topic 成长看板 | Samcoderg78（已做记忆）/ interview-guide（日历） | 1 天 | 高：作品集最容易被点开的一页 |
| 六种面试官风格 + 严格/引导两种纠错模式 | Hazenix | 1 天 | 中：persona 已有 3 种，扩到 6 种便宜 |
| 真实时语音（WebSocket + 流式 ASR/TTS） | interview-guide | 3~5 天 | 低（投入产出比最差） |
| 编码题沙箱执行 | 两者 roadmap 都有 | 3 天+ | 低（安全与资源成本高） |

---

## 四、诚实边界

- 上述四个项目我**没有 clone 运行**，机制描述来自 README/架构文档原文；"我们没有 vs 它有"的判断基于文档声称。
- 我们的校准只有一个标注者（我），80% 是"与我自己标准的一致率"，不证明标准正确。
- Dockerfile 写了但**本机没有 Docker，从未 build 过**——不能声称"支持一键部署"。
- Web 端会话在内存 + SQLite 双写，但多进程部署会各有各的 `SESSIONS` 字典。

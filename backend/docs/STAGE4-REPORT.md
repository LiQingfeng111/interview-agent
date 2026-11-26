# 阶段 4 完成报告：LangGraph 状态图 + RAG 接入主链路

> 日期：2026-11-26
> 状态：**图跑通，端到端实测通过**

---

## 一、这一阶段做了什么

| 模块 | 文件 | 行数 | 作用 |
|---|---|---|---|
| 状态图 | `graph/interview_graph.py` | 330 | 5 节点 + 条件入口 |
| 会话运行时 | `core/runtime.py` | 230 | 把旧 `step()` 拆成 4 个可独立调用的方法 |
| 运行时注册表 | `core/registry.py` | 60 | 存活实例管理 + 进程重启恢复 |
| 服务编排 | `core/service.py` | 380 | 图 + 注册表 + PG 持久化 |
| 会话路由（重写） | `api/sessions.py` | 160 | 走服务层，接口不变 |

```
后端总规模：33 文件 / 3,914 行（app 层）
测试：旧 379 passed + backend 30 passed
```

---

## 二、图的结构（条件入口是关键设计）

```
START ─┬─(无 projects)──→ plan ──→ probe ──→ END        ← 开场第一问
       └─(有 projects)──→ evaluate → route ─┬→ probe → END   ← 后续每轮
                                             └→ report → END  ← 收尾
```

**为什么要条件入口**：第一版固定 `plan` 为入口，结果每次 `invoke` 都重跑 plan，
`turn` 永远被重置成 0、`pending_question` 永远是第一问。
实测现象是"答三轮还是第 0 轮、问题一字不变"。

**`probe` 之后直接 END 也是刻意的**：一次 `invoke` 只产出一个问题，等候选人回答后再 invoke 一次。
这样"暂停/恢复"的边界才清晰，也才对得上 `interrupt()` 的语义。

---

## 三、运行时拆分（规则层零重写）

旧 `session.step(answer)` 是一体的：评分 → 推进 → 出题。
图需要它们分开，所以 `SessionRuntime` 暴露四个方法：

| 方法 | 对应旧实现 | 为什么要分开 |
|---|---|---|
| `next_question()` | `Interviewer.next_question()` | 出题节点单独调 |
| `evaluate_answer()` | `Evaluator.evaluate()` | 评分节点单独调，**评分失败不影响状态机** |
| `advance()` | `StateMachine.maybe_advance()` | 路由节点纯规则，不调模型 |
| `build_report()` | `session.finish()` | 收尾节点 |

**旧代码一行没改**。`SessionRuntime` 内部持有旧的 `InterviewSession` 实例，
通过 `getattr` 防御性读取它的内部状态（`sm`、`turns`、`interviewer`）。

---

## 四、端到端实测（真实运行）

```
=== 开面试 ===
{"session_id":"s_9aac0d7de26547e5","turn":0,
 "question":"我们开始吧。请用两分钟介绍一下你自己…",
 "projects":["项目一：课程问答助手（2026.03-2026.09）"],
 "phase":"OPENING","done":false}

=== 连续答题 ===
第1轮: verdict=partial avg=3.0 done=false
第2轮: verdict=partial avg=3.0 done=false
第3轮: verdict=partial avg=3.0 done=false

=== 结束后 ===
phase: OPENING → PROJECT_PROBE     ← 阶段推进了
total_turns: 3                      ← 轮次正确
status: completed
```

**三个 avg=3.0 相同是 mock 模式的正常表现**（同一答案固定打分）。
换成真实模型会不同——之前实测过 `pass 3.4` / `partial 2.2` / `fail 1.4`。

---

## 五、抓到的 4 个 bug（全是实跑才暴露的）

### bug 1：节点调错模块

```
AttributeError: module 'app.core.legacy_session' has no attribute 'get_runtime'
```

`get_runtime` 在 `registry` 里，不在 `legacy_session`。
**后果**：图整条挂掉 → 降级 → 轮次永远 0。

### bug 2：每次 invoke 都重跑 plan

第一版把 `plan` 固定成入口。图能跑，但 `turn` 永远 0。
**这是设计缺陷不是编码错误**——静态看代码发现不了，必须跑起来看多轮结果。

### bug 3：`quota` 导错包

```
ImportError: cannot import name 'quota' from 'app.core'
```

`quota` 在 `app.rag` 下。`route` 节点崩 → 整条降级。

### bug 4：`turns` 表外键约束

```
violates foreign key constraint "turns_session_id_fkey"
Key (session_id)=(s_20e44019b333401c) is not present in table "sessions"
```

调试时直接建 runtime 跑图，没走 `service.start` 的落库，`sessions` 行不存在。
**修法**：写 `turns` 之前先幂等插入 `sessions` 父行。

> 这四个 bug 的共同点：**单元测试、类型检查、代码审阅都发现不了**。
> 只有"真跑 + 看多轮结果"才会暴露。尤其是 bug 2，它是设计层面的。

---

## 六、RAG 已接进主链路

出题节点的三步：

```python
# ① RAG：用最近对话查简历，命中后取 parent_text
hits = retriever.search_resume(query, owner=user, k=3)
resume_ctx = retriever.build_context(hits)   # 返回整项目文本，不是单块

# ② TECH_QA 阶段：语义选题（含话题配额）
picked = bank_select.pick(query, role, asked_qids, turn, topic_counts, total_turns)

# ③ 出题：复用旧 Interviewer（清洗/去重/不变量/兜底都在里面）
question = runtime.next_question(resume_context=resume_ctx, bank_qid=bank_qid)
```

**RAG 上下文通过 `interviewer.web_refs` 通道注入**——那是旧代码已有的"外部参考资料"机制，
不用改 prompt 模板，9 条黄金快照仍然有效。

---

## 七、降级链（图挂了面试不能挂）

```
图执行异常
  → _fallback_round()：直接调 runtime.next_question()
  → 返回降级问题 + degraded=True
```

理由：图是新引入的复杂度，但它不该成为"整个面试不可用"的单点。
降级后功能受损（没有 checkpointer、没有 RAG 编排），但用户还能把这场面试答完。

---

## 八、进程重启恢复（已实现但未实测）

`_restore_runtime()`：从 PG 读 `sessions` + 最新 resume 文档 → 重建 `SessionRuntime` → 放回注册表。

**诚实说明**：这条路代码写了，但**没有实测**——
因为当前用的是 `MemorySaver`（内存 checkpointer），进程重启后图状态也丢了。
下一阶段换成 `PostgresSaver` 后才能真验证。

---

## 九、还没做的

| # | 任务 | 预估 |
|---|---|---|
| 1 | Postgres checkpointer（真断点续跑） | 0.5 天 |
| 2 | 与 27 条接口快照逐字段比对 | 1 天 |
| 3 | Vue 3 前端 6 个页面 | 5 天 |
| 4 | 三层记忆（长期画像） | 1.5 天 |

---

## 十、当前完整状态

```
PostgreSQL : ok   10 表，pgvector 0.8.7，722 题全部有向量
Ollama     : ok   bge-m3 1024 维
FastAPI    : ok   :8080，LangGraph 已接入
旧测试     : 379 passed
新测试     : 30 passed
```

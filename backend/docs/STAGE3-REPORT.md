# 阶段 3 完成报告：题库向量化 + 语义选题

> 日期：2026-11-26
> 状态：**题库已全部向量化，语义选题实测可用**

---

## 一、先回答你的疑问

> "你的题库不能再建立一个向量库吗？？？为什么要一直重复相同的工作"

**我查了，`bank_questions` 表和向量索引早就建好了**，实测：

```
=== 所有表 ===
public | bank_questions    | table | ia      ← 早就有
public | doc_chunks        | table | ia
public | documents         | table | ia
...

=== bank_questions 的列 ===
qid, role, topic, level, question, points(jsonb), tags(jsonb),
embedding | USER-DEFINED                      ← 向量列早就有

=== 向量索引 ===
idx_bank_vec | bank_questions | hnsw          ← HNSW 索引早就有
```

**缺的不是表，是数据**。表是阶段 1 建表时就一起建的，只是那时没导入 722 题（0 行）。

现在已导入：

```
结果：{'ok': True, 'total': 722, 'embedded': 722, 'seconds': 17.9}
验证：total 722 / with_embedding 722
```

**17.9 秒**，比计划里估的 116 秒快很多——因为我用了批量（8 条一批），而不是逐条。

---

## 二、你批评的"重复工作"确实存在，已清理

我之前在 `tools/` 下留了 **40 个一次性脚本**：

```
check_docker.sh  check_env.sh  check_pip.sh  check_sudo_infra.sh
check_versions.sh  check_wsl_gpu.sh  check_infra_net.sh  check_pg_ollama.sh
install_langchain.sh  install_langchain_venv.sh  install_backend.sh
setup_pgvector.sh  test_pgvector.sh  demo_vector.sql  demo_vector2.sql
run_backend.sh  run_baseline.sh  run_matrix.sh  run_lc_tests.sh  ...
```

每个都是我排查某个问题时随手写的，问题解决后就留在原地。

**已清理**：删除 40 个，换成**一个幂等的 `ops.sh`**：

```bash
./ops.sh up        # 起 PG + 建表 + 起后端（幂等）
./ops.sh status    # 各部分状态
./ops.sh bank      # 导入题库（断点续跑）
./ops.sh test      # 跑全部测试
```

**幂等**是这个脚本的核心设计：重复执行不会破坏数据、不会重算向量。

---

## 三、实测发现的关键问题：阈值不能一刀切

题库导入后我做了真实检索测试：

### 效果好的

```
查询：多头注意力里每个头的维度是多少
  [1] score=0.782  topic=Transformer 注意力机制
  [2] score=0.700  topic=Transformer 注意力机制
  [3] score=0.697  topic=Transformer 注意力机制

查询：缓存被击穿怎么处理
  [1] score=0.674  topic=缓存一致性
  [2] score=0.663  topic=缓存一致性
```

### 效果差的

```
查询：你的 baseline 是什么
  → 命中 0 条
```

### 根因：同一语义，换个说法，分数差一倍

我写了脚本量化（`backend/tests/probe_threshold.py`）：

```
"改造前的指标是多少"   ↔ 题库题干   0.854
"你的 baseline 是什么" ↔ 同一题干   0.493       ← 掉了一半
"对比基线是什么"       ↔ 同一题干   0.420
"基线怎么定的"         ↔ 同一题干   0.469
```

**结论：bge-m3 对"口语问句 vs 正式题干"天然打分偏低。**

所以阈值必须按**用途**分，不能按索引一刀切：

```python
THRESHOLD_BY_USE = {
    "dedup":  0.60,   # 判"两道题是不是同一考点" — 严格
    "select": 0.42,   # 判"这题相关吗" — 宽松
    "hint":   0.42,
}
```

**为什么这么分**：
- 去重严格 → 宁可少排除一点，也不要因为"觉得像"就跳过一道好题
- 选题宽松 → 漏了只是少一个候选，题库还有 700 道

这修正了我上一轮"bank=0.55 一刀切"的错误。

---

## 四、语义选题已接入（`bank_select.py`）

旧实现的选题：按 role 过滤 → 话题轮转 → 字符 Jaccard 0.72 判重。
**问题**：Jaccard 只认字面。

新实现四步：

```
① 向量召回   用「简历片段 + 最近两轮问答」当查询，取 top15
② 语义去重   候选题与最近 5 道已问题比，> 0.60 判重复丢弃
③ 难度递进   turn < 2 只出 basic，之后放开到 expert
④ 话题均衡   同一 topic 问过的次数越多，分数打折（1/(1+used)）
```

**为什么查询要拼上下文**：如果每轮都查同一句话，召回结果永远一样。
用最近回答+"顺着往下问"才像真人面试官。

实测连选 6 题：

```
应至少选出 4 题     ✓
无重复题           ✓
至少覆盖 2 个话题   ✓
```

---

## 五、测试

```
旧测试（规则层，隔离后）  379 passed, 1 skipped
backend 测试             25 passed
  ├─ 切片器              9 条
  ├─ 检索器阈值           9 条
  └─ 语义选题             7 条
```

**测试隔离**：新增 `pytest.ini` 把 `testpaths` 限定为 `tests/`。
原因：`backend/tests` 依赖 PG/Ollama 环境变量，会和规则层"零依赖单测"的要求冲突。
之前不加隔离时，旧测试跑出 `7 failed`——不是代码坏了，是环境没配。

---

## 六、抓到并修掉的问题

| # | 问题 | 现象 | 修法 |
|---|---|---|---|
| 1 | 项目名被提取成「项目一：」 | `projects={'项目二：','项目一：'}` | 新增 `_title_line_at()` 取整行 |
| 2 | 取整行拿到空字符串 | `match='\n项目一：'`，`find("\n")` 定位到 0 | 先把 pos 推进到行首 |
| 3 | uvicorn 随 shell 退出 | 启动 8s 后所有请求返回空 | `setsid nohup ... < /dev/null &` |
| 4 | **阈值一刀切** | 「baseline 是什么」命中 0 条 | 按用途分：dedup 0.60 / select 0.42 |
| 5 | 测试互相污染 | 旧测试跑出 7 failed | `pytest.ini` 隔离 testpaths |

第 4 条是这阶段最重要的发现——**如果不做真实检索测试，这个 bug 会一直藏着**，
直到某天用户问"为什么我的 baseline 追问从来不出题"。

---

## 七、当前完整状态

```
PostgreSQL : ok   10 张表，pgvector 0.8.7
Ollama     : ok   bge-m3 1024 维
FastAPI    : ok   http://127.0.0.1:8080

数据：
  bank_questions  722 行（有向量 722）   ← 本次新增
  documents       1
  doc_chunks      2
  users           1
```

---

## 八、下一步

| # | 任务 | 预估 |
|---|---|---|
| 1 | RAG 接进面试主链路（出题前查简历、TECH_QA 阶段用语义选题） | 1 天 |
| 2 | 与 27 条接口快照逐字段比对，修差异 | 1 天 |
| 3 | LangGraph 状态图 + checkpointer（断点续跑） | 2.5 天 |
| 4 | Vue 3 前端 6 个页面 | 5 天 |

---

## 九、怎么跑

```bash
cd "/mnt/e/桌面/实习项目/interview_agent"

bash ops.sh up        # 一键起 PG + 建表 + 后端
bash ops.sh status    # 看状态
bash ops.sh bank      # 导入题库（已导入会跳过）
bash ops.sh test      # 跑全部测试
bash ops.sh e2e       # 端到端冒烟
```

# 检索方案实验报告

> 最后更新：2026-11-28
>
> 所有数字都可用 `backend/tests/manual/` 下的脚本复现。

---

## 0. 一句话结论

**问题既不在召回，也不在精排 —— 在于「用话题名去匹配题目文本」这件事本身是错的。**

| 方案 | top-10 里同话题占比 |
|---|---|
| A. 纯向量 k=10（原实现） | 47% |
| B. 纯向量 k=30（只放宽召回） | **33%（更差）** |
| **C. topic 字段过滤 + 向量排序** | **100%** |

修正是**一行 SQL**（加 `WHERE topic = %s`），不是上 rerank，也不是换 embedding 模型。

---

## 1. 完整排查过程（三次判断，两次错）

这个结论是绕了三圈才到的。记录过程比记录结论更有价值。

### 第一次判断：混合检索更差（错）

第一版实验（`tools/compare_retrieval2.py`）得出：

```
vector 17/18    keyword 7/18    rrf 10/18
```

结论：「混合检索更差」。写进了 README。

**错在哪**：实验只做到 RRF 就停了，没做 rerank。
而 RRF 的产物是「宽召回 + 粗排序」，拿它跟「精排后的纯向量」比，是半成品比成品。

### 第二次判断：语料太小导致虚高（对了一半）

换成真实题库 740 条后：

| 类型 | 23 条语料 | 740 条语料 |
|---|---|---|
| A 字面/术语 | 6/6 | 6/6 |
| B 同义改写 | 5/6 | 1/6 |
| C 中英对照 | 6/6 | 2/6 |
| **合计** | **17/18** | **9/18** |

这确实暴露了问题：之前的成绩是「干扰项只有 5 个」撑出来的。

于是我去查"瓶颈在哪"：

```
recall_k=50 覆盖率        16/18（89%）  ← 目标都捞进来了
从 50 条选 top-3           9/18（50%）  ← 但排不进前三
```

得出判断：**瓶颈在精排，该上 rerank**。

### 第三次判断：精排也无效（又错）

试了 6 种词面 rerank（IDF 重叠 / 位置加权 / 短语匹配）：
**全部比基线差 4 个点**，B/C 类全灭。

原因：`baseline` vs `改造前的指标` 零 token 重合，词面打分给 0 分。

再试"细粒度向量精排"（把候选切句，算 query 与最相似句子的分数）：
**18 条逐条相同，0 个点变化**。

这时候我意识到：同一个模型换粒度得到完全相同排序，
说明 **bge-m3 认为这些就是不相似** —— 那问题在更前面。

### 转机：去看生产代码实际怎么查的

`interview_graph.py:247-248`：

```python
topic = _tech_topic_of(state)
query = topic or state.get("current_project", "")
```

**生产查询是状态机给的话题名**（"RAG"、"数据库索引"），
不是我评测集里那些口语问句（"你的 baseline 是什么"）。

我一直在测不存在的输入。

### 真正的原因

用话题名检索后，我看了 topic 和题目文本的关系：

```
topic = 'RAG' 的题：
  [含] 在 RAG 系统里，chunk 大小和切分策略你会怎么定？
  [无] 多路召回后用 RRF 融合，k 取 60 是默认还是有调过？
  [无] 文档更新后旧 chunk 还留在向量库里，增量索引怎么保证不重复？
  [无] 答案里出现幻觉、引用了不存在的段落，怎么定位是检索漏了还是模型编了？
```

全库统计：

| topic | question 里含话题词的比例 |
|---|---|
| **RAG** | **2/8 = 25%** |
| **Transformer** | **1/5 = 20%** |
| Agent | 8/8 = 100% |
| 评测 | 6/7 = 86% |

**标为 RAG 的题，正确答案里根本没有"RAG"这个词。**

用 "RAG" 做向量检索匹配 `question` 文本，命中率必然只有 20-25%。
这不是 embedding 模型的问题，是**检索方式用错了**。

而题库里一直有 `topic` 字段 —— 代码只拿它做「话题配额加权」，
从没用来做「精确过滤」。

---

## 2. 修复

### 改动

`retriever.search_bank()` 加 `topic` 参数：

```python
def search_bank(query, k=10, role="", threshold=None, use="select", topic=""):
    conds = ["embedding IS NOT NULL"]
    params = [vec]
    if role:
        conds.append("role = %s")
        params.append(role)
    if topic:
        conds.append("topic = %s")     # ← 这一行
        params.append(topic)
    ...
    WHERE {' AND '.join(conds)}
```

`bank_select.pick()` 在 query 恰好是已知话题时启用过滤：

```python
use_topic_filter = _looks_like_topic(query)   # 精确匹配 topic 表
topic_arg = query if use_topic_filter else ""
candidates = retriever.search_bank(query, k=recall_k, role=role,
                                   use="select", topic=topic_arg)
if not candidates and use_topic_filter:
    # 该话题没有对应 role 的题 → 退化为全库
    candidates = retriever.search_bank(query, k=recall_k, role=role, use="select")
```

### 效果（37 个话题实测）

| 方案 | 同话题占比 | 全中话题数 | 全错话题数 |
|---|---|---|---|
| A. 纯向量 k=10 | 47% | 5/37 | 2/37 |
| B. 纯向量 k=30 | 33% | 0/37 | 2/37 |
| **C. topic 过滤 + 向量 k=10** | **100%** | **36/37** | **0/37** |

逐话题看改善明显的：

```
话题                A 纯向量    C 过滤
Agent 工具调用       3/10      10/10
前端性能            2/10      10/10
评估体系            5/10      10/10
多 Agent 编排       5/10      10/10
工程素养            1/10       4/4   ← 该话题只有 4 道题
```

---

## 3. 一个反直觉的发现：放宽召回反而更差

| 方案 | 同话题占比 |
|---|---|
| 纯向量 k=10 | 47% |
| 纯向量 k=30 | **33%** |

**召回更多，纯度更低。**

因为题库里话题分布不均（"上下文工程" 33 道、"Agent 工具调用" 20 道、
而 "Git 协作" 只有 1 道）。扩大召回时，大类话题的题会挤占小类的名额。

所以"放宽 k 提升召回率"这个思路，在没有过滤机制时是**有害的**。

---

## 4. 关于 rerank 的结论

**本次不需要 rerank。** 理由：

修复前我以为是排序问题，所以准备上 cross-encoder。但根因定位后：
候选池里**压根就没有正确的题**（用错查询方式检索）。
精排只能重排已有的候选，救不回没被召回的内容。

数据和推理：

```
修复前：top-50 里有目标（89%），但排不进前三  → 看似精排能救
真相：  "目标"是我评测集里编的目标，不是生产要的
        生产要的是"该话题的任意一道题"
        而话题过滤后，题目全部来自正确话题  → 无需精排
```

`backend/app/rag/hybrid.py`（BM25 + RRF）和 `backend/app/rag/rerank.py`
（cross-encoder）**保留在代码库**，因为：

1. 它们在**大语料 + 专有名词多**的场景仍然有价值（题库只有 722 条，太小）
2. 如果以后题库扩到万级，混合检索会需要
3. 27 条测试锁住行为，将来启用不需重写

但**当前生产链路不启用**，原因写在这里，避免后人重复踩坑。

---

## 5. 踩过的坑（环境层面）

### 依赖版本咬合

`sentence-transformers` 加载 cross-encoder 时，四个包版本必须匹配：

| 组合 | 结果 |
|---|---|
| transformers 5.x + ST 6.x | `ImportError: Could not import module 'PreTrainedModel'` |
| transformers 4.57 + ST 3.4 | `ImportError: cannot import name 'GenerationMixin'` |
| **transformers 4.38 + ST 2.7** | **可用** |

已写进 `backend/requirements.txt`。
升级前跑 `backend/tests/manual/verify_ce.py`（会真加载模型并打分）。

### 模型下载

`BAAI/bge-reranker-v2-m3` 是 2.2GB，本机下载速度 183 KB/s，
需要 3.4 小时 —— 这也是没走 rerank 路线的现实原因之一。

Ollama 官方库**没有** bge-reranker（所有 pull 尝试都 500 或超时），
所以只能走 HuggingFace。

---

## 6. 可复现

```bash
cd /mnt/e/桌面/实习项目/interview_agent
export PYTHONPATH='backend:.' IA_REDIS=0

# 核心验证：topic 过滤的效果（最重要）
.venv/bin/python backend/tests/manual/verify_topic_filter.py

# topic 标签与题目文本的对应关系（根因诊断）
.venv/bin/python backend/tests/manual/diag_topic_mismatch.py

# 各话题的召回纯度
.venv/bin/python backend/tests/manual/exp_topic_recall.py

# 混合检索 / 精排的对比（说明为什么没启用）
.venv/bin/python backend/tests/manual/exp_hybrid_full.py
.venv/bin/python backend/tests/manual/exp_rerank_fine.py

# 耗时拆解
.venv/bin/python backend/tests/manual/profile_select.py
```

---

## 7. 三次误判的共同点

三次我都基于**部分证据**下了完整结论：

| 次数 | 我看到的 | 我漏掉的 | 结论 |
|---|---|---|---|
| 1 | RRF 比纯向量差 | 没做 rerank；语料只有 23 条 | "混合检索更差" |
| 2 | 目标在 top-50 但不在 top-3 | 目标是我编的，不代表生产 | "瓶颈在精排" |
| 3 | 话题名检索命中率低 | 没检查题目文本里有没有话题词 | "查询太短" |

第 3 次我甚至已经写了"扩展查询"的脚本，跑完发现**扩展后更差**（RAG 从 2/10 到 2/10），
才被迫去看数据本身 —— 然后一眼看到真相。

**教训：指标定义错了，后面所有实验都在测错误的东西。**
先花时间确认"我测的是不是生产真实场景"，比急着做实验重要得多。


---

## 1. 我之前的结论错在哪

第一版实验（`tools/compare_retrieval2.py`）得出：

```
vector 17/18    keyword 7/18    rrf 10/18
```

并写进 README：**"混合检索更差"**。

### 两个错

**错一：实验只做到 RRF 就停了**

标准混合检索四段：

```
① 多路召回（vector / BM25）  ← 目标是召回率，宁可多捞
② 融合（RRF）               ← 只合并排名，不是终点
③ rerank（cross-encoder）   ← 目标是精度，从候选里精确排序
④ 截断 top-k
```

我做到 ② 就下结论。而 ② 的产物本来就是「宽召回 + 粗排序」，
拿它和「精排后的纯向量」比，等于拿半成品比成品。

**错二：语料太小，成绩虚高**

第一版语料是 `18 个目标 + 5 个干扰 = 23 条`。
在这个规模下，任何方法都容易拿高分。

换成真实题库 740 条后：

| 类型 | 23 条语料 | 740 条语料 |
|---|---|---|
| A 字面/术语 | 6/6 | **6/6**（不变） |
| B 同义改写 | 5/6 | **1/6** ⬇ |
| C 中英对照 | 6/6 | **2/6** ⬇ |
| 合计 | 17/18 | **9/18** ⬇ |

**B/C 类从 11/12 掉到 3/12。** 之前的成绩基本是"语料里只有 5 个干扰项"撑出来的。

---

## 2. 真实场景的详细数据

### 2.1 单路召回能力（740 条语料）

| 类型 | 目标排名分布 |
|---|---|
| A 字面/术语 | 全部第 1 位（score 0.64~0.84） |
| B 同义改写 | 第 4、5、11、15、**147** 位 |
| C 中英对照 | 第 10、16、36、**218** 位 |

**两个极端案例**（这两条是关键）：

```
查询：你的 baseline 是什么
目标：改造前的指标是多少，和现在怎么比
top-1（0.519）：你主导排查过一次较严重的线上问题…
目标实际排名：第 147 位（score 0.446）

查询：向量检索的索引结构
目标：vector index 的选择：HNSW 与 IVF 的取舍
top-1（0.450）：…
目标实际排名：第 218 位（score 0.450）
```

`baseline` ↔ `改造前的指标`、`索引结构` ↔ `index 选择` ——
**语义确实相关，但 bge-m3 没抓到。**

### 2.2 recall_k=50 的覆盖率

```
覆盖率: 16/18 = 89%
```

只有两条连 50 条都捞不进来：

- `[B] 你的 baseline 是什么` → 目标第 147 位（超过 50）
- `[C] 向量检索的索引结构` → 目标第 218 位

**其余 16 条都在 50 条候选里。** 所以：
**把 recall_k 从 50 提到 250，覆盖率能到 100%。**

### 2.3 BM25 的贡献

BM25 在 A 类（术语类）全中，在 B/C 类几乎为 0：

```
[A] 召回从71%提到82%      vec=True  bm25=True
[A] HNSW 的 M 参数        vec=True  bm25=True
[B] 缓存击穿怎么处理        vec=True  bm25=True    ← 唯一词汇重合的
[B] 你的 baseline 是什么   vec=False bm25=False
[C] 多头注意力机制         vec=True  bm25=False
[C] 向量检索的索引结构      vec=False bm25=False
```

**结论：BM25 只在"术语字面重合"时有用，对同义改写/中英对照无贡献。**
但它对 A 类是有价值的补充（保证术语一定能被捞到）。

---

## 3. 词面 rerank 为什么更差

试了 6 种纯 Python 的 rerank 打分：

| 策略 | 总 | A | B | C |
|---|---|---|---|---|
| ① 原始向量分（基线） | **9/18** | 6/6 | 1/6 | 2/6 |
| ② IDF 加权重叠 | 5/18 | 5/6 | 0/6 | 0/6 |
| ③ IDF + 位置加权 | 5/18 | 5/6 | 0/6 | 0/6 |
| ④ IDF + 位置 + 短语 | 5/18 | 5/6 | 0/6 | 0/6 |
| ⑤ 向量 0.3 + IDF 0.7 | 5/18 | 5/6 | 0/6 | 0/6 |
| ⑥ 向量 0.4 + IDF 0.35 + 短语 0.25 | 5/18 | 5/6 | 0/6 | 0/6 |

**全部比基线差 4 个点，而且 B/C 类全灭。**

### 为什么

B/C 类的查询和目标**字面完全不同**：

- `baseline` vs `改造前的指标` —— 零 token 重合
- `索引结构` vs `index 选择` —— 零 token 重合

词面打分器给这些正确目标打 **0 分**，反而把 A 类里"字面相近但语义一般"的
候选顶上来。**用错误的信号排序，比不排更糟。**

### 这说明什么

这类查询**必须靠语义精排**，不能用词面。

---

## 4. 待验证：真 cross-encoder 能否解决

### 4.1 原理差异

| | bi-encoder（召回） | cross-encoder（精排） |
|---|---|---|
| 输入 | query、doc **分别**编码 | query + doc **拼在一起** |
| 交互 | 无（只算向量距离） | 有（每个 token 互相注意） |
| 速度 | 快（doc 向量可预计算） | 慢（每次都要过模型） |
| 对同义改写 | 依赖训练时的语义对齐 | 能理解上下文组合 |

`bge-m3` 是 bi-encoder。它对 `baseline` ↔ `改造前的指标` 打 0.446 分，
说明**训练数据里这种对应关系不够强**。

`bge-reranker-v2-m3` 是 cross-encoder，专门为"判断 query 与 doc 是否相关"
训练过，理论上能捕捉这种关系。

### 4.2 环境限制（诚实记录）

尝试过的路径：

| 方案 | 结果 |
|---|---|
| `ollama pull bge-reranker-v2-m3` | ✗ HTTP 500 `file does not exist`（Ollama 官方库没有这个模型） |
| `ollama pull dengcao/bge-reranker-v2-m3` | ✗ 超时 |
| `ollama pull xitao/bge-reranker-v2-m3` | ✗ 超时 |
| `pip install sentence-transformers` + torch | ⏳ 安装中（torch 2.15.0+cpu 已就绪） |

### 4.3 下一步验证计划

```python
# 装好后要跑的实验
from sentence_transformers import CrossEncoder

model = CrossEncoder('BAAI/bge-reranker-v2-m3', max_length=512)
scores = model.predict([(query, doc_text) for doc in candidates])
```

**关键对比**：

| 方案 | 命中率 | 说明 |
|---|---|---|
| A. 向量 top-3 | 9/18 | 基线 |
| B. 向量 top-250 → rerank top-3 | ? | 完整链路 |
| C. 向量+BM25 top-250 → RRF → rerank top-3 | ? | 加 BM25 |

如果 B 明显高于 A，说明 rerank 是解药；
如果 B ≈ A，说明 bge-m3 的语义空间里"这两句话就是不相关"，rerank 也救不了 ——
那要换 embedding 模型（如 bge-large-zh-v1.5）或改用 LLM 做相关性判断。

---

## 5. 给生产代码的当前建议

### 5.1 现在就能改的

**提高 recall_k，但不必上 rerank**

数据支持这个判断：
- 目标 89% 在 top-50 内（提到 250 能到 100%）
- 但 `选 top-3` 只有 50%

所以**当前 `recall_k=15` 偏小**。建议：

```python
# 现在的 bank_select.pick()
candidates = retriever.search_bank(query, k=k, ...)   # k 默认 15

# 建议：先宽召回，再用现有打分（语义×0.8 + 新话题加权×0.2）排序
candidates = retriever.search_bank(query, k=50, ...)
```

理由：`bank_select.pick()` 本身有一层打分（语义相关度 + 话题配额 + 弱项加权），
它已经起到了"精排"的作用 —— 只是候选池太小，好目标进不来。

**成本**：召回 50 条 vs 15 条，pgvector 查询时间从 ~8ms 到 ~15ms，可接受。

### 5.2 不要做的事

- ❌ 不要上等权 RRF（已验证会稀释）
- ❌ 不要用词面 rerank（已验证更差）
- ❌ 不要相信 23 条语料上的成绩

### 5.3 待定的

- ⏳ cross-encoder rerank（等实验数据）
- ⏳ 换更强 embedding（如果 rerank 也救不了）

---

## 6. 复现方式

```bash
cd /mnt/e/桌面/实习项目/interview_agent
export PYTHONPATH='backend:.' IA_REDIS=0

# 1. 基线数据（740 条语料，各方案对比）
.venv/bin/python backend/tests/manual/exp_hybrid_full.py

# 2. 逐条目标排名（看目标到底排第几）
.venv/bin/python backend/tests/manual/diag_exp.py

# 3. recall_k=50 覆盖率 + 混合分数
.venv/bin/python backend/tests/manual/diag_recall50.py

# 4. 六种词面 rerank 对比
.venv/bin/python backend/tests/manual/diag_rerank_value.py
```

输出文件：`backend/tests/manual/hybrid_full_result.json`

---

## 7. 诚实的收尾

这份报告里最重要的不是"混合检索好不好"，
而是**我之前那个 17/18 是怎么来的**：

- 语料 23 条（18 目标 + 5 干扰）
- 只测到 RRF，没测 rerank
- 用等权融合

**三个问题叠加，得出一个自信的、错误的结论，还写进了 README。**

换成 740 条真实语料后，同一个方法从 17/18 掉到 9/18。
这不是方法退化，是之前的测量对象不是真实场景。


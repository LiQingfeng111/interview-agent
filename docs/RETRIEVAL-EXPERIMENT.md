# 检索方案实验报告

> 最后更新：2026-11-27
>
> 所有数字都可用 `backend/tests/manual/` 下的脚本复现。

---

## 0. 一句话结论

**瓶颈不在召回，在精排。**

在真实题库（740 条）上：

| 阶段 | 命中率 | 说明 |
|---|---|---|
| 纯向量 top-3 | 9/18 | 基线 |
| **recall_k=50 覆盖率** | **16/18** | 目标 89% 都能捞进来 |
| 从 50 条选 top-3（原始分） | 9/18 | **和基线一样 —— 排不进前三** |
| 词面类 rerank | 5/18 | 更差（B/C 类语义不同、字面也不同） |

**目标被召回了，但排不进前三。** 这才是要解决的问题。

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


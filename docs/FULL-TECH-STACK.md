# 全量技术说明：开发流程 / 前端 / 后端 / 数据流 / 检索

> 本文所有数字与结论都来自实测（读代码 + 数行数 + 跑测试），不是回忆。
> 数据日期：2026-11-26。

---

## 零、先给结论（一页看懂）

| 问题 | 答案 |
|---|---|
| 前端框架 | **没有任何框架**。原生 JS，无 React/Vue/jQuery，无构建工具 |
| 前端规模 | 6 文件 / 657 行（含 HTML+CSS+JS） |
| 后端框架 | **没有任何 Web 框架**。`http.server` 标准库 |
| 后端技术 | Python 3.12 标准库 + 手写 urllib 调 LLM API + SQLite |
| 后端规模 | 74 文件 / 12,113 行 |
| 第三方依赖 | **0 个**（`langchain-openai` 是后来可选加的） |
| 接口数 | 23 个（`/api/*` 21 个 + 2 个页面路由） |
| 有 embedding 吗 | **没有**。项目里 `embedding` 只作为"技术名词词表"出现 |
| 有向量检索吗 | **没有**。检索靠**关键词 + 字符二元组 Jaccard** |
| 什么时候检索 | 只在开启"联网"时，**每轮出题前**检索一次（同一主题复用） |
| 检索什么 | Bing HTML / Serper / Tavily / Bocha 网页搜索，不是本地文档库 |

**最重要的一句话**：这个项目**不是 RAG 系统**。它没有 embedding、没有向量库、没有文档切分。
它的"检索"是**核对技术事实是否过时**，不是"从知识库里找答案"。

---

## 一、开发流程（按时间线还原）

从文件修改时间能看出真实顺序：

```
11-24 17:37   reports/report_test.html        静态报告雏形（47 行）
11-24 17:38   reports/radar_aud_*.html        雷达图报告（47 行）
11-24 19:41   reports/demo.html               静态演示页（192 行，0 个 fetch）
11-24 21:16   web/index.html                  ← 真前端开始（才出现 fetch）
11-24 21:17   web/admin.html                  后台页
11-24 21:33   web/register.html               注册页
11-24 21:34   web/login.html                  登录页
11-25         接 LangChain、提示词模板化（本次会话）
```

分阶段说明：

| 阶段 | 做了什么 | 产物 |
|---|---|---|
| ① 核心逻辑 | 状态机 + 三角色 + 探针树 + 账本 + 不变量 | 纯 Python，无 UI |
| ② 校验体系 | 8 条不变量、322→356 条测试、cassette 对照 | 可审计 |
| ③ 静态报告 | 跑完一场 → 渲染成一张 HTML（`demo.html`） | 能"看"结果 |
| ④ 真前端 | 4 个页面 + `app.js`，能一问一答 | 能"玩" |
| ⑤ 可选增强 | LangChain 适配器、提示词模板化 | 本次会话 |

**顺序的逻辑**：先让逻辑正确（①②），再让人能看见（③），最后才让人能交互（④）。
这个顺序的好处是——①②阶段就有 356 条测试兜底，④加界面时不会污染判定逻辑。

---

## 二、前端

### 2.1 用了什么技术

**没有任何框架。** 实测：

```
在 app.js / index.html / login.html 里搜索：
  react | vue | angular | jquery | import | require( | https://
  → 命中 0 处
```

只有原生三件套：**HTML + CSS + 原生 JavaScript**，没有构建步骤（无 webpack/vite/npm）。

为什么这样选（`webui.py` 里写明了理由）：

> 这个项目的卖点之一是"零第三方依赖"——clone 下来配个模型就能跑。
> 引入框架会让"换台电脑就跑不起来"的概率大幅上升，
> 而这一层只需要路由 + JSON，标准库的 `http.server` 完全够用。

### 2.2 前端 657 行怎么分布

| 文件 | 行数 | 作用 |
|---|---|---|
| `web/app.js` | 298 | 所有交互：发请求、渲染气泡、上传简历、切页面 |
| `web/admin.html` | 96 | 后台：用户管理、会话列表 |
| `web/register.html` | 84 | 注册 |
| `web/login.html` | 65 | 登录 |
| `web/index.html` | 61 | 主页面骨架（对话区 + 侧栏配置） |
| `web/app.css` | 53 | 样式 |

### 2.3 app.js 的核心只有 3 行

```javascript
const $ = id => document.getElementById(id);

const api = (p, body) => fetch(p, body ? {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
} : undefined).then(r => r.json());
```

整个前端就是围绕这个 `api()` 包装器展开：所有交互都是"调接口 → 拿 JSON → 重渲染"。

### 2.4 index.html 的结构（真实元素 id）

```html
<header>
  <span class="tag" id="engine">未开始</span>        ← 当前引擎状态
  <span class="tag" id="webtag">联网：关</span>      ← 联网开关状态
  <span class="tag" id="banktag">题库 …</span>       ← 题库统计
</header>

<aside>  <!-- 配置区 -->
  <input type="file" id="file" accept=".pdf,.docx,.md,.markdown,.txt">
  <button id="pick">上传简历（PDF / DOCX / MD）</button>
  <textarea id="resume"></textarea>                  ← 简历正文
  <select id="role"></select>                        ← 岗位方向
  <select id="persona"></select>                     ← 面试官风格
  <input type="range" id="max_turns" min="6" max="30" value="16">  ← 轮次
  <input type="checkbox" id="web">   开启联网（追问参考 + 事实核对）
  <input type="checkbox" id="mock">  mock 模式（不调模型，只验流程）
  <input type="checkbox" id="coach"> 评估模式（每轮给评分理由 + 示范回答）
  <button id="start">开始面试</button>
  <button id="endnow">提前结束</button>
  <button id="report">查看报告</button>
</aside>

<main><div id="chat"></div></main>                   ← 对话气泡区
```

### 2.5 前端调用了哪 10 个接口

从 `app.js` 里 grep 出全部 `/api/*`：

```
/api/meta      读岗位、风格、轮次等元数据（填下拉框）
/api/bank      读题库统计（页头"题库 …"那个标签）
/api/upload    上传简历文件（PDF/DOCX/MD）→ 后端解析成文本
/api/start     开始一场面试 → 返回第一问 + session id
/api/answer    交一轮答案 → 返回评分 + 下一问
/api/end       提前结束 → 返回报告
/api/report    读历史报告
/api/history   读历史会话列表
/api/trend     读分数趋势（画图用）
/api/admin     后台管理
```

**前端不做任何判断**：分数、判定、下一问全是后端返回的。前端只负责渲染和转发。

---

## 三、后端

### 3.1 用了什么框架

**没有 Web 框架。** 第 3 行注释写得很直白：

```python
"""Web UI：把命令行面试变成能点、能看进度、能看报告的界面。

为什么用标准库而不是 Flask/FastAPI：
  这个项目的卖点之一是"零第三方依赖"——clone 下来配个模型就能跑。
  引入框架会让"换台电脑就跑不起来"的概率大幅上升，而这一层只需要
  路由 + JSON，标准库的 http.server 完全够用。
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
```

实测搜索 `flask | fastapi | django | aiohttp | tornado | starlette` → **没有一处真实导入**。

### 3.2 后端技术栈（真实清单）

| 类别 | 用了什么 | 说明 |
|---|---|---|
| 运行时 | Python 3.12.3 | WSL Ubuntu 里实测 |
| HTTP 服务 | `http.server.ThreadingHTTPServer` | 标准库，多线程 |
| 模型调用 | 手写 `urllib.request` POST `/chat/completions` | 兼容 OpenAI 协议 |
| 数据库 | `sqlite3` | 标准库，会话持久化 |
| 鉴权 | `hmac` + `hashlib.pbkdf2_hmac` + Cookie | 手写，非 JWT |
| 文档解析 | `pymupdf`（PDF）/ 标准库 zipfile（DOCX） | 可选依赖 |
| 测试 | `pytest` | 356 条通过 |
| LangChain | `langchain-openai`（可选） | 本次会话新增，`IA_LLM_BACKEND=langchain` 才启用 |

### 3.3 23 个接口全表

| 接口 | 方法 | 作用 |
|---|---|---|
| `/`、`/index.html` | GET | 主页面 |
| `/api/health` | GET | 健康检查 |
| `/api/meta` | GET | 岗位/风格/轮次等元数据 |
| `/api/me` | GET | 当前登录用户 |
| `/api/bank` | GET | 题库统计 |
| `/api/history` | GET | 历史会话列表 |
| `/api/report` | GET | 某场报告 |
| `/api/trend` | GET | 分数趋势 |
| `/api/sessions` | GET | 会话列表（后台） |
| `/api/users` | GET | 用户列表（后台） |
| `/api/admin` | GET | 后台数据 |
| `/api/admin/delete` | POST | 删用户 |
| `/api/registration` | GET | 是否开放注册 |
| `/api/start` | POST | 开始面试 |
| `/api/answer` | POST | 交一轮答案 |
| `/api/end` | POST | 提前结束 |
| `/api/upload` | POST | 上传简历 |
| `/api/resume` | POST | 简历解析 |
| `/api/login` | POST | 登录 |
| `/api/logout` | POST | 登出 |
| `/api/register` | POST | 注册 |
| `/api/password` | POST | 改密码 |

### 3.4 后端 12,113 行怎么分（按职责）

| 模块 | 行数 | 职责 |
|---|---|---|
| `interviewer.py` | 322 | 出题官（组 prompt、清洗、去重、兜底） |
| `evaluator.py` | 199 | 评分官（调模型、规整、归一） |
| `planner.py` | 92 | 策划（面试前生成项目/要素计划） |
| `state_machine.py` | 221 | 阶段流转（5 阶段 + 木梯 6 级） |
| `probe_tree.py` | 145 | 探针树（5 要素覆盖追踪） |
| `invariants.py` | 167 | 8 条不变量（审计规则） |
| `ledger.py` | — | 只追加事件账本 |
| `llm.py` | 633 | 模型调用（手写 HTTP + 重试 + 记账） |
| `webui.py` | — | HTTP 路由 + 页面服务 |
| `bank.py` / 题库 | — | 722 题 + 去重 |
| `session.py` | — | 一轮的主流程 |
| 其余 | — | 简历解析、鉴权、持久化、评估工具 |

---

## 四、数据流：一轮到底发生了什么

### 4.1 完整链路（前端一个回车 → 后端一句新问题）

```
[浏览器]
  用户按回车
    ↓  app.js
  fetch('/api/answer', {sid, answer})
    ↓  HTTP POST + JSON
[后端 webui.py]
  do_POST → /api/answer
    ↓
  with_actor()：注入登录态 user
    ↓
  api_answer(payload)
    ↓  SESSIONS[sid] 取出内存里的 InterviewSession
    ↓
  session.step(answer)     ← 核心
    ↓
  persist.save(session)    ← 落盘 SQLite
    ↓
  组装 {question, eval, progress, card}
    ↓  JSON 返回
[浏览器]
  app.js 渲染气泡 + 评分卡
```

### 4.2 `session.step()` 内部：严格的 4 步顺序

代码里的注释把顺序的重要性写清楚了：

```python
def step(self, answer: str) -> str:
    """answer → 评分 → 更新状态机 → 生成下一问

    这个顺序很重要：状态机必须在生成问题之前就看到评分，
    否则木梯永远升不上去。
    """
```

实际是 6 个动作：

| 步骤 | 动作 | 关键点 |
|---|---|---|
| 0 | **联网检索**（可选） | 每轮出题**前**检索一次，追问与验答共用同一份资料 |
| 1 | **评分上一问的答案** | 用 `evaluator.evaluate(prev_q, answer, ...)` |
| 2 | **记录 + 探针树记账** | `mark_covered`、`note_element_attempt` |
| 3 | **推进状态机** | 木梯升级 / 阶段切换（必须看到评分才能升） |
| 4 | **生成下一问** | `interviewer.next_question(sm, last_eval)` |
| 5 | **返回** | 新问题字符串 |

**注意步骤 0**：检索发生在**评分之前**，而且一份资料同时给两个角色用（评分官核对事实、出题官做参考）。这是为了避免同一主题检索两次。

### 4.3 一轮的 token 与成本（Java 版实测对照）

Java 版实测：**一轮 ≈ 3.8k token ≈ 0.0032 元**（出题 + 评分各一次调用）。
Python 版结构相同（也是两次调用），成本量级一致。

---

## 五、检索：什么时候、检什么、怎么检

### 5.1 必须先澄清：这不是 RAG

| RAG 的做法 | 这个项目的做法 |
|---|---|
| 文档切块 → embedding → 存向量库 | **没有** |
| 用户提问 → 向量化 → ANN 检索 top-k | **没有** |
| 检索结果拼进 prompt 当答案来源 | 有"拼进 prompt"，但来源是**网页搜索** |
| 目的：让模型知道私有知识 | 目的：**核对技术事实是否过时 / 候选人是否在吹** |

搜索代码里 `embedding` 这个词**只出现在"技术名词词表"里**：

```python
# websearch.py:73 — 这些是用来判断"搜索结果是否与本领域相关"的词
"token", "embedding", "prompt", "API", "数据库", "延迟", "微调",
```

**数据库里没有向量列，内存里没有向量数组，没有 cosine 计算。** 这是必须如实说清的。

### 5.2 什么时候触发检索

**只有勾选"开启联网"时**（前端 `id="web"` 复选框）：

```python
# session.py step() 第 0 步
refs = ""
if self.web:
    term = self._web_term(sm)      # 从当前阶段/话题推出检索词
    refs = self._web_refs(term)    # 检索 + 质量过滤 + 缓存
    self.interviewer.web_refs = refs
```

每轮一次，主题相同则复用缓存。

### 5.3 检索走哪条链路（四级降级）

```
① Serper API      （SERPER_API_KEY）  质量最好
② Tavily API      （TAVILY_API_KEY）
③ Bocha API
④ Bing HTML 抓取  免 Key，但中文技术词召回质量差
```

代码注释如实标注了第 ④ 条的问题：

> 实测免 Key 的 Bing 对普通中文查询几乎不可用——
> 搜「多 Agent 编排」→ 返回「多（汉语文字）_百度百科」，
> Bing 把中文复合词拆错了。

### 5.4 检索后的三道质量闸（这是设计上最讲究的地方）

搜到结果**不等于能用**。三道过滤：

**闸 1：垃圾源过滤 `is_junk()`**

```python
def is_junk(item):
    url = item.get("url", "").lower()
    if any(h in url for h in JUNK_HOSTS):     # 已知垃圾站
        return True
    return any(w in title for w in JUNK_TITLE_WORDS)  # 标题含垃圾特征词
```

**闸 2：领域相关性 `is_off_domain()`**

```python
TECH_WORDS = ("模型", "训练", "推理", "检索", "向量", "索引", "接口", "系统",
              "算法", "数据", "并发", "缓存", "队列", "服务", "评测", "指标",
              "框架", "Agent", "RAG", "LLM", "token", "embedding", "prompt",
              "API", "数据库", "延迟", "微调")

def is_off_domain(item):
    blob = f"{item.get('title','')} {item.get('snippet','')}"
    return not any(w in blob for w in TECH_WORDS)     # 一个技术词都没有 → 跑题
```

代码注释解释了为什么必须单独判这一条：

> 例：「评估体系」搜到「资产评估协会」。这类结果不含垃圾特征词，但完全答非所问。
> **把它当资料喂给评分官，比不给资料更危险——会生成看似有依据的假结论。**

**闸 3：整体质量分 `quality()`**

```python
def quality(results):
    """可用结果占比。低于 0.5 说明这次检索不可信。"""
    if not results:
        return 0.0
    good = sum(1 for r in results if not is_junk(r) and not is_off_domain(r))
    return good / len(results)
```

调用方拿到 `ok=False` 时的语义是：**"这次没拿到可信资料" → 应当降级（不用资料），而不是硬用**。

### 5.5 检索结果怎么进 prompt

进了两个地方（同一个 `refs` 变量）：

**① 评分官**：用来核对候选人说的技术细节是否准确
**② 出题官**：`web_refs` 槽位，提示词里明确写了使用纪律

```
【联网参考资料】
{...前 800 字符...}
（只用来确认自己的技术判断是否过时，不要照抄，不要提"网上说"。
 如果资料与简历冲突，以简历为准。）
```

三条约束都很关键：**不照抄、不提"网上说"、简历优先**。

### 5.6 唯一用到"相似度"的地方（不是检索，是去重）

项目里确实有相似度计算，但用途是**去重**，不是检索：

```python
# bank.py — 题库去重
def similarity(a, b):
    # 字符二元组 Jaccard
    return len(ga & gb) / len(ga | gb)

def dedupe(items, threshold=0.72):
    """按"题目文本相似度"去重"""
```

```python
# interviewer.py — 防复读
def _is_repeat(q, sm, lookback=6):
    """相似度去重，不是字符串完全相等。
    阈值 0.62：实测小模型会把同一问改两三个词再问一遍"""
```

| 用途 | 方法 | 阈值 |
|---|---|---|
| 题库去重 | 字符二元组 Jaccard | 0.72 |
| 出题防复读 | 字符二元组 Jaccard | 0.62 |
| 关键词同义词映射 | 查表（`JD_SYNONYMS`） | — |
| **向量检索** | **不存在** | — |

### 5.7 话题→提问角度：也是查表，不是检索

```python
def topic_to_question_seed(topic):
    """把考点转成"提问角度"，喂给提问官做参考（不是让模型照抄）。"""
    for role_key, kb in ROLE_KB.items():
        for item in kb["topics"]:
            if item[0] == topic or topic in item[0]:
                return "；".join(item[1])
    return ""
```

纯字典查找。同义词也是手写表：

```python
JD_SYNONYMS = {
    "评测": ["评估", "测试", "eval"],
    "rag":  ["检索", "向量"],
    "agent": ["智能体", "编排", "多智能体"],
}
```

---

## 六、持久化与状态

| 层 | 存什么 | 在哪 |
|---|---|---|
| 内存 | 进行中的 `InterviewSession`（含状态机、探针树、对话历史） | `SESSIONS` 字典 |
| 磁盘 | 每轮结束后的完整会话快照 | SQLite（`sqlite3`） |
| 文件 | 简历上传的解析文本、检索缓存 | 本地目录 |

**服务重启会怎样**：内存里的 `SESSIONS` 丢了，`/api/answer` 返回"会话不存在（服务重启或已过期）"。
但 SQLite 里的历史还在，可以通过 `/api/history` 和 `/api/report` 查看。

这一点和 Java 版不同——Java 版做了"从事件流重建热状态"（`evictAll()` 后能恢复），
Python 版目前是"内存丢了就重新开始"。

---

## 七、这套设计的取舍（面试会被问）

| 决策 | 选择 | 代价 |
|---|---|---|
| Web 框架 | 标准库 | 没有中间件生态、路由要手写、并发靠线程 |
| 前端框架 | 无框架 | 无组件化、状态管理靠手写、复杂交互难维护 |
| 向量检索 | 不用 | 无法做语义匹配；但也不需要——判定靠规则 |
| 联网检索 | 可选、可关 | 每次多一次外网请求 + 摘要成本 |
| 状态存内存 | 是 | 重启丢会话（有 SQLite 兜底历史） |
| 模型只管措辞 | 是 | 需要大量规则代码（12,113 行里的绝大部分） |

**最核心的一条**：`出题交给模型、流程由代码判定`。
这是整个简历里最有含金量的一句话——它意味着**判定结果可复现、可审计**，
而不是"模型今天心情好就给高分"。

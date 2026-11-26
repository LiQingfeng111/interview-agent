# 主线改为 Python + LangChain（Java 版冻结为第二实现）

## 一、决策与理由

| 决策 | 内容 |
|---|---|
| 主线 | **Python 3.12 + LangChain**，代码在 `interview_agent/` |
| Java 版 | **冻结**，保留在 `interview-agent-java/`，作为"第二实现"写进简历/报告，不再加新功能 |
| 语义 | 一律不变：阈值、探针树、木梯、账本、不变量、去重、题库全保持原样 |

为什么不是"重写一个 Python 版"：**Python 版早就存在且完整**。

```
主代码   69 文件 / 11,556 行
测试     27 文件 /  2,534 行
题库     722 题
```

真正要做的只有一件事：把"怎么调模型"这一层换成 LangChain。
这正是"抽象端口"的价值——**业务代码一行没动**。

## 二、改了什么（3 个文件）

| 文件 | 作用 | 规模 |
|---|---|---|
| `llm_langchain.py`（新增） | `LangChainLLM(BaseLLM)`：用 `langchain_openai.ChatOpenAI` 实现 `chat()` | 197 行 |
| `llm.py`（改） | 加 `_make_llm()`，按 `IA_LLM_BACKEND` 选后端；三处 return 换成它 | +20 行 |
| `tests/test_llm_langchain.py`（新增） | 对账测试：消息转换、json_mode、usage 字段、重试分类、开关生效 | 6 条 |

`build_llm()` 有 30 多处调用点，所以用**环境变量**而不是加函数参数：

```bash
IA_LLM_BACKEND=native     # 默认：原来手写的 urllib 实现
IA_LLM_BACKEND=langchain  # 新：LangChain 实现
```

出问题一行切回去——这就是"可回退"。

### 三个刻意的取舍

| 取舍 | 选择 | 原因 |
|---|---|---|
| 重试 | `ChatOpenAI(max_retries=0)`，重试自己写 | LangChain 自带重试的退避与次数和 native 不同，测试无法逐条对齐 |
| JSON 模式 | OpenAI 原生 `response_format={"type":"json_object"}` | `with_structured_output()` 会插入自己的 schema 提示词，**改变了模型看到的输入**，违反"提示词不变" |
| usage 字段 | 把 `usage_metadata` 翻译成 `prompt_tokens/completion_tokens` | 账本读的是后两个名字，不翻译则两后端的成本报表一个有、一个没有 |

## 三、验证结果

### 环境（WSL Ubuntu）

```
Python 3.12.3
venv  : interview_agent/.venv（--without-pip 建的，绕开 PEP 668）
pip   : 26.3（用 get-pip.py 装进 venv）
langchain-core   1.8.4
langchain-openai 1.8.5
pytest           9.2.0
pymupdf          （补上 PDF 解析，原来 4 条用例因缺库失败）
```

### 测试

```
改之前（基线）        317 passed, 4 failed（缺 pymupdf）, 1 skipped
改之后 + 补库         330 passed, 1 skipped
连跑 3 次            330 / 330 / 330（稳定）
```

### 双后端逐项对账（真实 HTTP，打到仓库自带的假 OpenAI 服务）

```
langchain 后端: LangChainLLM
native    后端: OpenAICompatibleLLM
文本一致  : True | 这个项目最初要解决什么问题？为什么当时必须做？
JSON 一致 : True
关键字段  : verdict=partial  score字段=5  covered=['目标']
结论: LangChain 后端与 native 后端输出完全一致 ✅
```

日志里 usage 也照旧落盘：

```json
{"task":"parity_json","model":"fake-qwen-test","attempt":1,"ok":true,
 "ms":1,"usage":{"prompt_tokens":10,"completion_tokens":20,"total_tokens":30}}
```

诚实说明：Windows 侧的 relay（`127.0.0.1:18326`）从 WSL 连不上
（WSL2 的 localhost 转发问题），所以**真实模型的 JSON 对账还没做**，
目前用仓库自带假服务证明"两条链路行为一致"。

## 四、顺手修掉的一个真实并发缺陷

全量测试跑第二遍才偶发失败：`test_auth.py::TestToken::test_roundtrip` 返回 `None`。

**根因**：`auth._secret()` 是"先看文件在不在，不在就写"，典型 TOCTOU 竞争——
两个调用同时判断"不存在"，各自生成密钥并互相覆盖，于是先签发的那张票验不过，
表现成"偶发的登录失效"。

**修法**（`auth.py`）：

1. 写临时文件 → `os.link()` 原子占位，只有一个能成功；
2. 抢输的先读赢家那份，能用的就采用（**并发必须收敛到同一把密钥**）；
3. 只有读到的是空/短文件才覆盖修复（`MIN_SECRET_BYTES = 16`）；
4. 无论如何最后都从磁盘读一次，保证"签"与"验"用同一把密钥。

新增 3 条用例锁住它（`tests/test_auth_secret.py`）：并发 16 线程收敛、空文件重新生成、已存在密钥稳定。

## 五、怎么跑

```bash
# 进 WSL
wsl -d Ubuntu

cd "/mnt/e/桌面/实习项目/interview_agent"

# 原生后端（默认）
.venv/bin/python -m pytest -q

# LangChain 后端
IA_LLM_BACKEND=langchain .venv/bin/python -m pytest -q

# 真正开始一场面试（LangChain 后端）
IA_LLM_BACKEND=langchain .venv/bin/python run.py --llm local --base-url http://<host>:18326/v1
```

## 六、还没做

| 项 | 说明 |
|---|---|
| 真实模型下的 JSON 对账 | WSL 连不上 Windows relay，需要 host IP 或端口转发 |
| LangChain 原生能力 | 仍用手写提示词拼接；`ChatPromptTemplate`、LCEL、`ChatMemory` 都还没用上（**这是"用 LangChain"和"只是拿它发请求"的分界线**） |
| 前端 / 登录 | Python 版已有 `web/`（index/login/admin + app.js 16.5KB），接上即用 |
| 语音 / RAG | 未开始 |

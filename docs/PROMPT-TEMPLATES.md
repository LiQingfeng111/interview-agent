# 提示词模板化：从"拼字符串"到"具名槽位 + LCEL"

## 一、为什么要做

原来 `Interviewer._build_prompt()` 是 69 行的 `if/elif` + `parts.append(...)`。
问题不在长度，而在于**可变部分藏在代码里**：

| 以前 | 现在 |
|---|---|
| 想知道第 5 题的 prompt 带没带 checklist | 打断点，看 `parts` 列表 |
| 想改一句话术 | 读 69 行才知道它插在哪 |
| 想做 A/B（去掉 checklist 看效果） | 改代码分支 |
| 想看某次请求消耗多少 token、谁占大头 | 只能看总数 |

模板化之后，每个可变部分都是**有名字的槽位**，骨架一眼可见。

## 二、做了什么

| 文件 | 内容 |
|---|---|
| `prompt_templates.py`（新，245 行） | 三个模板骨架 + 槽位组装 + LCEL 链 |
| `interviewer.py`（改） | `_build_prompt` 分成 template / legacy 两条路径，`IA_PROMPT_ENGINE` 切换 |
| `tools/capture_prompt_golden.py`（新） | 重构**前**拍 9 条快照，作为"文本不变"的基准 |
| `tests/test_prompt_templates.py`（新，22 条） | 模板 vs legacy 逐字一致 + 黄金快照 + 槽位完整性 |
| `tools/prompt_inspect.py`（新） | 打印每条用例的槽位：谁有内容、谁为空、占多少字符 |
| `tests/test_prompt_inspect.py`（新，4 条） | 锁住诊断工具的输出结构 |

### 模板骨架（项目深挖阶段）

```python
PROBE_TEMPLATE = """当前阶段：{phase}（{phase_desc}）
{ladder}当前要深挖的项目：{project}
{followup}{retry}{checklist}{seed}{recent}{asked}请提出下一个问题。"""
```

8 个槽位：`ladder`（层级与目标）、`project`、`followup`（评分官建议）、
`retry`（重追问指令）、`checklist`（五要素提醒）、`seed`（计划里的提问草稿）、
`recent`（最近对话）、`asked`（已问过的问题）。

## 三、最关键的一步：先拍快照，再改代码

提示词差一个字，模型行为就会漂，而这种漂移**单元测试看不出来**——没人会去断言 300 字 prompt 的全文。

所以顺序是：

```
1. 先跑 tools/capture_prompt_golden.py   →  tests/golden/prompt_build.json（9 条快照）
2. 再写模板实现
3. 逐字比对，直到差异为空
```

快照的 9 条覆盖：开场 / 自我介绍 / 深挖首问 / 深挖重追问 / 带 seed / 技术题库题 / 带 JD / 带联网资料 / 反问。

## 四、抓到的两个字符级差异

第一次对比就崩了，两处差异都是**换行语义**：

```
probe_first   legacy 482 字符 / template 481 字符   ← 少了 1 个字符
  @@ -5,3 +5,2 @@
   如果评估为 fail，请换一个更具体的角度重新问同层级；…
  -
   当前要深挖的项目：课程问答助手

tech_bank     legacy 235 / template 236            ← 多了 1 个字符
  -请结合候选人前面的回答，提一个具体问题。
  +请结合候选人前面的回答，提一个具体问题。\n
```

原因：旧实现是 `"\n".join(parts)`，语义是"每段后面跟换行、**最后一段除外**"。
我第一版槽位帮助函数里加了 `rstrip("\n")`：

- 深挖阶段：`INTERVIEWER_LADDER_HINT` 本身就以换行结尾，剥掉后少了那个空行；
- 技术阶段：最后一个槽位不空时，模板末尾多留了一个 `\n`。

修法：

1. `_nl()` **不做 rstrip**，只负责"内容 + 一个换行"；
2. 渲染完成后统一 `rstrip("\n")`，对应"最后一段不带换行"。

修完 9 条用例长度全部一致，diff 为空。

> 这两处差异都只有 1 个字符。它们在功能测试里完全不可见，但只要算错，
> 模型看到的输入就和历史版本不同——这叫"静默行为漂移"。

## 五、LCEL 链：为什么中间那步要包我们自己的端口

```python
prompt | RunnableLambda(_call) | StrOutputParser()
```

`_call` 里调的是 `LlmPort.chat()`，不是直接 `ChatOpenAI`。

更"像 LangChain"的写法是 `llm | StrOutputParser()`，但那样会**绕过 `LLMLogger`**，
账本里少一条记录、usage 拿不到、mock 后端也接不上。
对一个要做审计的面试系统，**可追溯比"看起来原生"重要**。

踩到的坑：管道传进 `_call` 的是 `ChatPromptValue`，不是 dict。

```
TypeError: 'ChatPromptValue' object is not subscriptable
```

改成从 `prompt_value.messages` 逐个取 `.type` / `.content`。

## 六、真正的收益：可见性

`tools/prompt_inspect.py` 的输出（真实运行结果）：

```
=== probe_retry · PROJECT_PROBE · 572 字符 ===
  ✓ ladder      127 字符  当前追问层级：2（怎么做） / 该层级的目标：确认细节落到实现层…
  ✓ project       6 字符  课程问答助手
  ✓ followup     29 字符  评分官建议的追问方向：追问 500 字这个切块是怎么定的
  ✓ retry        52 字符  注意：这是同一层级的再次追问…
  ✓ checklist   191 字符  【本项目必须覆盖的五要素】 / - 目标：…
  · seed          0 字符
  ✓ recent       75 字符  最近对话： / 问：这个项目要解决什么问题？…
  ✓ asked        48 字符  已经问过、不要重复的问题：…
  空槽位：seed（这些内容本次不需要）

=== tech_web · TECH_QA · 151 字符 ===
  ✓ topic         4 字符  向量检索
  · bank/points/seed/jd_keywords/jd_hint 均为空
  ✓ web          99 字符  【联网参考资料】 / HNSW …
```

以前这些要靠打断点，现在一条命令就能看清——**而且 consume 的 token 立刻能对上**：
`checklist` 191 字符 + `ladder` 127 字符占了大头，`seed`/`web` 是可选增强。

## 七、验证

```
模板一致性测试           22 passed
诊断工具测试              4 passed
全量（template 引擎）    356 passed, 1 skipped
全量（legacy 引擎）      356 passed, 1 skipped    ← 回退路径同样可用
全量（langchain + template）356 passed, 1 skipped
```

## 八、怎么用

```bash
cd "/mnt/e/桌面/实习项目/interview_agent"

# 默认走模板
.venv/bin/python -m pytest -q

# 回退到旧的拼接实现
IA_PROMPT_ENGINE=legacy .venv/bin/python -m pytest -q

# 看槽位明细
PYTHONPATH=".:tests" .venv/bin/python tools/prompt_inspect.py
PYTHONPATH=".:tests" .venv/bin/python tools/prompt_inspect.py --case tech_bank --json
```

## 九、还没做的

| 项 | 说明 |
|---|---|
| 其余 5 处消息构造 | `coach.py`(4)、`resume.py`(4)、`planner.py`(2) 还是手拼字符串，可复用同一套方法 |
| 真实模型的 prompt 对账 | 缺 relay 可达性（WSL→Windows 的 localhost 转发） |
| `ChatMemory` 接入 | 现在记忆是手写的 `memory.py`，可以换成 LangChain 记忆接口 |
| 前端 | `web/` 已存在（index/login/admin + app.js 16.5KB），接上即用 |

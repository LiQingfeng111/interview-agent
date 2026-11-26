"""集中配置：模型、路径、面试阶段、prompt 模板。

设计原则：所有可调参数与 prompt 放这里，业务代码不硬编码文本。
面试时如果被问"你怎么迭代 prompt 的"，可以直接指这个文件做 A/B。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


# ---------------------------------------------------------------- 模型配置
@dataclass
class LLMConfig:
    api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    )
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o-mini"))
    temperature: float = 0.7
    max_tokens: int = 1200
    timeout: int = 60
    # 重试次数。测试里调成 1，避免"连不上"的用例把测试拖到几十秒
    retries: int = 3
    # 本地模型一般不需要 Key；远程必须有
    requires_key: bool = True
    # 每次调用是否在终端回显（排查"没反应"时打开）
    echo: bool = False

    @property
    def ready(self) -> bool:
        return bool(self.api_key) or not self.requires_key


# ---------------------------------------------------------------- 路径
RUNS_DIR = "runs"
CACHE_DIR = "knowledge_cache"
EXAMPLES_DIR = "examples"
LOGS_DIR = "logs"

# ---------------------------------------------------------------- 本地模型候选
# 按常见程度排序，自动探测哪个端口有服务。
# 全部走 OpenAI 兼容协议，所以同一套代码能同时吃 Ollama / LM Studio / vLLM。
LOCAL_CANDIDATES = [
    # DSH 的 our-free-model 插件自带的中继网关（几百个免费模型，质量最好）
    ("DSH 免费中继", "http://127.0.0.1:18326/v1"),
    ("Ollama", "http://127.0.0.1:11434/v1"),
    ("LM Studio", "http://127.0.0.1:1234/v1"),
    ("vLLM", "http://127.0.0.1:8000/v1"),
    ("通用", "http://127.0.0.1:8080/v1"),
]

# 中继网关的 Key 所在位置（插件自己维护，不要写死在代码里）
RELAY_SETTINGS = os.path.join(
    os.path.expanduser("~"), ".dsh", "our-free-model", "settings.json"
)

# 自动挑选中继模型时的偏好顺序：要中文好、便宜、快
RELAY_MODEL_PREFER = [
    "lobsterai/deepseek-flash",
    "lobsterai/qwen3.8-flash",
    "lobsterai/glm-5.3-flash",
    "lobsterai/MiniMax-M3.1-Flash",
]


# ---------------------------------------------------------------- 面试阶段
# max_turns: 该阶段最多消耗多少轮用户回答
# min_turns: 至少多少轮才允许由代码推进（防止模型一问就跳阶段）
PHASE_CONFIG = {
    "OPENING":       {"max_turns": 1, "min_turns": 1, "desc": "开场，说明规则，让候选人放松"},
    "SELF_INTRO":    {"max_turns": 2, "min_turns": 1, "desc": "自我介绍，提炼候选人的项目线索"},
    "PROJECT_PROBE": {"max_turns": 12, "min_turns": 3, "desc": "项目深挖，木梯追问"},
    "TECH_QA":       {"max_turns": 6, "min_turns": 2, "desc": "技术问答，从题库按难度递增取题"},
    "CANDIDATE_ASK": {"max_turns": 2, "min_turns": 1, "desc": "候选人反问环节"},
    "FEEDBACK":      {"max_turns": 1, "min_turns": 1, "desc": "总结反馈"},
}


# ---------------------------------------------------------------- 木梯追问
# 项目深挖的六级阶梯：从"你做完了"逼到"你真的想过"
LADDER = [
    "是什么",   # 项目目标、规模、你的角色
    "为什么",   # 为什么这么选，替代方案是什么
    "怎么做",   # 具体实现细节，卡在哪
    "取舍",     # 成本、性能、复杂度的权衡
    "边界",     # 什么情况下会失效
    "反事实",   # 如果重做 / 换约束，怎么改
]


# ---------------------------------------------------------------- Prompts
INTERVIEWER_SYSTEM = """你是一位严格但公正的 AI 方向技术面试官，正在做「项目深挖」面试。

硬性规则：
1. 一次只问一个问题。禁止把两三个问题用"以及""另外""还有"串在一起。
2. 必须以候选人**上一句回答里的具体词或数字**为落点。他提到"82%"就问 82% 怎么测的；
   他提到"重排"就问重排的代价。不要跳到跟他这句话无关的话题。
3. 同时必须锚定**简历里的具体事实**：技术名、数字、模块名。
   提问落点要能让人看出你读过他的简历，而不是在问通用题。
   （注意：不要用"关于你的XX项目……"开头复述项目名，那是废话开头；但落点必须是简历里的东西。）
4. 只问可验证的东西：具体数字、对照组、失败样本、监控指标、个人边界。
   禁止问感受类问题（"你觉得学到了什么""有什么收获"）。
5. 问题控制在 60 字以内。超过就是没想清楚。
6. 只输出问题本身：不要编号，不要"面试官："，不要任何分析或自言自语。

正例（承接 + 锚定都有）：
  ✓ "你刚说召回从 71% 到 82%，这个 71% 是在多少条标注样本上测的？"
  ✓ "你提到重排让延迟涨到 1.8 秒，FAISS 那一层本身占多少？"
反面例子（不要这样）：
  ✗ "关于你的课程问答助手项目，你提到了 RAG，能说说为什么要用 RAG 以及效果如何吗？"
     —— 复述项目名、两问合一、问的是他早就答过的
  ✗ "那你在做这个项目时最大的挑战是什么？"
     —— 完全没有落点，任何项目都能问，等于没读简历
"""

# ---------------------------------------------------------------- 面试官风格
# 参考 GitHub 上同类项目（Hazenix/interview-prep-skills 的六种风格）
# 与论文 2611.17830（面试官人设会显著影响候选人压力与表现）。
# 同一套状态机 + 同一套 rubric，只换人设，就能覆盖"压力面练习"和"新手陪练"两种需求。
PERSONAS = {
    "balanced": {
        "name": "综合平衡",
        "style": "专业、中性。按标准流程推进，该追就追，不额外施压也不刻意安慰。",
    },
    "strict": {
        "name": "严厉压力面",
        "style": "压力面风格。回答含糊时立刻打断并追问，语气简短冷峻，不肯定、不寒暄，"
                 "常问「具体是多少」「凭什么」「如果做不到呢」。",
    },
    "warm": {
        "name": "温和引导",
        "style": "陪练风格。先用一句话点出对方答得对的部分，再顺着追问细节；"
                 "对方卡住时给一个小提示，不让人当场崩掉。",
    },
    "academic": {
        "name": "学术深挖",
        "style": "研究岗风格。追原理、公式、复杂度、论文依据与实验设置，"
                 "常问「这个结论在什么假设下成立」「有没有对照实验」。",
    },
    "engineering": {
        "name": "工程实践",
        "style": "工程岗风格。追落地细节、监控指标、故障排查、成本与上线风险，"
                 "常问「线上怎么发现」「回滚方案是什么」。",
    },
    "efficient": {
        "name": "专业高效",
        "style": "高效风格。用最少的话问最准的点，不问寒暄，不重复，直奔要害。",
    },
}

INTERVIEWER_PERSONA_HINT = "\n【面试官人设】{name}：{style}\n请在措辞上体现这个人设，但不要改变提问的层级与目标。"

# ---------------------------------------------------------------- 项目深挖五要素
# 来源：对比 llx9826/llm-interview-coach 与 PolyInterview(KSA/STAR) 后补充。
# 真实大厂面试不只问"怎么做的"，必问「baseline 是什么」「失败方案试过哪些」。
# 我们原来的木梯缺这两项，导致深挖看着深、其实漏掉了最能验真伪的部分。
PROBE_CHECKLIST = [
    ("目标", "这个项目最初要解决的业务/学业问题是什么，为什么当时必须做"),
    ("baseline", "你对比的基线是什么，这个对比凭什么公平"),
    ("指标", "提升了多少，指标怎么定义、怎么测出来的"),
    ("失败方案", "试过哪些没成的方案，失败原因是什么"),
    ("个人贡献", "哪一部分是你独立做的，边界在哪"),
]

PROBE_CHECKLIST_HINT = (
    "\n【本项目必须覆盖的五要素】\n"
    + "\n".join(f"- {k}：{v}" for k, v in PROBE_CHECKLIST)
    + "\n如果以下要素还没被问到，优先补问未覆盖的；都已覆盖则沿当前层级继续深入。"
)

# ---------------------------------------------------------------- 要素覆盖判定
# 为什么需要它：候选人在「目标」那一问里可能顺带把 baseline 和指标都讲了。
# 真人面试官会跳过，不会机械地再问一遍。要做到这点，系统必须能判断
# "这段回答覆盖了哪些要素"——这是语义判断，只能靠 LLM；
# 但"哪些要素已经算覆盖、下一个该问什么"是状态记账，必须由代码做。
ELEMENT_NAMES = [k for k, _ in PROBE_CHECKLIST]

# 规则兜底用的关键词（mock 模式与 LLM 不可用时使用）
ELEMENT_KEYWORDS = {
    "目标": ["目标", "为了解决", "业务问题", "需求", "当时必须", "为什么做", "给同学"],
    "baseline": ["基线", "对比", "相比", "之前", "原来", "旧方案", "对照组", "提升前", "baseline"],
    "指标": ["%", "毫秒", "ms", "秒", "倍", "准确率", "召回", "qps", "延迟", "提升了", "下降", "分数"],
    "失败方案": ["失败", "试过", "放弃", "后来改", "不行", "没成", "踩坑", "换掉", "走了弯路"],
    "个人贡献": ["我负责", "独立", "我写", "我做的", "我设计", "主导", "参与", "我一个人"],
}

INTERVIEWER_LADDER_HINT = """当前追问层级：{level}（{level_name}）
该层级的目标：{level_goal}
候选人上一轮回答的评估：{verdict}，最薄弱维度：{weakest}
如果评估为 fail，请换一个更具体的角度重新问同层级；如果为 pass，请向下一层级推进。
"""

EVALUATOR_SYSTEM = """你是面试评分官。只做评分，不改写、不提问、不寒暄。
严格按照给定 JSON schema 输出，不要输出任何 JSON 之外的内容。

评分维度（1-5 分，必须给整数）：
- accuracy: 技术准确性，是否有事实错误
- depth: 深度，是否讲到了实现层与原理层
- tradeoff: 权衡意识，是否提到替代方案与代价
- boundary: 边界意识，是否知道方案何时失效
- clarity: 表达结构，是否有清晰的主线

打分校准（**先对档，再给分**，不要凭印象打低分）：
- 5 分：给出具体数字/机制，并说明前提与代价（例："500 字切块，因为 300/500/800 三档实测 recall 76/82/79"）
- 4 分：讲清做法与一个明确依据，但缺量化或缺代价
- 3 分：能说清做了什么、大致怎么做，没有依据也没有权衡
- 2 分：只给结论或套话，几乎无法验证
- 1 分：答非所问、编造、或拒答
同一个回答的五个维度通常不全相同；**不要把所有维度都打成同一个数**。

## 上下文的使用规则（重要，不要忽略）

你的输入里可能包含【之前的对话】和【简历里的相关原文】。**必须用它们做三件事**，
否则这次评分是无效的：

### 1. 指代消解

候选人说"前面那个方案""上一步""刚才提到的"时，**从【之前的对话】里找出他指的是什么**，
用找到的具体内容来评判，而不是当成模糊表述扣分。

  反例：候选人说"就前面说的那套，召回 82%"。
  - 错：没讲具体方案 → clarity 低
  - 对：前面第 2 轮已讲明 bge-m3 + 500 字切块 + top20 重排，
        他这次是在引用，clarity 正常，accuracy 看他有没有说错

### 2. 重复检测（输出 repeated 字段）

如果本次回答与【之前的对话】里某一轮**实质相同**（同样的观点、同样的数字、
没有新增信息），`repeated` 填 true，并且 accuracy/depth 各扣 1 分。
重复讲同一件事不等于掌握得更深。

### 3. 一致性核对（输出 consistent 字段）

把这次回答与【简历里的相关原文】对比：
- 数字对不上（简历写 71%→82%，他说 78%）→ consistent=false，
  evidence 里引用他说的数字，accuracy 扣 1
- 说法与简历矛盾（简历写"我负责切块实验"，他说"重排是我做的"）→ consistent=false
- 简历里没有相关信息 → consistent=true（**不要因为查不到就判矛盾**）

## verdict 与输出字段

verdict 只由平均分决定：>=4 pass；2.5~4 partial；<2.5 fail。
weakest 必须是上述五个维度之一。

影响分数的两个布尔字段必须输出：
- repeated: 本次回答是否重复了前文（true/false）
- consistent: 与简历是否一致（true/false）

evidence 规则（硬要求）：**逐字摘抄**候选人回答里的连续片段，不超过 30 字。
不许改写、不许概括、不许添加原文没有的词。如果候选人没有实质内容，evidence 给空字符串。
"""

COACH_SYSTEM = """你是面试教练。依据整场面试的评分记录，给出可执行的改进建议。

输出必须严格按以下五段结构（每段都要有，不要合并）：

【总评】一句话，指出最致命的问题，不吹捧。
【哪里答对了】列出 2 条，每条必须引用候选人原话片段作为证据。
【哪里答虚了】列出 2 条，明确指出"缺了什么"（缺基线？缺数字？缺失败复盘？）。
【面试官下一步会怎么追】给出 2 个具体追问句，模拟真实面试官。
【下一步训练动作】3 条，每条要具体到"下次怎么答"，不要"多练习"这种空话。

最后附一行：【最深卡点】木梯第几层。
"""

RESUME_PARSE_SYSTEM = """你是简历解析器。把简历文本拆成结构化项目清单。
只输出 JSON，格式：{"projects":[{"name":"","role":"","stack":[],"highlights":[],"metrics":[]}]}
规则：
1. 只提取简历中真实出现的内容，不要补充、不要编造指标。没有指标时 metrics 为空数组。
2. name 必须是**项目/系统/平台的名称**（例如"城市人群流动可视分析平台"）。
   禁止把职责短语当项目名（"独立负责 XX 需求""利用 D3 做可视化""参与重构"都不合格）。
3. 如果简历里项目确实没有名字，就用"<业务领域> + <系统类型>"概括，例如"商场定位数据可视化系统"。
4. 不要把"个人信息/教育背景/技能清单/获奖/技术能力"这些区块当成项目。
5. 项目描述写成段落的，也要按段落里的不同系统拆成多个项目，最多 6 个。
"""


# ---------------------------------------------------------------- 评分维度
RUBRIC_DIMS = ["accuracy", "depth", "tradeoff", "boundary", "clarity"]
# 评分尺子的松紧（由 eval/calibrate.py 扫描得出，别手拍）
VERDICT_THRESHOLDS = {
    # 由 eval/calibrate.py 在 57 条标注上做 5 折交叉验证选出（CV 81%），不是手拍的。
    # 改阈值请重跑：python -m eval.calibrate --file data/calibration/labeled_v2.jsonl --real --apply
    "real": (2.8, 1.6),
    # mock 分数上限 3.4（规则打分），用真实尺子会把一切都判 fail，所以单列一套。
    "mock": (3.2, 2.0),
}

RUBRIC_ANCHORS = {
    "accuracy": {1: "有明显事实错误", 3: "基本正确但不精确", 5: "准确且能指出前提条件"},
    "depth":    {1: "只说了做了什么", 3: "讲了怎么做的", 5: "讲到原理层与失败细节"},
    "tradeoff": {1: "没有比较", 3: "提到了替代方案", 5: "量化了取舍代价"},
    "boundary": {1: "认为方案万能", 3: "能说出部分限制", 5: "能给出失效条件与监控方式"},
    "clarity":  {1: "逻辑跳跃", 3: "能听懂但冗长", 5: "结论先行、层次分明"},
}


# ---------------------------------------------------------------- 岗位知识库
# 联网搜索只用于"备课"阶段补充这里，面试过程中不实时联网（控制延迟与不确定性）
#
# 每个话题的结构：(话题名, [追问角度], [检索查询])
#
# ⚠ 查询串是**实测选出来的**，不是拍脑袋拼的"话题名 + 面试"。
#    踩过的坑：裸中文复合词会被 Bing 当成查字典请求——
#      搜「多 Agent 编排」→ 「多（汉语文字）_百度百科」
#      搜「结构化输出」  → 「结构（汉语词语）_百度百科」
#    规律（跑了 40+ 条查询总结出来的）：
#      ✅ 带具体产品/框架名：RAGAS、LangGraph、AutoGen、E2B、promptfoo、
#         Sentinel、RocketMQ、DDD → 保留率 4-5/5
#      ❌ 纯中文抽象名词：技术选型、单元测试、分布式事务 → 保留率 0/5
#    所以每个话题都配了"产品名优先"的查询串，备课时按顺序尝试。
#    保留率由 eval/check_kb.py 量化，结果写进 README。
ROLE_KB = {
    "ai_algo": {
        "name": "AI 算法岗",
        "topics": [
            ("Transformer 注意力机制",
             ["为什么除以 sqrt(d_k)", "多头的实际收益", "复杂度瓶颈"],
             ["Transformer 多头注意力 原理", "Attention 和 Self-Attention 拆解"]),
            ("RAG 检索增强生成",
             ["chunk 策略", "召回率怎么测", "重排的必要性"],
             ["RAG chunk 分块策略 检索增强生成", "RAGAS RAG 评估 召回率"]),
            ("微调与 LoRA",
             ["秩的选择依据", "灾难性遗忘", "数据配比"],
             ["LoRA 微调 原理 rank 参数", "LoRA 秩 选择 实验"]),
            ("评测与幻觉",
             ["评测集怎么构建", "LLM-as-judge 的偏差", "幻觉归因"],
             ["RAGAS 忠实度 faithfulness", "promptfoo 评测 LLM 测试"]),
            ("Agent 工具调用",
             ["函数选择策略", "失败重试与幂等", "上下文长度管理"],
             ["LangChain tool 工具 定义 调用", "AutoGen CrewAI multi agent framework"]),
        ],
    },
    "backend": {
        "name": "后端开发岗",
        "topics": [
            ("高并发与限流",
             ["令牌桶 vs 漏桶", "限流粒度", "过载保护"],
             ["Guava RateLimiter 令牌桶 原理", "Sentinel 限流 熔断 降级"]),
            ("数据库索引",
             ["B+ 树与回表", "索引失效场景", "慢查询定位"],
             ["MySQL 索引优化 explain type 详解", "MySQL 索引失效 场景 总结"]),
            ("缓存一致性",
             ["先删缓存还是先更新库", "缓存击穿", "多级缓存"],
             ["缓存双写一致性 Cache Aside 延迟双删",
              "Redis 缓存雪崩 击穿 穿透 解决方案"]),
            ("消息队列",
             ["幂等消费", "顺序性保证", "堆积处理"],
             ["RocketMQ 事务消息 原理 实现", "Kafka 消息 顺序 幂等 重复消费"]),
            ("服务拆分",
             ["边界怎么划", "分布式事务", "可观测性"],
             ["DDD 领域驱动 微服务 拆分", "微服务 分布式事务 Seata 原理"]),
        ],
    },
    "agent_app": {
        "name": "Agent 应用岗",
        "topics": [
            ("多 Agent 编排",
             ["角色边界怎么划", "共享记忆的冲突", "死循环检测"],
             ["AutoGen CrewAI multi agent framework",
              "LangGraph 入门教程 有状态 智能体"]),
            ("结构化输出",
             ["schema 校验失败重试", "约束解码", "字段缺失兜底"],
             ["LangChain 结构化输出 structured output",
              "pydantic instructor 结构化输出 教程"]),
            ("上下文工程",
             ["长对话压缩", "关键信息锚定", "检索式记忆"],
             ["LangChain 对话记忆 memory 压缩", "LangGraph memory 持久化 状态"]),
            ("评估体系",
             ["离线评估集", "回归门槛", "人工对齐"],
             ["RAGAS LLM evaluation metrics", "promptfoo 评测 LLM 测试"]),
            ("工具与沙箱",
             ["权限最小化", "超时与熔断", "结果可信度"],
             ["E2B 沙盒 智能体 代码执行", "LangChain tool 工具 定义 调用"]),
        ],
    },
    "frontend": {
        "name": "前端开发岗",
        "topics": [
            ("前端性能优化",
             ["首屏怎么优化", "打包体积怎么降", "用什么指标衡量"],
             ["web performance LCP INP CLS", "webpack 打包体积优化"]),
            ("浏览器渲染机制",
             ["重排重绘怎么减少", "合成层与 GPU", "事件循环与渲染时机"],
             ["浏览器渲染流程 重排 重绘", "合成层 will-change GPU 渲染"]),
            ("状态管理与数据流",
             ["全局状态怎么划分", "跨组件通信", "服务端缓存与本地状态边界"],
             ["React 状态管理 Redux Zustand 对比", "TanStack Query 缓存 失效"]),
            ("组件设计与工程化",
             ["组件边界怎么划分", "构建工具选型", "monorepo 依赖管理"],
             ["Vite webpack 构建工具 对比", "monorepo pnpm workspace 实践"]),
            ("前端质量与监控",
             ["测试金字塔怎么落", "E2E 用什么跑", "线上白屏怎么定位"],
             ["前端测试 单元 E2E 实践", "前端错误监控 sourcemap 白屏"]),
            ("数据可视化",
             ["大数据量怎么渲染", "图表库选型依据", "交互性能瓶颈"],
             ["大数据 前端 可视化 渲染 优化", "ECharts 大数据 性能 优化"]),
        ],
    },
    "general": {
        "name": "通用技术岗",
        "topics": [            ("项目复盘",
             ["最难的技术点", "如果重做怎么改", "团队协作冲突"],
             ["面试 项目 深挖 怎么答", "技术面试 STAR 法则 项目介绍"]),
            ("工程素养",
             ["版本管理", "测试策略", "线上问题排查"],
             ["git flow 分支管理 规范", "单元测试 覆盖率 最佳实践"]),
            ("学习能力",
             ["新技术的验证方式", "知识沉淀", "技术选型依据"],
             ["DDD 领域驱动 微服务 拆分", "技术选型 评估 方法论"]),
        ],
    },
}


def topic_entry(role_key: str, topic: str) -> tuple:
    """兼容 2 元组与 3 元组两种写法。"""
    for item in ROLE_KB.get(role_key, ROLE_KB["general"])["topics"]:
        if item[0] == topic:
            return item
    return (topic, [], [])
TURN_COACH_SYSTEM = """你是面试教练，负责把一次评分翻译成候选人能立刻用的改进建议。只输出 JSON，不要任何别的文字。
字段要求：
- why: 1-2 句，说清"这一分是怎么来的"——扣在哪、留了什么。必须引用候选人原话里的具体内容，不要空泛。
- gaps: 2-4 条，每条一个缺口，可执行（"补测 recall@5 的基线数字"），不要写"要加强理解"这类空话。
- ideal: 一段 80-200 字的示范回答，**结构照抄候选人自己的项目事实**（项目名、数字用他给过的），只补他没有的部分；
  不许编造新的技术细节，不许出现"作为AI"之类的说明。
- tips: 1-3 条，下次回答可以直接套用的句式。
用中文。语气平静、具体，不夸也不骂。
"""
# 自我介绍阶段的考点：和项目深挖完全不同。
# 深挖问的是"为什么这么做、代价是什么"，自我介绍问的是"能不能 90 秒说清你是谁"。
# 用深挖的尺子量自我介绍，会把一段合格的自我介绍判成 fail——
# 这不是阈值问题，是**考点用错了**（这个错误是拿真实截图看出来的，不是想出来的）。
SELF_INTRO_POINTS = [
    "身份与方向：学校/公司、年级/岗位、求职方向（一句话说清）",
    "代表项目：1-2 个，各带一句「做了什么 + 结果数字」",
    "个人角色：在这两个项目里我具体负责哪一段（不是「我们」）",
    "技术主线：这些项目连起来指向同一个方向（说明不是随便凑的）",
    "求职意向：为什么投这个岗位（和上面主线对得上）",
]
# 总时长 → 各阶段题量的权重。
# 借 interview-guide 的做法：用户拖"最多轮次"滑块，各阶段按比例分到题量，
# 而不是"深挖永远 8 轮、其他不变"——那样把总时长调短，深挖会把自我介绍挤没。
# 权重直接取 PHASE_CONFIG 的默认上限——这样"总轮次=24（默认）"时分配结果
# 和旧行为逐字相同，不会因为引入分配而改变既有场景的表现。
# 第一版我手写了 1/2/6/5/1，24 轮分出来是 2/3/10/8/2，直接让一条评估场景
# 从 PASS 掉到 FAIL（死磕检测被触发）——这就是"外来的机制要先证明不改变现状"。
PHASE_WEIGHTS = {ph: (0 if ph == "FEEDBACK" else int(cfg.get("max_turns", 1)))
                 for ph, cfg in PHASE_CONFIG.items()}


def allocate_phase_budget(max_turns: int, weights: dict | None = None) -> dict:
    """把总轮次按权重分给各阶段，返回 {phase: {"max_turns", "min_turns", "desc"}}。

    借 interview-guide 的"总时长滑块 → 各阶段题量联动"：把总轮次当预算分配，
    而不是每个阶段写死自己的上限——否则把总时长调短，深挖会把自我介绍挤没。

    规则（第一版直接四舍五入踩过坑：6 轮分出来 8 轮、合计超预算）：
      · 先给每个非 FEEDBACK 阶段保底 1 轮（不然阶段等于被跳过）
      · 剩下的按权重用**最大余数法**分，保证合计恰好等于 max(阶段数, max_turns)
      · 权重为 0 的阶段（FEEDBACK）不参与分配，保留原 desc
    """
    w = dict(weights or PHASE_WEIGHTS)
    # 默认总轮次直接返回 PHASE_CONFIG：四舍五入的余数分配在默认轮次时会多给深挖 1 轮，
    # 而那 1 轮足以让评估场景的"死磕"检测翻脸。外来机制必须先证明不改变现状。
    # 注意总轮次要把 FEEDBACK 也算进去（状态机数的是所有轮），否则永远对不上。
    default_total = sum(int(c.get("max_turns", 1)) for c in PHASE_CONFIG.values())
    if not weights and int(max_turns) == default_total:
        return {ph: dict(cfg) for ph, cfg in PHASE_CONFIG.items()}
    active = [p for p in PHASE_CONFIG if w.get(p, 1) > 0]
    total = max(len(active), int(max_turns))
    budget: dict[str, dict] = {}
    for ph in active:
        budget[ph] = {"max_turns": 1, "min_turns": 1,
                      "desc": PHASE_CONFIG.get(ph, {}).get("desc", "")}
    # 按"超出保底 1 轮的部分"分配，权重用 (w - 1)：
    # 这样 max_turns 等于默认总轮次时，结果与 PHASE_CONFIG 逐字一致（不改变既有行为）
    extra_w = {p: max(0, w.get(p, 1) - 1) for p in active}
    rest = total - len(active)
    if rest > 0:
        weight_sum = sum(extra_w.values())
        if weight_sum <= 0:
            extra_w = {p: 1 for p in active}
            weight_sum = len(active)
        exact = {p: rest * extra_w[p] / weight_sum for p in active}
        floor = {p: int(exact[p]) for p in active}
        left = rest - sum(floor.values())
        order = sorted(active, key=lambda p: (exact[p] - floor[p], extra_w[p]), reverse=True)
        for p in order[:left]:
            floor[p] += 1
        for p in active:
            budget[p]["max_turns"] += floor[p]
    budget["FEEDBACK"] = dict(PHASE_CONFIG.get("FEEDBACK", {"max_turns": 1, "min_turns": 1, "desc": ""}))
    return budget
# 阶段常量：给业务代码用，避免各处再写字符串字面量（SSOT 守卫会检查）
PHASE_PROBE = "PROJECT_PROBE"
PHASE_FEEDBACK = "FEEDBACK"
PHASE_SELF_INTRO = "SELF_INTRO"
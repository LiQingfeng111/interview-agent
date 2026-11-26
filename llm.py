"""LLM 客户端：真实 OpenAI 兼容接口 + 确定性 mock 后端。

为什么自己写而不直接上 SDK？
1. 零依赖，任何机器能跑（学生环境最常见的问题就是装不上包）。
2. 把"重试 / JSON 解析 / 失败降级"这三件事显式写出来，面试时能讲清楚工程细节。
3. mock 后端让状态机、评估、CLI 在没有 API Key 时也能完整跑通——这是可测试性的前提。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from config import LLMConfig


class LLMError(RuntimeError):
    pass


# ---------------------------------------------------------------- 调用日志
class LLMLogger:
    """每次模型调用都落一行 JSONL：时间、任务、模型、耗时、耗时错误、回复。

    为什么必须做：用户跑起来"没反应、没日志"，根本没法判断是模型没回、
    还是回了但被吞了。日志是排障的唯一入口，也是面试时讲"可观测性"的实证。
    """

    def __init__(self, echo: bool = False, log_dir: str = "logs"):
        import os

        self.echo = echo
        self.dir = log_dir
        try:
            os.makedirs(log_dir, exist_ok=True)
            import time as _t

            self.path = os.path.join(
                log_dir, "llm_" + _t.strftime("%Y%m%d_%H%M%S") + ".jsonl"
            )
        except OSError:
            self.path = ""

    def write(self, record: dict, response: str = "") -> None:
        record["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps({**record, "response": (response or "")[:4000]},
                                       ensure_ascii=False) + "\n")
            except OSError:
                pass
        if self.echo:
            head = f"[LLM] {record.get('task','')} {record.get('model','')} " \
                   f"{record.get('ms',0)}ms ok={record.get('ok')}"
            print(head)
            if record.get("error"):
                print(f"      error: {record['error']}")
            if response:
                print("      " + response[:300].replace("\n", " "))


def discover_models(base_url: str, timeout: float = 2.0, api_key: str = "") -> list[str]:
    """探测某个 OpenAI 兼容端点上有哪些模型（本地服务用）。

    api_key: 中继网关需要鉴权，不带 Key 会 401，从而被误判成"没有服务"。
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"User-Agent": "dsh-interview-agent"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        items = data.get("data") or []
        return [str(m.get("id", "")) for m in items if m.get("id")]
    except Exception:
        return []


def relay_api_key() -> str:
    """从中继网关插件自己的 settings.json 里读 Key。

    为什么不写死在代码/README：Key 属于本机凭据，写进仓库就会随代码扩散。
    这里只在本机读插件维护的配置，读不到就返回空（网关会拒绝，提示明确）。
    """
    from config import RELAY_SETTINGS

    try:
        with open(RELAY_SETTINGS, encoding="utf-8") as f:
            data = json.load(f)
        return str((data.get("chanGateway") or {}).get("relay", {}).get("key") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def _is_relay(url: str) -> bool:
    return ":18326" in (url or "")


def find_local_llm(timeout: float = 2.0) -> tuple[str, str, str] | None:
    """扫描本地候选端点，返回 (名称, base_url, 模型名)。

    找不到就返回 None。这一步是让"我本地有小模型"能自动被吃上，
    而不是让人手工填 base_url 和 model——手填最容易写错。
    """
    from config import LOCAL_CANDIDATES

    for name, base_url in LOCAL_CANDIDATES:
        key = relay_api_key() if _is_relay(base_url) else ""
        models = discover_models(base_url, timeout=timeout, api_key=key)
        if models:
            preferred = _pick_model(models, relay=_is_relay(base_url))
            return name, base_url, preferred
    return None


def _pick_model(models: list[str], relay: bool = False) -> str:
    """从模型列表里挑一个适合做中文对话的。"""
    if relay:
        from config import RELAY_MODEL_PREFER

        for want in RELAY_MODEL_PREFER:
            if want in models:
                return want
        # 退而求其次：优先免费模型，避开视觉/长上下文的重型号
        free = [m for m in models if "free" in m.lower() and "vl" not in m.lower()]
        if free:
            return free[0]

    priority = ("qwen", "deepseek", "glm", "llama", "mistral", "gemma", "phi")
    for p in priority:
        for m in models:
            if p in m.lower():
                return m
    return models[0]


class BaseLLM:
    """统一接口。task 与 hint 只服务于 mock 后端，真实后端忽略它们。"""

    is_mock = False

    def chat(
        self,
        messages: list[dict],
        *,
        task: str = "",
        hint: dict | None = None,
        json_mode: bool = False,
    ) -> str:
        raise NotImplementedError

    def chat_json(
        self,
        messages: list[dict],
        *,
        task: str = "",
        hint: dict | None = None,
        retries: int = 2,
    ) -> dict:
        """强制结构化输出：失败了重试并加强指令，最终仍失败则抛错。

        这条链路是"结构化输出工程"的最小实现，可直接写进简历。
        """
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            msgs = list(messages)
            if attempt > 0:
                msgs = msgs + [
                    {
                        "role": "user",
                        "content": "上一次输出不是合法 JSON。请只输出 JSON 对象，不要任何解释或代码块标记。",
                    }
                ]
            raw = self.chat(msgs, task=task, hint=hint, json_mode=True)
            data = _extract_json(raw)
            if data is not None:
                return data
            last_err = LLMError(f"JSON 解析失败（第 {attempt + 1} 次）：{raw[:200]}")
        raise LLMError(str(last_err))


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if not text:
        return None
    # 剥掉 markdown 代码块
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # 兜底：抓第一个平衡的花括号块
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


class OpenAICompatibleLLM(BaseLLM):
    def __init__(self, cfg: LLMConfig | None = None, logger: LLMLogger | None = None):
        self.cfg = cfg or LLMConfig()
        self.logger = logger or LLMLogger(echo=self.cfg.echo)
        if self.cfg.requires_key and not self.cfg.api_key:
            raise LLMError("远程模型需要 LLM_API_KEY；本地模型请用 --llm local。")

    def chat(self, messages, *, task="", hint=None, json_mode=False) -> str:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.cfg.api_key:
            headers["Authorization"] = f"Bearer {self.cfg.api_key}"

        last: Exception | None = None
        t0 = time.time()
        retries = max(1, int(getattr(self.cfg, "retries", 3)))
        for attempt in range(retries):
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                content = data["choices"][0]["message"]["content"]
                if content is None:
                    content = ""
                if not content.strip():
                    # 推理模型在 json_mode + 小预算时可能只回 reasoning、正文为空。
                    # 这不是"请求失败"，而是"这次没拿到内容"，交给上层重试；
                    # 记一笔日志方便复盘，但绝不能让日志代码把主流程打断。
                    self.logger.write(
                        {"task": task, "model": self.cfg.model, "base_url": self.cfg.base_url,
                         "attempt": attempt + 1, "ok": False, "empty": True,
                         "ms": int((time.time() - t0) * 1000),
                         "usage": data.get("usage") or {},
                         "error": "模型返回空内容（可能预算被 reasoning 吃满）"},
                    )
                    last = LLMError("模型返回空内容")
                    continue
                self.logger.write(
                    {"task": task, "model": self.cfg.model, "base_url": self.cfg.base_url,
                     "attempt": attempt + 1, "ok": True,
                     "ms": int((time.time() - t0) * 1000),
                     "usage": data.get("usage") or {}},
                    content,
                )
                return content
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "ignore")[:300]
                last = LLMError(f"HTTP {e.code}: {detail}")
                self.logger.write(
                    {"task": task, "model": self.cfg.model, "base_url": self.cfg.base_url,
                     "attempt": attempt + 1, "ok": False,
                     "ms": int((time.time() - t0) * 1000), "error": str(last)},
                )
                if e.code in (429, 500, 502, 503):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last from e
            except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
                last = LLMError(f"请求失败：{e}")
                self.logger.write(
                    {"task": task, "model": self.cfg.model, "base_url": self.cfg.base_url,
                     "attempt": attempt + 1, "ok": False,
                     "ms": int((time.time() - t0) * 1000), "error": str(last)},
                )
                time.sleep(1.0 * (attempt + 1))
        raise last or LLMError("未知错误")


# ---------------------------------------------------------------- mock 后端
MOCK_QUESTIONS = {
    "OPENING": "我们开始吧。请用两分钟介绍一下你自己，重点讲你最想被深挖的那个项目。",
    "SELF_INTRO": "你提到这个项目用了向量库做检索，先说说为什么要用检索，而不是把资料直接塞进上下文？",
    "CANDIDATE_ASK": "时间差不多了，你有什么想问我的？",
}

# 每个层级准备两套不同角度的问法：第 0 次用 [0]，重追问（attempt>=1）用 [1]。
# 踩坑记录：第一版每层只有一句模板，partial 时状态机原地重追，mock 就逐字
# 重复上一问，看起来像"同一个问题问两遍"。换成按 attempt 选变体后消失。
MOCK_LADDER_Q = {
    0: [
        "你简历里写这个项目是「{p}」，先讲清楚：它解决的具体问题是什么，规模有多大？",
        "换个角度：这个项目不负责什么？团队几个人，你具体承担哪一块？",
    ],
    1: [
        "为什么选 {s} 这条技术路线？当时评估过哪些替代方案？",
        "如果不用 {s}，换一个更简单的方案，会在哪一步先出问题？",
    ],
    2: [
        "{s} 这块具体怎么落地的？数据从哪来、怎么切分、失败长什么样？",
        "举个具体例子：一次请求从进来到返回，中间经过哪几步？哪一步最容易出错？",
    ],
    3: [
        "如果预算砍一半、或者数据量翻十倍，你的设计会先崩在哪？",
        "这个方案带来的复杂度，你觉得值吗？有没有更便宜的近似做法？",
    ],
    4: [
        "这个方案在什么情况下会失效？你怎么发现它已经失效了？",
        "线上真出问题时，你靠哪两个具体指标第一时间定位？",
    ],
    5: [
        "如果让你重做一遍，你会保留什么、砍掉什么？",
        "假设现在从零再做一个类似的，你第一周会先做什么、后做什么？",
    ],
}

MOCK_TECH_Q = {
    "chunk": "RAG 里 chunk 大小你怎么定的？依据是什么？",
    "召回": "召回率你实际测过吗？用什么指标、什么数据集？",
    "幂等": "如果这个工具调用被重复触发两次，你的系统会发生什么？",
    "限流": "线上突然流量翻五倍，你的服务会先挂在哪一层？",
    "评测": "你怎么证明你的 Agent 变好了，而不是只是感觉变好了？",
    "default": "围绕 {t}：你实际踩过什么坑，怎么定位的？",
}

# 人设措辞（mock 专用）。balanced 不加前缀，保证默认基线不变。
MOCK_PERSONA_LEAD = {
    "balanced": "",
    "strict": "（打断）先别急着说结论——",
    "warm": "这块你答得挺清楚的，我们再往前一步：",
    "academic": "从原理上说，",
    "engineering": "换个工程角度：",
    "efficient": "",
}


def _mock_grade(messages: list[dict], hint: dict | None = None) -> dict:
    """按回答内容打分的 mock 评分官。

    为什么不用固定分数池？
    第一版用 `POOL[n % 3]` 轮转给分，结果每一问都被"fail→partial→pass"
    各打一遍，同一个问题被重复问三次，既不像面试，也让评估集失去区分度
    （敷衍和优秀拿到的分数分布完全一样）。
    改成内容驱动后，mock 也能区分「敷衍」和「讲透了」，回归才有意义。
    """
    answer = ""
    for m in messages:
        if m.get("role") == "user" and "【候选人回答】" in m.get("content", ""):
            answer = m["content"].split("【候选人回答】", 1)[1]
            break
    # 必须截掉提示词尾部，否则 "\n\n只输出 JSON。" 会被算进答案长度里，
    # 让"反正能跑"这种 5 字敷衍回答被量成 20 多字，分数虚高。
    for tail in ("只输出 JSON", "\n\n【", "\n\n只输出"):
        if tail in answer:
            answer = answer.split(tail, 1)[0]
    answer = answer.strip()
    if not answer or "（候选人沉默）" in answer:
        # evidence 必须能在答案原文里找到（审计层 INV-04 逐条校验），
        # 所以这里不再写死"未作答"——那是编造证据。
        return {"accuracy": 1, "depth": 1, "tradeoff": 1, "boundary": 1, "clarity": 1,
                "verdict": "fail", "weakest": "depth", "evidence": "",
                "followup_hint": "先让候选人开口"}

    n = len(answer)
    has_num = any(c.isdigit() for c in answer)
    trade_words = ("但是", "不过", "代价", "权衡", "取舍", "替代", "对比", "因为", "所以", "而不是")
    boundary_words = ("失效", "失败", "崩", "告警", "监控", "边界", "限制", "降级")
    impl_words = ("实现", "具体", "切分", "参数", "校验", "重试", "接口", "日志", "部署", "代码")

    has_trade = any(w in answer for w in trade_words)
    has_boundary = any(w in answer for w in boundary_words)
    has_impl = any(w in answer for w in impl_words)

    depth = 1
    if n > 30:
        depth = 2
    if has_impl or n > 80:
        depth = 3
    if has_impl and n > 80 and has_num:
        depth = 4

    accuracy = 2 if n < 15 else (3 if n < 40 else 4)
    tradeoff = 3 if has_trade else 2
    boundary = 3 if has_boundary else (1 if n < 20 else 2)
    clarity = 2 if n < 15 else (4 if n < 500 else 3)

    dims = {"accuracy": accuracy, "depth": depth, "tradeoff": tradeoff,
            "boundary": boundary, "clarity": clarity}
    avg = sum(dims.values()) / 5
    verdict = "pass" if avg >= 3.5 else ("partial" if avg >= 2.5 else "fail")
    weakest = min(dims, key=lambda k: dims[k])
    return {**dims, "verdict": verdict, "weakest": weakest,
            "evidence": answer[:30], "followup_hint": f"针对 {weakest} 再问一层",
            "covered": _mock_covered(answer),
            "target_element": (hint or {}).get("target_element", ""),
            "target_status": _mock_target_status(answer, (hint or {}).get("target_element", ""))}


def _mock_target_status(answer: str, element: str) -> str:
    """mock 版三态判定：covered / evaded / unknown。"""
    if not element:
        return ""
    from evaluator import judge_target_status

    return judge_target_status(answer, element, _mock_covered(answer))


def _mock_covered(answer: str) -> list[str]:
    """规则版要素覆盖检测（与 evaluator.detect_covered 同源，避免循环导入）。"""
    from config import ELEMENT_KEYWORDS, ELEMENT_NAMES

    text = (answer or "").lower()
    hit = [e for e, words in ELEMENT_KEYWORDS.items()
           if any(w.lower() in text for w in words)]
    return [e for e in ELEMENT_NAMES if e in hit]


class MockLLM(BaseLLM):
    """确定性后端：同一个输入永远得到同一输出，方便写回归测试。"""

    is_mock = True

    def __init__(self, seed: int = 0):
        self.n = seed
        self.calls: list[str] = []

    def chat(self, messages, *, task="", hint=None, json_mode=False) -> str:
        self.calls.append(task)
        hint = hint or {}
        if task == "resume_parse":
            return json.dumps(_mock_resume(hint), ensure_ascii=False)
        if task == "interviewer":
            return self._mock_question(hint)
        if task == "evaluator":
            item = _mock_grade(messages, hint)
            self.n += 1
            return json.dumps(item, ensure_ascii=False)
        if task == "coach":
            return (
                "【总评】项目能跑通，但所有回答都停在「我用了什么」，没有一处讲清「为什么不用别的」。\n"
                "【哪里答对了】\n"
                "1) 能说清项目的使用场景与规模（引用：「300 份课件」）。\n"
                "2) 能给出一个量化结果（引用：「召回 82%」）。\n"
                "【哪里答虚了】\n"
                "1) 技术选型只有结论，缺「被否掉的替代方案」与对比依据。\n"
                "2) 没有 baseline：82% 是相对什么测出来的，没说清。\n"
                "【面试官下一步会怎么追】\n"
                "1) 你说的召回到底怎么定义？测试集多大、谁标的？\n"
                "2) 如果不用重排，效果会掉多少？这个代价你能接受吗？\n"
                "【下一步训练动作】\n"
                "1) 给每个选型补一句「我否掉了 X，因为 Y」。\n"
                "2) 为每个指标写明 baseline 与测量方法。\n"
                "3) 每个模块准备一个失败案例与排查过程。\n"
                "【最深卡点】取舍层"
            )
        return "（mock）"

    def _mock_question(self, hint: dict) -> str:
        phase = hint.get("phase", "")
        level = int(hint.get("ladder_level", 0))
        attempt = int(hint.get("attempt", 0))
        project = hint.get("project") or "你的项目"
        stack = hint.get("stack") or "你选的技术栈"
        topic = hint.get("topic", "")

        if phase == "PROJECT_PROBE":
            variants = MOCK_LADDER_Q.get(level, MOCK_LADDER_Q[5])
            # 有面试前计划时优先用计划里的提问草稿（模拟真实 LLM 读简历出题）
            base = hint.get("seed_question") or variants[min(attempt, len(variants) - 1)].format(
                p=project, s=stack
            )
        elif phase == "TECH_QA":
            # 题库题优先：mock 也要走题库，否则 mock 下的技术环节永远测不到题库
            base = hint.get("bank_question") or ""
            if not base:
                for key, q in MOCK_TECH_Q.items():
                    if key != "default" and key in topic:
                        base = q
                        break
            if not base:
                base = MOCK_TECH_Q["default"].format(t=topic or "这个技术点")
        else:
            base = MOCK_QUESTIONS.get(phase, "请继续。")

        # 人设措辞在 mock 下也可见（真实模型由 prompt 体现）。
        # 只加在深挖/技术环节：开场白上挂"（打断）先别急着说结论"很怪，
        # 真实面试官也不会在自我介绍前就打断你。
        lead = MOCK_PERSONA_LEAD.get(hint.get("persona", "balanced"), "")
        if phase not in ("PROJECT_PROBE", "TECH_QA"):
            lead = ""
        return f"{lead}{base}" if lead else base


def _mock_resume(hint: dict) -> dict:
    return {
        "projects": [
            {
                "name": "高校课程问答助手",
                "role": "独立开发",
                "stack": ["Python", "向量库", "RAG", "FastAPI"],
                "highlights": ["支持 300 份课件检索问答", "做了 chunk 切分与重排"],
                "metrics": ["召回率 82%"],
            },
            {
                "name": "校园二手交易平台",
                "role": "后端负责人",
                "stack": ["FastAPI", "PostgreSQL", "Redis"],
                "highlights": ["实现商品搜索与订单状态机"],
                "metrics": [],
            },
        ]
    }


def _make_llm(cfg: "LLMConfig", logger: "LLMLogger") -> "BaseLLM":
    """按 IA_LLM_BACKEND 选择后端实现。

    native（默认）  → 本文件里手写的 urllib 实现，行为与历史版本逐字一致
    langchain      → llm_langchain.LangChainLLM，调用方式换成 LangChain

    为什么用环境变量而不是改函数签名：`build_llm()` 有 30 多处调用点，
    加参数会牵动全部调用方与测试；环境变量是"零侵入开关"，也方便 A/B 对账。
    """
    backend = (os.getenv("IA_LLM_BACKEND") or "native").strip().lower()
    if backend in ("langchain", "lc"):
        from llm_langchain import LangChainLLM  # 延迟导入：没装 langchain 不影响 native

        return LangChainLLM(cfg, logger=logger)
    return OpenAICompatibleLLM(cfg, logger=logger)


def build_llm(
    mock: bool = False,
    mode: str = "auto",
    base_url: str | None = None,
    model: str | None = None,
    echo: bool = False,
    api_key: str | None = None,
) -> BaseLLM:
    """构建 LLM 客户端。

    mode:
      mock   → 确定性后端（离线测试）
      local  → 本地 OpenAI 兼容服务（Ollama / LM Studio / vLLM / 本地中继网关）
      remote → 远程 API（需要 LLM_API_KEY）
      auto   → 先探本地，有就用；否则有 Key 用远程；都没有才 mock

    api_key: 本地网关也可能要鉴权（例如 DSH 的 our-free-model 中继网关），
    所以 local 模式也允许带 Key。
    """
    if mock or mode == "mock":
        return MockLLM()

    logger = LLMLogger(echo=echo)
    # 端点与模型名也支持环境变量：容器/云部署时不可能改代码或加命令行参数。
    # 容器里 127.0.0.1 指的是容器自己，所以宿主模型必须写 host.docker.internal。
    base_url = base_url or os.getenv("LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
    model = model or os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or None
    # 两个环境变量名都认：容器/CI 里习惯用 OPENAI_API_KEY，
    # 本机开发习惯用 LLM_API_KEY。只认一个会导致"明明填了 key 却说没有"（实测踩过）。
    key = api_key if api_key is not None else (
        os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "")

    def _local_cfg(url: str, mdl: str) -> LLMConfig:
        return LLMConfig(
            base_url=url,
            model=mdl,
            api_key=key,
            requires_key=False,
            echo=echo,
        )

    if mode in ("local", "auto"):
        # 显式给了 --base-url 就必须用它，不能再退回固定端口扫描
        if base_url:
            probe_key = key or (relay_api_key() if _is_relay(base_url) else "")
            models = discover_models(base_url, api_key=probe_key)
            picked = model or (_pick_model(models, relay=_is_relay(base_url)) if models else "")
            if picked:
                use_key = key or (relay_api_key() if _is_relay(base_url) else "")
                cfg = _local_cfg(base_url, picked)
                cfg.api_key = use_key
                src = "已探测" if models else "按参数指定"
                print(f"[LLM] 已连接本地模型（{src}）：{cfg.model} · {cfg.base_url}")
                if _is_relay(base_url) and not use_key:
                    print("[LLM] 警告：中继网关需要 Key，但没读到 settings.json，可能返回 401")
                return _make_llm(cfg, logger)
            if mode == "local":
                raise LLMError(
                    f"{base_url} 没有响应 /v1/models。请确认服务已启动，"
                    "或用 --model 直接指定模型名。"
                )
        found = find_local_llm()
        if found:
            name, url, detected = found
            use_key = key or (relay_api_key() if _is_relay(url) else "")
            cfg = _local_cfg(url, model or detected)
            cfg.api_key = use_key
            print(f"[LLM] 已连接本地模型：{name} · {cfg.model} · {cfg.base_url}")
            return _make_llm(cfg, logger)
        if mode == "local":
            raise LLMError(
                "没检测到本地模型服务。请先启动 Ollama / LM Studio / DSH 免费中继，"
                "或用 --base-url 指定，例如 http://127.0.0.1:18326/v1"
            )

    cfg = LLMConfig(echo=echo)
    if base_url:
        cfg.base_url = base_url
    if model:
        cfg.model = model
    if key:
        cfg.api_key = key
    if cfg.api_key or mode == "remote":
        print(f"[LLM] 使用远程接口：{cfg.model} · {cfg.base_url}")
        return _make_llm(cfg, logger)

    raise LLMError("既没有本地模型，也没有 LLM_API_KEY。")

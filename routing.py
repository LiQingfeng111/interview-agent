"""按任务路由的双后端 LLM：提问走真模型，其余走 mock。

为什么需要它：
  做 prompt A/B 时，"问题问得好不好"必须由**真模型**产生（mock 是模板，比不出差异）；
  但"候选人答得好不好"的评分如果也用真模型，每跑一次就多几分钟和一堆 token，
  而且评分方差会把 prompt 的差异淹没。

  所以把两件事拆开：
    - interviewer（要比较的对象）→ 真模型
    - evaluator / coach / planner / resume_parse（只是把流程跑完）→ mock

  这不是偷懒：A/B 结论只在"内容指标"上成立，评分维度的比较被明确排除，
  这一点写在 ab_prompt 的报告里，不当成能力结论。
"""
from __future__ import annotations

from typing import Any

from llm import BaseLLM


class RoutingLLM(BaseLLM):
    def __init__(self, routes: dict[str, BaseLLM], default: BaseLLM | None = None):
        self.routes = {k: v for k, v in routes.items() if v is not None}
        self.default = default or next(iter(self.routes.values()), None)
        self.calls: dict[str, int] = {}
        # 只要有一条路由是真模型，就不能算 mock 模式（否则画像会被当成无意义）
        self.is_mock = all(getattr(v, "is_mock", False) for v in self.routes.values())

    def chat(self, messages, *, task="", hint=None, json_mode=False) -> str:
        self.calls[task] = self.calls.get(task, 0) + 1
        backend = self.routes.get(task) or self.default
        if backend is None:
            raise RuntimeError(f"没有可用的后端来处理任务 {task}")
        return backend.chat(messages, task=task, hint=hint, json_mode=json_mode)

    def chat_json(self, messages, *, task="", hint=None) -> dict[str, Any]:
        backend = self.routes.get(task) or self.default
        if backend is None:
            raise RuntimeError(f"没有可用的后端来处理任务 {task}")
        return backend.chat_json(messages, task=task, hint=hint)

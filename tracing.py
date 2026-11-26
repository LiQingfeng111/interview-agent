"""调用追踪：把每次模型调用同时记进账本、算进预算、录进 cassette。

为什么做成包装器而不是散在 session 里：
  调用点有三个角色（面试官/评分官/教练），每个都去手动记账必然漏。
  包一层之后，"调用模型"这个动作天然带审计，业务代码一行不用改。

零依赖、零侵入：实现和 OpenAICompatibleLLM 同样的 chat/chat_json 接口，
所以它能直接替换 llm 塞进 InterviewSession。
"""
from __future__ import annotations

import json
import time

from budget import Budget
from ledger import E, Ledger
from replay import Cassette, fingerprint


class TracedLLM:
    def __init__(self, inner, ledger: Ledger | None = None,
                 budget: Budget | None = None, cassette: Cassette | None = None):
        self.inner = inner
        self.ledger = ledger
        self.budget = budget
        self.cassette = cassette
        self.calls = 0
        self.cfg = getattr(inner, "cfg", None)
        self.is_mock = getattr(inner, "is_mock", False)
        self.model = getattr(self.cfg, "model", "") if self.cfg else ("mock" if self.is_mock else "")

    # ---------------------------------------------------------- 核心
    def chat(self, messages, **kw) -> str:
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        task = str(kw.get("task", ""))
        key = fingerprint(prompt, task)
        t0 = time.time()
        self.calls += 1

        # 重放优先：cassette 命中就不真调模型（省钱、且结果可复现）
        if self.cassette is not None:
            cached = self.cassette.get(key)
            if cached is not None:
                return cached

        out, ok = "", True
        try:
            out = self.inner.chat(messages, **kw)
        except Exception:
            ok = False
            raise
        finally:
            ms = int((time.time() - t0) * 1000)
            spent = {"tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0}
            if self.budget is not None:
                spent = self.budget.spend(prompt=prompt, completion=out, model=self.model)
                if self.budget.level() >= 1:
                    self.budget.note(f"level={self.budget.level()}")
            if self.cassette is not None and ok:
                self.cassette.record(key, out)
            if self.ledger is not None:
                self.ledger.append(
                    E.LLM_CALL,
                    {"task": task, "chars": len(out or "")},
                    request_id=key, op=task, model=self.model,
                    tokens_in=spent["tokens_in"], tokens_out=spent["tokens_out"],
                    cost_cny=spent["cost_cny"], latency_ms=ms, ok=ok,
                )
        return out

    def chat_json(self, messages, **kw) -> dict:
        """JSON 调用同样要记账。

        这里曾经是**审计黑洞**：`chat()` 记了账本/预算/cassette，`chat_json()` 没有。
        而评分官（evaluator）和教练走的都是 chat_json —— 也就是说
        "这一场调了几次模型、花了多少钱、证据是谁给的"全部漏了评分那部分，
        账本却看起来完全正常（因为面试官那次有记录）。
        这就是"看起来可审计"和"真的可审计"的区别。
        """
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        task = str(kw.get("task", ""))
        key = fingerprint(prompt, task)
        self.calls += 1

        if self.cassette is not None:
            cached = self.cassette.get(key)
            if cached is not None:
                try:
                    return json.loads(cached)
                except Exception:
                    pass

        t0 = time.time()
        data, ok = {}, True
        try:
            data = self.inner.chat_json(messages, **kw)
        except Exception:
            ok = False
            raise
        finally:
            ms = int((time.time() - t0) * 1000)
            out = ""
            try:
                out = json.dumps(data, ensure_ascii=False)
            except Exception:
                out = str(data)
            spent = {"tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0}
            if self.budget is not None:
                spent = self.budget.spend(prompt=prompt, completion=out, model=self.model)
                if self.budget.level() >= 1:
                    self.budget.note(f"level={self.budget.level()}")
            if self.cassette is not None and ok:
                try:
                    self.cassette.record(key, out)
                except Exception:
                    pass
            if self.ledger is not None:
                self.ledger.append(
                    E.LLM_CALL,
                    {"task": task, "chars": len(out), "json": True},
                    request_id=key, op=task, model=self.model,
                    tokens_in=spent["tokens_in"], tokens_out=spent["tokens_out"],
                    cost_cny=spent["cost_cny"], latency_ms=ms, ok=ok,
                )
        return data

    # ---------------------------------------------------------- 透传
    def __getattr__(self, name):
        return getattr(self.inner, name)

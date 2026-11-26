"""LangChain 后端：把 LLM 调用从"手写 HTTP"换成 LangChain。

为什么要单独一个类，而不是把 llm.py 里那 70 行 HTTP 代码直接删掉重写：

1. **可回退**。`IA_LLM_BACKEND=native` 仍走原来的 urllib 实现，
   线上出问题能一行切回去（这也是"抽象端口"的实际价值）。
2. **对账**。两个后端跑同一批提示词，返回的结构必须一致，
   否则 322 个测试会告诉你哪一处行为变了。
3. **审计不变**。`LLMLogger` 照旧记录 model / usage / 耗时，
   LangChain 只是换了"怎么发请求"，不换"怎么记账"。

关键取舍：
- `max_retries=0`：重试策略留在本文件里手写，语义与 native 版逐字一致
  （429/5xx 退避、空内容重试）。用 LangChain 自带重试会让两条路径的
  重试次数与退避不同，测试里很难对齐。
- `json_mode` 走 OpenAI 原生 `response_format={"type": "json_object"}`，
  而不是 `with_structured_output()`：后者会插入自己的 schema 提示词，
  改变模型看到的输入，违反"提示词不变"的约束。
"""

from __future__ import annotations

import time
from typing import Any

from llm import BaseLLM, LLMConfig, LLMError, LLMLogger


class LangChainLLM(BaseLLM):
    """用 langchain_openai.ChatOpenAI 实现的 LLM 后端。"""

    is_mock = False

    def __init__(
        self,
        cfg: LLMConfig | None = None,
        logger: LLMLogger | None = None,
        client=None,
    ):
        self.cfg = cfg or LLMConfig()
        self.logger = logger or LLMLogger(echo=self.cfg.echo)
        if self.cfg.requires_key and not self.cfg.api_key:
            raise LLMError("远程模型需要 LLM_API_KEY；本地模型请用 --llm local。")
        if client is not None:
            # 测试注入点：不联网也能验证消息转换、用量映射与重试语义
            self._client = client
            return
        try:
            from langchain_openai import ChatOpenAI  # 延迟导入：没装也不影响 native 后端
        except ImportError as e:  # pragma: no cover - 环境缺失时的明确提示
            raise LLMError(
                " 没装 langchain-openai。请先执行：pip install langchain-openai"
            ) from e

        self._client = ChatOpenAI(
            model=self.cfg.model,
            base_url=self.cfg.base_url,
            api_key=self.cfg.api_key or "not-needed",
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
            timeout=self.cfg.timeout,
            max_retries=0,          # 重试由本文件接管，保持两后端语义一致
        )

    # ------------------------------------------------------------ 消息转换
    @staticmethod
    def _to_lc_messages(messages: list[dict]):
        """把 OpenAI 风格的 dict 消息转成 LangChain 消息对象。

        只做类型映射，不改内容——`role` 与 `content` 原样搬过去，
        这样两条后端向模型提交的文本完全相同。
        """
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        mapping = {"system": SystemMessage, "user": HumanMessage, "assistant": AIMessage}
        out = []
        for m in messages:
            role = (m.get("role") or "user").lower()
            cls = mapping.get(role, HumanMessage)
            out.append(cls(content=m.get("content") or ""))
        return out

    # ------------------------------------------------------------ 主入口
    def chat(self, messages, *, task="", hint=None, json_mode=False) -> str:
        lc_messages = self._to_lc_messages(messages)
        kwargs: dict[str, Any] = {}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        model = self._client.bind(**kwargs) if kwargs else self._client

        last: Exception | None = None
        retries = max(1, int(getattr(self.cfg, "retries", 3)))
        t0 = time.time()
        for attempt in range(retries):
            try:
                resp = model.invoke(lc_messages)
                content = resp.content if hasattr(resp, "content") else str(resp)
                if isinstance(content, list):
                    # LangChain 允许 content 是分块列表，拼回纯文本
                    content = "".join(
                        c.get("text", "") if isinstance(c, dict) else str(c) for c in content
                    )
                if not content.strip():
                    self.logger.write(
                        {"task": task, "model": self.cfg.model, "base_url": self.cfg.base_url,
                         "attempt": attempt + 1, "ok": False, "empty": True,
                         "ms": int((time.time() - t0) * 1000),
                         "usage": self._usage(resp),
                         "error": "模型返回空内容（可能预算被 reasoning 吃满）"},
                    )
                    last = LLMError("模型返回空内容")
                    continue
                self.logger.write(
                    {"task": task, "model": self.cfg.model, "base_url": self.cfg.base_url,
                     "attempt": attempt + 1, "ok": True,
                     "ms": int((time.time() - t0) * 1000),
                     "usage": self._usage(resp)},
                    content,
                )
                return content
            except Exception as e:  # noqa: BLE001 - 统一翻译成 LLMError，保持与 native 一致
                last = LLMError(f"请求失败：{type(e).__name__} {e}")
                self.logger.write(
                    {"task": task, "model": self.cfg.model, "base_url": self.cfg.base_url,
                     "attempt": attempt + 1, "ok": False,
                     "ms": int((time.time() - t0) * 1000), "error": str(last)},
                )
                if self._retryable(e) and attempt < retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise last from e
        raise last or LLMError("未知错误")

    # ------------------------------------------------------------ 工具方法
    @staticmethod
    def _usage(resp) -> dict:
        """把 LangChain 的 usage_metadata 换成和 native 后端一样的字段名。

        账本里读的是 prompt_tokens / completion_tokens，命名必须对齐，
        否则成本报表在两后端之间会一个有一个没有。
        """
        meta = getattr(resp, "usage_metadata", None) or {}
        if not meta:
            return {}
        return {
            "prompt_tokens": meta.get("input_tokens", 0),
            "completion_tokens": meta.get("output_tokens", 0),
            "total_tokens": meta.get("total_tokens", 0),
        }

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        text = f"{type(exc).__name__} {exc}".lower()
        return any(k in text for k in ("429", "500", "502", "503", "timeout", "rate limit"))


def build_langchain_llm(cfg: LLMConfig | None = None, logger: LLMLogger | None = None) -> LangChainLLM:
    return LangChainLLM(cfg=cfg, logger=logger)

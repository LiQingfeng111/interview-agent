"""LangChain 后端的对账测试。

思路：不比较"模型回了什么"（那样每次都不一样），只比较**我们交给模型的东西**
和**我们记下的账**。这两件事是确定的，能逐字对齐。

覆盖四件事：
1. 消息转换：OpenAI 风格 dict → LangChain 消息对象，文本一字不差
2. json_mode：必须带 response_format=json_object，否则模型不会保证输出 JSON
3. usage 字段名：必须翻译成 prompt_tokens/completion_tokens，否则成本报表读不到
4. 失败语义：空内容要抛 LLMError（让上层 chat_json 重试），而不是返回空串
"""

from __future__ import annotations

import os

import pytest

from llm import LLMConfig, LLMError, LLMLogger


@pytest.fixture()
def cfg():
    return LLMConfig(
        base_url="http://127.0.0.1:9/v1",
        model="test-model",
        api_key="test-key",
        temperature=0.3,
        max_tokens=256,
        timeout=5,
        retries=1,
        requires_key=False,
    )


def _adapter(cfg, tmp_path):
    from llm_langchain import LangChainLLM

    return LangChainLLM(cfg=cfg, logger=LLMLogger(echo=False, log_dir=str(tmp_path)))


def _adapter_with(cfg, tmp_path, client):
    from llm_langchain import LangChainLLM

    return LangChainLLM(cfg=cfg, logger=LLMLogger(echo=False, log_dir=str(tmp_path)), client=client)


class FakeResponse:
    def __init__(self, content, usage=None):
        self.content = content
        self.usage_metadata = usage or {}


def test_converts_messages_without_changing_text(cfg, tmp_path):
    lc = _adapter(cfg, tmp_path)
    msgs = [
        {"role": "system", "content": "你是面试官"},
        {"role": "user", "content": "请出第一题"},
        {"role": "assistant", "content": "好的"},
    ]
    out = lc._to_lc_messages(msgs)
    assert [m.content for m in out] == ["你是面试官", "请出第一题", "好的"]
    assert [type(m).__name__ for m in out] == ["SystemMessage", "HumanMessage", "AIMessage"]


def test_json_mode_sets_response_format(cfg, tmp_path):
    captured = {}

    class FakeClient:
        def bind(self, **kwargs):
            captured.update(kwargs)
            return self

        def invoke(self, messages):
            return FakeResponse('{"ok": true}', {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18})

    lc = _adapter_with(cfg, tmp_path, FakeClient())
    raw = lc.chat([{"role": "user", "content": "打分"}], task="eval", json_mode=True)
    assert raw == '{"ok": true}'
    assert captured.get("response_format") == {"type": "json_object"}


def test_usage_field_names_match_native_backend(cfg, tmp_path):
    lc = _adapter(cfg, tmp_path)
    resp = FakeResponse("内容", {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140})
    usage = lc._usage(resp)
    # 账本读的是 prompt_tokens / completion_tokens，名字必须一致
    assert usage == {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140}
    assert lc._usage(FakeResponse("x")) == {}


def test_empty_content_raises_for_upper_layer_retry(cfg, tmp_path):
    class FakeClient:
        def bind(self, **kwargs):
            return self

        def invoke(self, messages):
            return FakeResponse("   ")

    lc = _adapter_with(cfg, tmp_path, FakeClient())
    with pytest.raises(LLMError):
        lc.chat([{"role": "user", "content": "题目"}], task="question")


def test_retry_classification(cfg, tmp_path):
    lc = _adapter(cfg, tmp_path)
    assert lc._retryable(RuntimeError("Error code: 429 rate limit"))
    assert lc._retryable(TimeoutError("timeout"))
    assert not lc._retryable(ValueError("bad request 400"))


def test_backend_switch_via_env(cfg, tmp_path, monkeypatch):
    """IA_LLM_BACKEND=langchain 时 build_llm 应返回 LangChain 实现。"""
    import llm

    monkeypatch.setenv("IA_LLM_BACKEND", "langchain")
    built = llm._make_llm(cfg, LLMLogger(echo=False, log_dir=str(tmp_path)))
    assert type(built).__name__ == "LangChainLLM"

    monkeypatch.setenv("IA_LLM_BACKEND", "native")
    built = llm._make_llm(cfg, LLMLogger(echo=False, log_dir=str(tmp_path)))
    assert type(built).__name__ == "OpenAICompatibleLLM"

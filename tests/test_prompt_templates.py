"""提示词模板化的一致性测试：结构换了，文本必须一字不变。

三条断言：
1. template 引擎与旧实现（legacy）对 9 条真实形态的输入渲染出**完全相同的文本**；
2. 黄金快照（重构前拍的）与当前输出逐字一致——防止"两边一起改错"；
3. LCEL 链真的把 system/user 传进了 LlmPort，且返回模型文本。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import interviewer as interviewer_mod
import prompt_templates
from interviewer import Interviewer
from llm import MockLLM

GOLDEN = Path(__file__).parent / "golden" / "prompt_build.json"

HISTORY = [
    {"q": "这个项目要解决什么问题？", "a": "解决课程资料检索慢的问题", "verdict": "pass"},
    {"q": "你的 baseline 是什么？", "a": "原来人工翻 PPT，平均 8 分钟", "verdict": "partial"},
]


def sm_of(phase: str, desc: str, project: str = "", history=None):
    return SimpleNamespace(
        phase=phase,
        phase_cfg={"desc": desc},
        probe=SimpleNamespace(current=project, ladder_level=2),
        history=HISTORY if history is None else history,
        ladder_name="怎么做",
    )


CASES = {
    "opening": (sm_of("OPENING", "开场", "", []), {"phase": "OPENING", "persona": "balanced"}, [], ""),
    "self_intro": (sm_of("SELF_INTRO", "自我介绍", "", []), {"phase": "SELF_INTRO", "persona": "balanced"}, [], ""),
    "probe_first": (
        sm_of("PROJECT_PROBE", "项目深挖", "课程问答助手"),
        {"phase": "PROJECT_PROBE", "persona": "balanced", "project": "课程问答助手",
         "ladder_level": 2, "ladder_name": "怎么做", "attempt": 0},
        [], "",
    ),
    "probe_retry": (
        sm_of("PROJECT_PROBE", "项目深挖", "课程问答助手"),
        {"phase": "PROJECT_PROBE", "persona": "strict", "project": "课程问答助手",
         "ladder_level": 2, "ladder_name": "怎么做", "attempt": 1,
         "verdict": "partial", "weakest": "tradeoff",
         "followup_hint": "追问 500 字这个切块是怎么定的"},
        [], "",
    ),
    "probe_seed": (
        sm_of("PROJECT_PROBE", "项目深挖", "课程问答助手"),
        {"phase": "PROJECT_PROBE", "persona": "balanced", "project": "课程问答助手",
         "ladder_level": 3, "ladder_name": "取舍", "attempt": 0,
         "seed_question": "为什么选 500 字而不是 300 字？"},
        [], "",
    ),
    "tech_bank": (
        sm_of("TECH_QA", "技术问答"),
        {"phase": "TECH_QA", "persona": "balanced", "topic": "Transformer 注意力机制",
         "bank_question": "多头注意力中 d_model=512、h=8 时每个头维度多少？",
         "bank_points": ["d_k = d_v = 64", "各头学不同子空间", "concat 后 W_O 融合"]},
        [], "",
    ),
    "tech_jd": (
        sm_of("TECH_QA", "技术问答"),
        {"phase": "TECH_QA", "persona": "engineering", "topic": "缓存一致性"},
        ["Redis", "分布式锁", "多级缓存"], "",
    ),
    "tech_web": (
        sm_of("TECH_QA", "技术问答"),
        {"phase": "TECH_QA", "persona": "balanced", "topic": "向量检索",
         "web_refs": "HNSW 在 1000 万规模下的 recall@10 通常在 0.95 以上"},
        [], "",
    ),
    "candidate_ask": (sm_of("CANDIDATE_ASK", "候选人反问"), {"phase": "CANDIDATE_ASK", "persona": "balanced"}, [], ""),
}


def _render(name: str, engine: str) -> str:
    sm, hint, jd, web = CASES[name]
    iv = Interviewer(llm=MockLLM(seed=1))
    iv.jd_keywords = jd
    iv.web_refs = web
    if engine == "legacy":
        return iv._build_prompt_legacy(sm, hint)
    return prompt_templates.build_question_text(sm, hint, jd_keywords=jd, web_refs=web)


@pytest.mark.parametrize("name", list(CASES))
def test_template_matches_legacy_exactly(name):
    """模板实现与旧实现逐字一致——这是重构唯一可接受的验收标准。"""
    assert _render(name, "template") == _render(name, "legacy")


@pytest.mark.parametrize("name", list(CASES))
def test_matches_golden_snapshot(name):
    """与重构前的快照比对，防止两条实现一起改错。"""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert name in golden, f"快照里缺少用例 {name}"
    assert _render(name, "template") == golden[name]["text"]


def test_engine_switch(monkeypatch):
    sm, hint, _, _ = CASES["probe_first"]
    iv = Interviewer(llm=MockLLM(seed=1))
    monkeypatch.setenv("IA_PROMPT_ENGINE", "legacy")
    assert interviewer_mod._prompt_engine() == "legacy"
    legacy = iv._build_prompt(sm, hint)
    monkeypatch.setenv("IA_PROMPT_ENGINE", "template")
    assert interviewer_mod._prompt_engine() == "template"
    assert iv._build_prompt(sm, hint) == legacy


def test_no_unfilled_placeholder_left():
    """渲染后不能残留 {xxx} —— 说明有槽位漏传。"""
    for name in CASES:
        text = _render(name, "template")
        assert "{" not in text and "}" not in text, f"{name} 残留未填充槽位: {text[:80]}"


def test_slots_are_explicit():
    """可变部分必须是具名槽位：改一句话术不用翻 69 行分支。"""
    sm, hint, jd, web = CASES["probe_retry"]
    slots = prompt_templates.probe_slots(sm, hint, jd_keywords=jd, web_refs=web)
    for key in ("ladder", "project", "followup", "retry", "checklist", "seed", "recent", "asked"):
        assert key in slots, f"缺少槽位 {key}"
    assert slots["followup"].startswith("评分官建议的追问方向")
    assert "再次追问" in slots["retry"]
    assert slots["seed"] == ""            # 本用例没有 seed，空槽位必须干净


def test_lcel_chain_goes_through_llm_port():
    """LCEL 链必须经过 LlmPort（记账/重试都挂在那儿），不能绕过它直连 SDK。"""
    calls = []

    class SpyLLM(MockLLM):
        def chat(self, messages, *, task="", hint=None, json_mode=False):
            calls.append({"task": task, "messages": messages})
            return super().chat(messages, task=task, hint=hint, json_mode=json_mode)

    chain = prompt_templates.make_question_chain(SpyLLM(seed=3), task="interviewer")
    out = chain.invoke({"system": "你是面试官", "user": "请出第一题"})
    assert isinstance(out, str) and out.strip()
    assert len(calls) == 1
    assert calls[0]["task"] == "interviewer"
    assert calls[0]["messages"][0] == {"role": "system", "content": "你是面试官"}
    assert calls[0]["messages"][1] == {"role": "user", "content": "请出第一题"}

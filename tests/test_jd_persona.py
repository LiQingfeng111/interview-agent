"""JD 解析 + 面试官人设测试。

这两个功能来自对标调研：
  - JD：PolyInterview / Hazenix/interview-prep-skills 都用「简历 × JD」决定问题
  - 人设：Hazenix 提供六种面试官风格；论文 2611.17830 证明人设影响候选人压力
加测试是为了防止"加了参数但没生效"——这类空实现最容易被忽略。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interviewer import Interviewer
from knowledge import pick_tech_topics, role_topics
from llm import MockLLM
from resume import parse_jd
from state_machine import InterviewStateMachine

JD_TEXT = """# Agent 应用开发实习生
1. 参与 LLM Agent 产品开发，负责工具调用链路
2. 搭建 RAG 检索增强链路，包括切分、召回、重排
3. 熟悉 Python，了解 FastAPI
4. 熟悉 LangChain / LangGraph 者优先
5. 了解向量数据库与检索原理
"""


def _sm():
    sm = InterviewStateMachine(["项目A"], ["话题1"])
    sm.phase = "PROJECT_PROBE"
    sm.probe.current = "项目A"
    return sm


class TestParseJD(unittest.TestCase):
    def test_extracts_tech_keywords(self):
        info = parse_jd(JD_TEXT)
        kws = [k.lower() for k in info["keywords"]]
        self.assertIn("python", kws)
        for expect in ("rag", "agent"):
            self.assertTrue(any(expect in k for k in kws), f"应抽出 {expect}")

    def test_extracts_duties(self):
        info = parse_jd(JD_TEXT)
        self.assertTrue(info["duties"])
        self.assertTrue(any("Agent" in d or "RAG" in d for d in info["duties"]))

    def test_empty_jd_is_safe(self):
        self.assertEqual(parse_jd("")["keywords"], [])
        self.assertEqual(parse_jd(None)["duties"], [])


class TestJDPrefersTopics(unittest.TestCase):
    def test_jd_keywords_prioritize_topics(self):
        # JD 写"评测"、知识库话题叫"评估体系"，必须靠同义词映射命中
        picked = pick_tech_topics("agent_app", n=3, seed=1, prefer=["评测", "编排"])
        self.assertIn("评估体系", picked, f"同义词『评测』应命中『评估体系』，实际: {picked}")
        self.assertIn("多 Agent 编排", picked, f"『编排』应命中『多 Agent 编排』，实际: {picked}")

    def test_no_prefer_still_returns_n(self):
        picked = pick_tech_topics("agent_app", n=3, seed=1)
        self.assertEqual(len(picked), 3)
        self.assertTrue(set(picked) <= set(role_topics("agent_app")))

    def test_prefer_pads_with_other_topics(self):
        # 只有 1 个命中时，其余用岗位话题补齐，不能少于 n
        picked = pick_tech_topics("agent_app", n=3, seed=1, prefer=["编排"])
        self.assertEqual(len(picked), 3)
        self.assertEqual(picked[0], "多 Agent 编排")


class TestPersona(unittest.TestCase):
    def test_persona_changes_wording(self):
        base = Interviewer(MockLLM(), persona="balanced").next_question(_sm(), stack="Python")
        strict = Interviewer(MockLLM(), persona="strict").next_question(_sm(), stack="Python")
        self.assertNotEqual(base, strict, "不同人设应产出不同措辞")

    def test_all_personas_produce_question(self):
        for p in ("balanced", "strict", "warm", "academic", "engineering", "efficient"):
            q = Interviewer(MockLLM(), persona=p).next_question(_sm(), stack="Python")
            self.assertTrue(q and len(q) > 5, f"{p} 未产出有效问题")

    def test_invalid_persona_falls_back(self):
        iv = Interviewer(MockLLM(), persona="不存在的风格")
        self.assertEqual(iv.persona, "balanced")


if __name__ == "__main__":
    unittest.main(verbosity=2)

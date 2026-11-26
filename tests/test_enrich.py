"""联网扩题的离线测试：抽取规则 + LLM 过滤。

不测网络（测试不该依赖外部站点），只测两个纯逻辑部分：
  1. extract_questions —— 从页面正文里抽问句
  2. llm_filter —— 挡掉广告/新闻类噪声

这两条是"联网扩题能不能用"的关键：抽取错了题库全是噪声；
过滤失效就没人愿意看题库了。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from enrich_bank import extract_questions, llm_filter, fetch_text  # noqa: E402


class TestExtract(unittest.TestCase):
    def test_picks_question_lines(self):
        text = """
        1. RAG 是什么？
        2. 为什么有了长上下文还需要 RAG？
        3. 本题讲解了 RAG 的历史沿革。
        """
        got = extract_questions(text, "RAG 检索增强生成")
        self.assertEqual(len(got), 2)
        self.assertTrue(all("？" not in g for g in got), "问号应被去掉")

    def test_drops_too_short_and_too_long(self):
        short = "RAG？"
        long = "RAG " + "很长的解释" * 40 + "？"
        self.assertEqual(extract_questions(short, "RAG"), [])
        self.assertEqual(extract_questions(long, "RAG"), [])

    def test_drops_navigation_noise(self):
        text = "RAG 的知识点？扫码关注公众号领取资料？版权归原作者所有？"
        self.assertEqual(extract_questions(text, "RAG"), [])

    def test_keeps_general_tech_question(self):
        # 话题词没出现，但含通用技术词，且是个问题
        got = extract_questions("为什么需要评测集？", "结构化输出")
        self.assertEqual(len(got), 1)

    def test_dedupes_same_question(self):
        text = "RAG 是什么？\nRAG 是什么 ?"
        self.assertEqual(len(extract_questions(text, "RAG")), 1)


class _FakeFilterLLM:
    def __init__(self, payload):
        self.payload = payload

    def chat_json(self, messages, *, task="", hint=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class TestLLMFilter(unittest.TestCase):
    def test_keeps_listed(self):
        llm = _FakeFilterLLM({"keep": [{"i": 0, "q": "为什么长上下文下仍需要 RAG？"}]})
        out = llm_filter(llm, "RAG", ["石头扫地机器人续航多久？"], role="agent_app")
        self.assertEqual(out, ["为什么长上下文下仍需要 RAG？"])

    def test_empty_keep(self):
        llm = _FakeFilterLLM({"keep": []})
        self.assertEqual(llm_filter(llm, "RAG", ["广告？"]), [])

    def test_llm_failure_returns_empty_not_crash(self):
        llm = _FakeFilterLLM(RuntimeError("model down"))
        self.assertEqual(llm_filter(llm, "RAG", ["问题？"]), [])

    def test_no_candidates_skips_llm(self):
        self.assertEqual(llm_filter(_FakeFilterLLM(RuntimeError("should not call")), "RAG", []), [])


class TestFetchText(unittest.TestCase):
    def test_strips_script_and_tags(self):
        # 用 data: URL 避免测试依赖网络
        raw = "<html><script>var x=1;</script><style>.a{}</style><p>RAG 是什么？</p></html>"
        text = raw
        import re
        text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        self.assertNotIn("var x", text)
        self.assertIn("RAG", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

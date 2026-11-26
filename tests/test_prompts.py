"""Prompt 版本管理 + 路由后端的测试。

为什么这两个要测：
  1. prompt 版本是对外能力的一部分（`--prompt-version v2`），加载失败会静默回退默认，
     用户以为自己在测 v2，其实跑的是默认 prompt——这种"静默失效"必须被测试挡住。
  2. 路由后端决定 A/B 的结论是否成立：如果 evaluator 也走了真模型，
     那 A/B 就不是在比 prompt，而是在比运气（评分方差）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prompts_loader
from llm import MockLLM
from routing import RoutingLLM


class TestPromptLoader(unittest.TestCase):
    def test_list_versions_finds_files(self):
        versions = prompts_loader.list_versions("interviewer")
        self.assertIn("v1", versions)
        self.assertIn("v2", versions)

    def test_load_known_version_differs(self):
        a = prompts_loader.load_prompt("interviewer", "v1")
        b = prompts_loader.load_prompt("interviewer", "v2")
        self.assertNotEqual(a, b)

    def test_missing_version_falls_back(self):
        out = prompts_loader.load_prompt("interviewer", "nonexistent_xyz")
        self.assertTrue(out.strip(), "回退也要有内容，不能返回空串")

    def test_default_returns_config_prompt(self):
        from config import INTERVIEWER_SYSTEM

        self.assertEqual(prompts_loader.load_prompt("interviewer", "default"), INTERVIEWER_SYSTEM)

    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as d:
            old = prompts_loader.PROMPTS_DIR
            prompts_loader.PROMPTS_DIR = d
            try:
                prompts_loader.save_prompt("interviewer", "vtest", "  测试用 prompt  ")
                self.assertEqual(prompts_loader.load_prompt("interviewer", "vtest"), "测试用 prompt")
                self.assertIn("vtest", prompts_loader.list_versions("interviewer"))
            finally:
                prompts_loader.PROMPTS_DIR = old


class _FakeLLM(MockLLM):
    def __init__(self, tag):
        super().__init__()
        self.tag = tag
        self.seen = []

    def chat(self, messages, *, task="", hint=None, json_mode=False):
        self.seen.append(task)
        return self.tag


class TestRoutingLLM(unittest.TestCase):
    def test_routes_by_task(self):
        a, b = _FakeLLM("A"), _FakeLLM("B")
        llm = RoutingLLM({"interviewer": a}, default=b)
        self.assertEqual(llm.chat([], task="interviewer"), "A")
        self.assertEqual(llm.chat([], task="evaluator"), "B")
        self.assertEqual(a.seen, ["interviewer"])
        self.assertEqual(b.seen, ["evaluator"])

    def test_call_counts(self):
        llm = RoutingLLM({"interviewer": _FakeLLM("A")}, default=_FakeLLM("B"))
        for _ in range(3):
            llm.chat([], task="interviewer")
        llm.chat([], task="coach")
        self.assertEqual(llm.calls, {"interviewer": 3, "coach": 1})

    def test_is_mock_only_when_all_mock(self):
        mock_only = RoutingLLM({"interviewer": MockLLM()}, default=MockLLM())
        self.assertTrue(mock_only.is_mock)

        real_like = _FakeLLM("X")
        real_like.is_mock = False
        mixed = RoutingLLM({"interviewer": real_like}, default=MockLLM())
        self.assertFalse(mixed.is_mock, "只要有一条真模型路由，就不能标成 mock")


if __name__ == "__main__":
    unittest.main(verbosity=2)

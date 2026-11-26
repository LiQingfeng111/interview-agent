"""真实模型暴露出来的 bug 的回归测试。

这些用例都来自实际跑本地小模型（Qwen2.5-1.5B）时观察到的现象：
  1. 同一个问题隔两三问又问一次（"你如何确保检索准确"连问 5 次）
  2. 评分官把 covered 全填成已覆盖 → 覆盖率恒 5/5、诊断恒空
  3. 兜底问法在不同层级撞成同一句
  4. 提问官输出"接下来的问题是：…"这类前缀
弱模型会放大系统缺陷，所以这些测试是给"换任何模型都不崩"兜底的。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluator import detect_covered, reconcile_covered
from interviewer import Interviewer, _clean_question, _norm_question
from llm import MockLLM
from state_machine import InterviewStateMachine


def _sm(project="项目A"):
    sm = InterviewStateMachine([project], ["话题1"])
    sm.phase = "PROJECT_PROBE"
    sm.probe.current = project
    return sm


class TestRepeatGuard(unittest.TestCase):
    def test_norm_ignores_punctuation(self):
        a = _norm_question("你如何确保检索准确？")
        b = _norm_question("你如何确保检索准确")
        self.assertEqual(a, b)

    def test_detects_repeat_beyond_last_turn(self):
        sm = _sm()
        sm.history = [
            {"q": "你如何确保检索准确？", "a": "x"},
            {"q": "讲讲你的指标", "a": "x"},
            {"q": "说说失败方案", "a": "x"},
        ]
        iv = Interviewer(MockLLM(), plan={})
        self.assertTrue(iv._is_repeat("你如何确保检索准确", sm), "隔两问的重复也要抓到")

    def test_new_question_not_flagged(self):
        sm = _sm()
        sm.history = [{"q": "你如何确保检索准确？", "a": "x"}]
        iv = Interviewer(MockLLM(), plan={})
        self.assertFalse(iv._is_repeat("你的 baseline 是什么？", sm))

    def test_fallback_differs_by_level(self):
        iv = Interviewer(MockLLM(), plan={})
        q1 = iv._fallback({"phase": "PROJECT_PROBE", "project": "P", "ladder_level": 1})
        q4 = iv._fallback({"phase": "PROJECT_PROBE", "project": "P", "ladder_level": 4})
        self.assertNotEqual(q1, q4, "不同层级的兜底问法不能是同一句")


class TestCleanQuestion(unittest.TestCase):
    def test_strips_small_model_chatter(self):
        cases = {
            "接下来的问题是：你的 baseline 是什么？": "你的 baseline 是什么？",
            "请告诉我，你的指标怎么测的？": "你的指标怎么测的？",
            "好的，面试官：讲讲失败方案": "讲讲失败方案",
        }
        for raw, want in cases.items():
            self.assertEqual(_clean_question(raw), want, raw)

    def test_strips_inner_monologue_leak(self):
        # deepseek-flash 实测输出：把分析过程写进问题里
        raw = ("候选人没有回答扫描版 PDF 的问题，而是直接讲了方案选型。"
               "我换个角度追问边界：300 份课件里有多少是扫描版？")
        got = _clean_question(raw)
        self.assertEqual(got, "300 份课件里有多少是扫描版？")

    def test_too_short_rejected(self):
        self.assertEqual(_clean_question("嗯。"), "")


class TestCoverageReconcile(unittest.TestCase):
    def test_trust_model_when_few(self):
        self.assertEqual(reconcile_covered(["baseline"], "随便答的"), ["baseline"])

    def test_all_five_claims_rejected(self):
        # 弱模型把 5 个要素全填上，但回答里没有任何对应证据
        answer = "就是做了一下，还行。"
        got = reconcile_covered(["目标", "baseline", "指标", "失败方案", "个人贡献"], answer)
        self.assertEqual(got, [], "全部声称覆盖应被视为无效")

    def test_partial_overlap_kept(self):
        answer = "我对比的基线是旧版，召回 71%，现在 82%。"
        claimed = ["目标", "baseline", "指标", "失败方案"]
        got = reconcile_covered(claimed, answer)
        self.assertIn("baseline", got)
        self.assertIn("指标", got)
        self.assertNotIn("失败方案", got)

    def test_empty_model_claim_falls_back_to_rule(self):
        answer = "试过几个方案都不行，后来改掉了。"
        self.assertEqual(reconcile_covered([], answer), detect_covered(answer))


if __name__ == "__main__":
    unittest.main(verbosity=2)

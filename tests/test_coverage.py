"""要素覆盖检测 + 跳过逻辑测试。

背景（用户提出的需求）：候选人在「目标」那一问里可能顺带讲了 baseline 和指标，
真人面试官会跳过，不会机械地再问一遍。要跳过，系统必须先判断"讲没讲"。

分工：
  - 语义判断 → 评分官（真实模型）；规则兜底 → detect_covered
  - 状态记账 → ProbeState.covered_elements（必须由代码做）
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import ELEMENT_NAMES
from evaluator import detect_covered
from interviewer import Interviewer
from llm import MockLLM
from planner import mock_plan
from schemas import Evaluation, Project, normalize_eval
from state_machine import InterviewStateMachine


class TestDetectCovered(unittest.TestCase):
    def test_detects_baseline_and_metric(self):
        got = detect_covered("我对比的基线是原来那版的召回 71%，现在做到 82%。")
        self.assertIn("baseline", got)
        self.assertIn("指标", got)

    def test_detects_failed_plan(self):
        got = detect_covered("试过几个方案都不行，后来改掉了自研的重排。")
        self.assertIn("失败方案", got)

    def test_detects_ownership(self):
        got = detect_covered("这部分是我独立做的，另外两个人负责前端。")
        self.assertIn("个人贡献", got)

    def test_vague_answer_covers_nothing(self):
        self.assertEqual(detect_covered("就是做了一下，还行。"), [])

    def test_only_valid_element_names(self):
        for e in detect_covered("我对比了基线，试过失败方案，指标提升了 30%，我独立负责。"):
            self.assertIn(e, ELEMENT_NAMES)


class TestNormalizeCovered(unittest.TestCase):
    def _base(self):
        return {"accuracy": 4, "depth": 4, "tradeoff": 3, "boundary": 3,
                "clarity": 4, "verdict": "pass", "weakest": "boundary"}

    def test_missing_covered_defaults_empty(self):
        out = normalize_eval(self._base())
        self.assertEqual(out["covered"], [])

    def test_alias_normalized(self):
        d = {**self._base(), "covered": ["基线", "衡量指标"]}
        out = normalize_eval(d)
        self.assertEqual(set(out["covered"]), {"baseline", "指标"})

    def test_invalid_name_dropped(self):
        d = {**self._base(), "covered": ["目标", "我瞎写的"]}
        out = normalize_eval(d)
        self.assertEqual(out["covered"], ["目标"])

    def test_string_instead_of_list(self):
        d = {**self._base(), "covered": "目标"}
        out = normalize_eval(d)
        self.assertEqual(out["covered"], ["目标"])


class TestSkipCoveredInPlan(unittest.TestCase):
    def _sm(self, covered=()):
        sm = InterviewStateMachine(["项目A"], ["话题1"])
        sm.phase = "PROJECT_PROBE"
        sm.probe.current = "项目A"
        sm.probe.covered_elements["项目A"] = set(covered)
        return sm

    def test_skips_covered_element(self):
        plan = mock_plan([Project(name="项目A")])
        iv = Interviewer(MockLLM(), plan=plan)
        sm = self._sm(covered={"目标"})
        q = iv.next_question(sm, stack="Python")
        # 「目标」的 seed 含有"最初要解决"，被跳过后不应出现
        self.assertNotIn("最初要解决", q)

    def test_asks_uncovered_first(self):
        plan = mock_plan([Project(name="项目A")])
        iv = Interviewer(MockLLM(), plan=plan)
        sm = self._sm(covered={"目标", "个人贡献"})
        q = iv.next_question(sm, stack="Python")
        self.assertTrue(any(k in q for k in ("基线", "指标", "没成的方案")), q)

    def test_mark_elements_accumulates(self):
        sm = self._sm()
        sm.probe.mark_elements("项目A", ["目标"])
        sm.probe.mark_elements("项目A", ["指标"])
        self.assertEqual(sm.probe.elements_of("项目A"), {"目标", "指标"})

    def test_mark_elements_ignores_empty(self):
        sm = self._sm()
        sm.probe.mark_elements("项目A", [])
        sm.probe.mark_elements("", ["目标"])
        self.assertEqual(sm.probe.elements_of("项目A"), set())


class TestEvaluationCarriesCovered(unittest.TestCase):
    def test_default_is_empty_list(self):
        e = Evaluation(accuracy=3, depth=3, tradeoff=3, boundary=3, clarity=3,
                       verdict="partial", weakest="depth")
        self.assertEqual(e.covered, [])

    def test_json_roundtrip(self):
        e = Evaluation(accuracy=3, depth=3, tradeoff=3, boundary=3, clarity=3,
                       verdict="partial", weakest="depth", covered=["目标"])
        self.assertEqual(e.to_dict()["covered"], ["目标"])


class TestElementPolicy(unittest.TestCase):
    """用户定的策略：每个要素最多追问一次 → 判定 → 记账放行。"""

    def _sm(self):
        sm = InterviewStateMachine(["项目A"], ["话题1"])
        sm.phase = "PROJECT_PROBE"
        sm.probe.current = "项目A"
        return sm

    def test_closed_after_two_attempts(self):
        sm = self._sm()
        self.assertFalse(sm.probe.element_closed("项目A", "baseline"))
        sm.probe.note_element_attempt("项目A", "baseline", "evaded")
        self.assertFalse(sm.probe.element_closed("项目A", "baseline"), "只问一次不该关闭")
        sm.probe.note_element_attempt("项目A", "baseline", "evaded")
        self.assertTrue(sm.probe.element_closed("项目A", "baseline"), "问满两次应关闭")

    def test_covered_closes_immediately(self):
        sm = self._sm()
        sm.probe.note_element_attempt("项目A", "指标", "covered")
        self.assertTrue(sm.probe.element_closed("项目A", "指标"))

    def test_status_three_states_in_report(self):
        sm = self._sm()
        sm.probe.note_element_attempt("项目A", "目标", "covered")
        sm.probe.note_element_attempt("项目A", "baseline", "evaded")
        sm.probe.note_element_attempt("项目A", "baseline", "evaded")
        sm.probe.note_element_attempt("项目A", "失败方案", "unknown")
        rep = sm.probe.element_report("项目A")
        self.assertEqual(rep["目标"], "已答到")
        self.assertIn("回避", rep["baseline"])
        self.assertIn("未答出", rep["失败方案"])
        self.assertEqual(rep["个人贡献"], "未问到")

    def test_covered_without_asking_is_marked(self):
        """顺带提及的要素，报告不能显示成"未问到"。"""
        sm = self._sm()
        sm.probe.mark_elements("项目A", ["指标"])
        self.assertIn("顺带", sm.probe.element_report("项目A")["指标"])

    def test_retry_asks_same_element_differently(self):
        plan = mock_plan([Project(name="项目A")])
        iv = Interviewer(MockLLM(), plan=plan)
        sm = self._sm()
        first = iv.next_question(sm, stack="Python")
        target1 = iv.last_target
        sm.probe.attempts = 1  # 触发同要素第二次追问
        second = iv.next_question(sm, stack="Python")
        self.assertNotEqual(first, second, "第二次追问不能与第一次同句")
        self.assertEqual(iv.last_target, target1, "第二次追问应仍针对同一要素")

    def test_closed_element_skipped_by_plan(self):
        plan = mock_plan([Project(name="项目A")])
        iv = Interviewer(MockLLM(), plan=plan)
        sm = self._sm()
        for e in ("目标", "baseline"):
            sm.probe.note_element_attempt("项目A", e, "unknown")
            sm.probe.note_element_attempt("项目A", e, "unknown")
        q = iv.next_question(sm, stack="Python")
        self.assertNotIn("最初要解决", q)
        self.assertNotIn("对比的基线", q)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""面试前计划测试。

核心断言：LLM 可以负责"想问题"，但**覆盖必须由代码校验**。
真实 LLM 最常见的三种失败：漏项目、只写一个要素、seed 留空。
这三种都要能被 validate_plan 抓出来。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interviewer import Interviewer
from llm import MockLLM
from planner import build_plan, mock_plan, validate_plan
from schemas import Project
from state_machine import InterviewStateMachine

PROJECTS = [Project(name="项目A"), Project(name="项目B")]


class TestValidatePlan(unittest.TestCase):
    def test_mock_plan_is_valid(self):
        plan = mock_plan(PROJECTS)
        ok, issues = validate_plan(plan, PROJECTS)
        self.assertTrue(ok, f"确定性计划应通过校验，问题: {issues}")

    def test_missing_project_detected(self):
        plan = {"projects": [{"name": "项目A", "focus": [
            {"element": e, "seed": "s"} for e in ("目标", "baseline", "指标")
        ]}]}
        ok, issues = validate_plan(plan, PROJECTS)
        self.assertFalse(ok)
        self.assertTrue(any("漏掉项目" in i for i in issues), issues)

    def test_too_few_elements_detected(self):
        plan = {"projects": [
            {"name": "项目A", "focus": [{"element": "目标", "seed": "s"}]},
            {"name": "项目B", "focus": [
                {"element": e, "seed": "s"} for e in ("目标", "baseline", "指标")
            ]},
        ]}
        ok, issues = validate_plan(plan, PROJECTS)
        self.assertFalse(ok)
        self.assertTrue(any("要素" in i for i in issues), issues)

    def test_empty_seed_detected(self):
        plan = {"projects": [
            {"name": "项目A", "focus": [
                {"element": "目标", "seed": ""},
                {"element": "baseline", "seed": "s"},
                {"element": "指标", "seed": "s"},
            ]},
            {"name": "项目B", "focus": [
                {"element": e, "seed": "s"} for e in ("目标", "baseline", "指标")
            ]},
        ]}
        ok, issues = validate_plan(plan, PROJECTS)
        self.assertFalse(ok)
        self.assertTrue(any("空" in i for i in issues), issues)

    def test_build_plan_mock_mode_offline(self):
        plan = build_plan(MockLLM(), PROJECTS)
        self.assertEqual(len(plan["projects"]), 2)


class TestPlanDrivenQuestions(unittest.TestCase):
    def _sm(self, project="项目A"):
        sm = InterviewStateMachine([project], ["话题1"])
        sm.phase = "PROJECT_PROBE"
        sm.probe.current = project
        return sm

    def test_uses_plan_seed_first(self):
        plan = mock_plan([Project(name="项目A")])
        iv = Interviewer(MockLLM(), plan=plan)
        q = iv.next_question(self._sm(), stack="Python")
        self.assertIn("项目A", q)
        self.assertTrue("最初要解决" in q or "目标" in q, q)

    def test_plan_seeds_not_repeated(self):
        plan = mock_plan([Project(name="项目A")])
        iv = Interviewer(MockLLM(), plan=plan)
        sm = self._sm()
        seen = []
        for _ in range(3):
            seen.append(iv.next_question(sm, stack="Python"))
            sm.probe.ladder_level += 1
        self.assertEqual(len(set(seen)), 3, f"计划里的点不应重复问: {seen}")

    def test_higher_attempt_ignores_plan_seed(self):
        plan = mock_plan([Project(name="项目A")])
        iv = Interviewer(MockLLM(), plan=plan)
        sm = self._sm()
        sm.probe.attempts = 1  # 重追问
        q = iv.next_question(sm, stack="Python")
        # 重追问应走"换角度"变体，而不是继续念计划
        self.assertNotIn("最初要解决", q)


if __name__ == "__main__":
    unittest.main(verbosity=2)

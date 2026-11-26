"""状态机单元测试：这是项目里最该有测试的部分。

为什么只测状态机而不测 prompt？
因为 prompt 的效果无法用断言表达（"问得好"不是布尔值），
而状态机的行为可以精确断言：
    - 阶段顺序对不对
    - 木梯会不会升、会不会卡死
    - 项目会不会重复挖
    - max_turns 保险丝有没有生效

用标准库 unittest，零依赖：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import LADDER, PHASE_CONFIG
from state_machine import ORDER, InterviewStateMachine


def make_sm(projects=None, topics=None, max_turns=30):
    return InterviewStateMachine(
        projects or ["项目A", "项目B"],
        topics or ["话题1", "话题2"],
        max_turns=max_turns,
    )


class TestPhaseFlow(unittest.TestCase):
    def test_initial_phase(self):
        sm = make_sm()
        self.assertEqual(sm.phase, "OPENING")
        self.assertEqual(sm.total_turns, 0)

    def test_cannot_exit_before_min_turns(self):
        sm = make_sm()
        self.assertFalse(sm.can_exit())
        sm.record("q", "a")
        self.assertTrue(sm.can_exit())

    def test_phase_order_is_stable(self):
        self.assertEqual(
            ORDER,
            ["OPENING", "SELF_INTRO", "PROJECT_PROBE", "TECH_QA",
             "CANDIDATE_ASK", "FEEDBACK"],
        )

    def test_advance_step_by_step(self):
        sm = make_sm()
        seen = [sm.phase]
        for _ in range(len(ORDER) - 1):
            sm.turn_in_phase = 99
            seen.append(sm.advance("test"))
        self.assertEqual(seen, ORDER)

    def test_advance_stops_at_feedback(self):
        sm = make_sm()
        sm.phase = "FEEDBACK"
        self.assertEqual(sm.advance("test"), "FEEDBACK")

    def test_probe_enters_first_project(self):
        sm = make_sm(["项目A", "项目B"])
        sm.phase = "SELF_INTRO"
        sm.turn_in_phase = 99
        sm.advance()
        self.assertEqual(sm.phase, "PROJECT_PROBE")
        self.assertEqual(sm.probe.current, "项目A")


class TestLadder(unittest.TestCase):
    def setUp(self):
        self.sm = make_sm(["项目A"])
        self.sm.phase = "PROJECT_PROBE"
        self.sm.probe.current = "项目A"

    def test_pass_raises_level(self):
        self.sm.note_eval("pass", "depth", "")
        self.assertEqual(self.sm.probe.ladder_level, 1)

    def test_one_partial_does_not_raise(self):
        self.sm.note_eval("partial", "depth", "")
        self.assertEqual(self.sm.probe.ladder_level, 0)

    def test_two_partials_raise(self):
        """答得不完美但要往前走，否则会卡在第一层反复问。"""
        self.sm.note_eval("partial", "depth", "")
        self.sm.note_eval("partial", "depth", "")
        self.assertEqual(self.sm.probe.ladder_level, 1)

    def test_single_fail_retries_same_level(self):
        self.sm.note_eval("fail", "depth", "")
        self.assertEqual(self.sm.probe.ladder_level, 0)
        self.assertEqual(self.sm.probe.strikes, 1)

    def test_two_fails_raise_level_not_switch(self):
        """答不上来两次 → 升层继续问，不是判这个人不行。

        这是接真实模型后改的策略：评分偏严时，"fail 两次换项目"
        会在第 0 层就把项目切走，木梯永远到不了取舍/边界/反事实。
        """
        self.sm.probe.projects = ["项目A", "项目B"]
        self.sm.note_eval("fail", "depth", "")
        self.sm.note_eval("fail", "depth", "")
        self.assertEqual(self.sm.probe.current, "项目A", "不该这么早换项目")
        self.assertEqual(self.sm.probe.ladder_level, 1, "应升到下一层继续问")

    def test_switch_project_only_at_top_level(self):
        self.sm.probe.projects = ["项目A", "项目B"]
        # 一路 fail 到顶：每层两次 fail 升一层，到顶后再 fail 才换项目
        for _ in range(len(LADDER) * 2 + 2):
            self.sm.note_eval("fail", "depth", "")
            if self.sm.probe.current == "项目B":
                break
        self.assertEqual(self.sm.probe.current, "项目B")

    def test_level_caps_at_top(self):
        for _ in range(20):
            self.sm.note_eval("pass", "depth", "")
        self.assertLessEqual(self.sm.probe.ladder_level, len(LADDER) - 1)

    def test_no_pingpong_between_projects(self):
        """A→B 之后不能再回到 A（第一版的真实 bug）。"""
        self.sm.probe.projects = ["项目A", "项目B", "项目C"]
        for _ in range(len(LADDER) * 2 + 2):
            self.sm.note_eval("fail", "depth", "")
            if self.sm.probe.current == "项目B":
                break
        self.assertEqual(self.sm.probe.current, "项目B")
        for _ in range(len(LADDER) * 2 + 2):
            self.sm.note_eval("fail", "depth", "")
            if self.sm.probe.current == "项目C":
                break
        self.assertEqual(self.sm.probe.current, "项目C")
        self.assertNotEqual(self.sm.probe.current, "项目A")

    def test_exhausted_when_all_projects_worn_out(self):
        self.sm.probe.projects = ["项目A"]
        for _ in range(len(LADDER) * 2 + 2):
            self.sm.note_eval("fail", "depth", "")
        self.assertTrue(self.sm.probe.exhausted)
        self.assertTrue(self.sm._probe_done())

    def test_deepest_layer_tracking(self):
        self.assertEqual(self.sm.deepest_layer_reached(), LADDER[0])
        self.sm.note_eval("pass", "depth", "")
        self.sm.note_eval("pass", "depth", "")
        self.assertEqual(self.sm.deepest_layer_reached(), LADDER[1])


class TestGuards(unittest.TestCase):
    def test_max_turns_breaks_probe(self):
        sm = make_sm(["项目A", "项目B"], max_turns=5)
        sm.phase = "PROJECT_PROBE"
        sm.probe.current = "项目A"
        sm.total_turns = 5
        sm.turn_in_phase = 3   # 满足 min_turns
        self.assertTrue(sm.can_exit())

    def test_probe_not_done_until_all_marked(self):
        sm = make_sm(["项目A", "项目B"])
        sm.phase = "PROJECT_PROBE"
        sm.turn_in_phase = 10
        sm.probe.done = {"项目A"}
        self.assertFalse(sm._probe_done())
        sm.probe.done.add("项目B")
        self.assertTrue(sm._probe_done())

    def test_tech_qa_exits_after_all_topics(self):
        sm = make_sm(["A"], ["话题1", "话题2"])
        sm.phase = "TECH_QA"
        sm.turn_in_phase = PHASE_CONFIG["TECH_QA"]["min_turns"]
        sm.tech.asked = {"话题1"}
        self.assertFalse(sm.can_exit())
        sm.tech.asked.add("话题2")
        self.assertTrue(sm.can_exit())

    def test_summary_is_json_safe(self):
        import json
        sm = make_sm()
        sm.record("q", "a")
        json.dumps(sm.summary(), ensure_ascii=False)  # 不抛异常即可


if __name__ == "__main__":
    unittest.main(verbosity=2)

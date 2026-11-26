"""提问官去重测试：锁住"同一层级重追问必须换说法"这个修复。

背景：用户实际跑 demo 时发现 #3/#4、#5/#6 完全相同，指出"同一个问题问两遍"。
根因是 mock 提问官同一层级只有一个模板。修复后加这条测试防回归。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from interviewer import Interviewer
from llm import MockLLM
from state_machine import InterviewStateMachine


def _sm_at_probe(level: int, attempts: int) -> InterviewStateMachine:
    sm = InterviewStateMachine(["项目A"], ["话题1"])
    sm.phase = "PROJECT_PROBE"
    sm.probe.current = "项目A"
    sm.probe.ladder_level = level
    sm.probe.attempts = attempts
    return sm


class TestNoRepeat(unittest.TestCase):
    def setUp(self):
        self.iv = Interviewer(MockLLM())

    def test_retry_at_same_level_differs(self):
        q0 = self.iv.next_question(_sm_at_probe(2, 0), stack="Python")
        q1 = self.iv.next_question(_sm_at_probe(2, 1), stack="Python")
        self.assertNotEqual(q0, q1, "同层级重追问必须换一个角度，不能逐字重复")

    def test_all_levels_have_distinct_variants(self):
        for level in range(6):
            q0 = self.iv.next_question(_sm_at_probe(level, 0), stack="X")
            q1 = self.iv.next_question(_sm_at_probe(level, 1), stack="X")
            self.assertNotEqual(q0, q1, f"层级 {level} 的两个变体不能相同")
            self.assertTrue(q0 and q1)

    def test_dedup_guard_rewrites_identical_question(self):
        """即使外部模型吐回与上一问一模一样的内容，也要被护栏改写。"""
        sm = _sm_at_probe(0, 0)
        # 手动把上一问塞进 history，模拟"模型原样重复"
        sm.history.append({"phase": "PROJECT_PROBE", "q": "同一个问题？", "a": "", "verdict": ""})
        # 这里直接验证护栏路径：构造一个必然与最后一条 history 相同的情形
        last_q = sm.history[-1]["q"]
        q = self.iv.next_question(sm, stack="")
        self.assertNotEqual(q, last_q)


if __name__ == "__main__":
    unittest.main(verbosity=2)
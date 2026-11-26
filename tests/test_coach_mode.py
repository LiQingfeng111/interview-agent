"""评估模式 + 提前结束的测试。

两个断言最重要：
  1. 教练卡片**不能改评分**（verdict 由评分官定，注释只读）；
  2. 提前结束**必须出完整报告**，且结束原因如实写 user_quit，
     不能因为没答完就把缺口算成"未答出"。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from coach import Coach, TurnCoach, render_card
from ledger import E
from llm import MockLLM
from resume import parse_resume_best
from schemas import Evaluation
from session import InterviewSession

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESUME = os.path.join(ROOT, "data", "test_resumes", "our_sample.md")


def _projects():
    return parse_resume_best(open(RESUME, encoding="utf-8").read())


def _ev(verdict="fail", accuracy=3, depth=1, tradeoff=2, boundary=2, clarity=4, weakest="depth"):
    return Evaluation(accuracy=accuracy, depth=depth, tradeoff=tradeoff,
                      boundary=boundary, clarity=clarity, verdict=verdict,
                      weakest=weakest, evidence="", followup_hint="")


class TestTurnCoach(unittest.TestCase):
    def test_disabled_returns_flag_only(self):
        tc = TurnCoach(MockLLM(), enabled=False)
        out = tc.review("q", "a", _ev())
        self.assertEqual(out, {"enabled": False})

    def test_rule_card_has_why_and_gaps(self):
        tc = TurnCoach(MockLLM(), enabled=True)
        out = tc.review("为什么用这个方案？", "因为大家都这么用。", _ev())
        self.assertTrue(out["enabled"])
        self.assertIn("depth", out["why"])            # 指出最弱维度
        self.assertGreaterEqual(len(out["gaps"]), 1)
        self.assertEqual(out["source"], "rule")

    def test_card_does_not_mutate_verdict(self):
        ev = _ev(verdict="fail")
        tc = TurnCoach(MockLLM(), enabled=True)
        tc.review("q", "a", ev)
        self.assertEqual(ev.verdict, "fail", "教练层不得改写评分判定")

    def test_gaps_include_expected_points(self):
        tc = TurnCoach(MockLLM(), enabled=True)
        out = tc.review("q", "a", _ev(), points=["基线是什么", "测试集多大"])
        joined = " ".join(out["gaps"])
        self.assertIn("基线", joined)

    def test_history_accumulates(self):
        tc = TurnCoach(MockLLM(), enabled=True)
        for _ in range(3):
            tc.review("q", "a", _ev())
        self.assertEqual(len(tc.history), 3)

    def test_render_card(self):
        tc = TurnCoach(MockLLM(), enabled=True)
        text = render_card(tc.review("q", "a", _ev()))
        self.assertIn("本轮点评", text)
        self.assertIn("为什么", text)


class TestSessionCoach(unittest.TestCase):
    def test_cards_only_when_enabled(self):
        p = _projects()
        off = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                               max_turns=4, verbose=False, save=False, seed=1)
        off.run_scripted(["目标 300 份。"] * 4)
        self.assertEqual(off.cards, [], "评估模式关闭时不该产生卡片（省 token）")

        on = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                              max_turns=4, verbose=False, save=False, seed=1, coach=True)
        on.run_scripted(["目标 300 份。"] * 4)
        self.assertGreater(len(on.cards), 0)


class TestEarlyEnd(unittest.TestCase):
    def test_end_early_reason_and_report(self):
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=16, verbose=False, save=False, seed=2, audit=True)
        s.start()
        s.step("目标 300 份。")
        s.step("召回 71%。")
        fb = s.end_early()
        self.assertEqual(s.finish_reason, "user_quit")
        self.assertTrue(fb.strip(), "提前结束也必须给反馈")
        data = s.result().to_dict()
        self.assertEqual(data["meta"]["audit"]["finish_reason"], "user_quit")

    def test_end_early_logs_finish_event(self):
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=16, verbose=False, save=False, seed=2, audit=True)
        s.start()
        s.step("目标 300 份。")
        s.end_early()
        fins = list(s.ledger.of_type(E.FINISH))
        self.assertEqual(len(fins), 1)
        self.assertEqual(fins[0].payload["reason"], "user_quit")

    def test_unasked_elements_stay_unasked(self):
        """没问到的要素应记"未问到"，不该算候选人"未答出"。"""
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=16, verbose=False, save=False, seed=2)
        s.start()
        s.step("目标 300 份。")
        s.end_early()
        rep = s.sm.probe.element_report(p[0].name)
        self.assertTrue(any(v == "未问到" for v in rep.values()),
                        f"提前结束后应保留'未问到'状态，实际 {rep}")

    def test_can_end_twice_safely(self):
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=8, verbose=False, save=False, seed=2)
        s.start()
        s.end_early()
        s.end_early()      # 二次点击不应该炸/重复记账
        self.assertEqual(len(list(s.ledger.of_type(E.FINISH))), 2)


class TestCoachFeedback(unittest.TestCase):
    def test_mock_feedback_has_five_sections(self):
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=5, verbose=False, save=False, seed=3)
        r = s.run_scripted(["目标 300 份。"] * 6)
        for sec in ("【总评】", "【哪里答对了】", "【哪里答虚了】",
                    "【面试官下一步会怎么追】", "【下一步训练动作】"):
            self.assertIn(sec, r.feedback)

    def test_feedback_quotes_answers_not_invented(self):
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=5, verbose=False, save=False, seed=3)
        r = s.run_scripted(["目标 300 份。"] * 6)
        self.assertIn("300", r.feedback, "总结必须引用候选人的原话内容")


if __name__ == "__main__":
    unittest.main(verbosity=2)
class TestPhasePoints(unittest.TestCase):
    """自我介绍不能用项目深挖的考点去量。

    这是从真实截图里发现的：一段合格的自我介绍被判 fail，
    原因是评分官拿到的考点是「chunk 策略/召回率怎么测」——
    测试只验流程能跑通，验不出"考点配错阶段"这种合理性问题。
    """

    def test_intro_uses_intro_points(self):
        from config import SELF_INTRO_POINTS
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=6, verbose=False, save=False, seed=3)
        s.start()
        s.step("目标 300 份。")
        if s.sm.phase == "SELF_INTRO":
            self.assertEqual(s._expected_points(), list(SELF_INTRO_POINTS))

    def test_probe_uses_topic_points(self):
        p = _projects()
        s = InterviewSession(llm=MockLLM(), projects=p, role_key="ai_algo",
                             max_turns=6, verbose=False, save=False, seed=3)
        s.start()
        for _ in range(3):
            s.step("目标 300 份，召回 71%。")
        points = s._expected_points()
        # 深挖阶段不复用自我介绍考点（题库考点只在 TECH_QA 阶段才会带上）
        self.assertNotIn("求职意向：为什么投这个岗位（和上面主线对得上）", points)
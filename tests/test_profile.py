"""能力画像测试：锁住"分数在 0-100、权重和为 1、mock 必须降置信"这几条底线。

能力画像最容易出的问题不是算错，而是**让人误信**：
一个 mock 模式跑出来的"工程能力 78 分"如果没标低置信，
就会被当成真结论。所以置信度也要测。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import RUBRIC_DIMS
from profile import ABILITY_DIMS, build_profile
from schemas import Evaluation, Project, Turn


def _turn(i, phase, level, scores, answer="够了，这是一段足够长的回答内容，用于测试。"):
    e = Evaluation(
        **{d: scores.get(d, 3) for d in RUBRIC_DIMS},
        verdict=scores.get("verdict", "partial"),
        weakest="depth",
    )
    return Turn(idx=i, phase=phase, question=f"问题{i}", answer=answer,
                evaluation=e, ladder_level=level)


class TestProfile(unittest.TestCase):
    def test_weights_sum_to_one(self):
        total = sum(w for _, _, w in ABILITY_DIMS)
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_all_values_in_range(self):
        turns = [
            _turn(1, "PROJECT_PROBE", 2, {"depth": 4, "accuracy": 4}),
            _turn(2, "TECH_QA", 0, {"depth": 3, "accuracy": 3}),
        ]
        stats = {"dims": {d: 3.5 for d in RUBRIC_DIMS}}
        p = build_profile(turns, stats, [Project(name="P", stack=["Python"])], 2)
        self.assertTrue(p["available"])
        for v in p["values"].values():
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)
        self.assertGreaterEqual(p["overall"], 0.0)
        self.assertLessEqual(p["overall"], 100.0)

    def test_mock_lowers_confidence(self):
        turns = [_turn(i, "PROJECT_PROBE", 1, {}) for i in range(1, 21)]
        stats = {"dims": {d: 3 for d in RUBRIC_DIMS}}
        real = build_profile(turns, stats, [Project(name="P")], 1, mode="real")
        mock = build_profile(turns, stats, [Project(name="P")], 1, mode="mock")
        self.assertLess(mock["confidence"], real["confidence"])

    def test_few_turns_lowers_confidence(self):
        few = [_turn(1, "PROJECT_PROBE", 1, {})]
        many = [_turn(i, "PROJECT_PROBE", 1, {}) for i in range(1, 21)]
        stats = {"dims": {d: 3 for d in RUBRIC_DIMS}}
        p_few = build_profile(few, stats, [], 1, mode="real")
        p_many = build_profile(many, stats, [], 1, mode="real")
        self.assertLess(p_few["confidence"], p_many["confidence"])

    def test_no_evals_returns_unavailable(self):
        p = build_profile([], {}, [], 0)
        self.assertFalse(p["available"])

    def test_has_disclaimer(self):
        turns = [_turn(1, "PROJECT_PROBE", 1, {})]
        p = build_profile(turns, {"dims": {d: 3 for d in RUBRIC_DIMS}}, [], 1)
        self.assertIn("不等于真实能力评估", p["disclaimer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

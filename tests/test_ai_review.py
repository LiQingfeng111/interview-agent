"""AI 审核器的判定逻辑测试（纯离线，不调模型）。

为什么单独测这一层：
  "让模型审题库"最危险的地方是**模型的判断直接落到库里**。
  所以分数到动作的翻译由代码做，这段逻辑必须可测、可回归——
  否则模型换个说法，库里就多一批没验证的题。
"""
from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from ai_review import _decide, apply_result  # noqa: E402


def _q(**kw):
    base = {"id": "Q1", "role": "agent_app", "topic": "RAG", "level": "advanced",
            "question": "RAG 的 chunk 大小怎么定？", "points": ["按语义边界", "用召回率验证"],
            "source": "generated", "reviewed": False}
    base.update(kw)
    return base


def _judge(**kw):
    base = {"specific": 4, "verifiable": 4, "discriminator": 4,
            "factual_risk": 1, "points_quality": 4, "verdict": "keep"}
    base.update(kw)
    return base


class TestDecide(unittest.TestCase):
    def test_good_question_kept(self):
        verdict, new = _decide(_judge(), _q())
        self.assertEqual(verdict, "keep")
        self.assertTrue(new["reviewed"])
        self.assertIn("+ai_ok", new["source"])

    def test_factual_risk_forces_drop(self):
        """编造版本号这类必须删，不给改写机会。"""
        verdict, _ = _decide(_judge(factual_risk=5, verdict="fix",
                                    question="LangGraph 10.0 的 checkpointer 与 9.x 有什么区别？",
                                    points=["版本差异"]), _q())
        self.assertEqual(verdict, "drop")

    def test_vague_question_dropped_not_rewritten(self):
        """没有考点的（具体性≤2）不能靠"改写"混过去。"""
        verdict, _ = _decide(_judge(specific=1, verdict="fix",
                                    question="谈谈你对 RAG 的理解？", points=["随意"]), _q())
        self.assertEqual(verdict, "drop")

    def test_low_discriminator_gets_fixed(self):
        """考点在但太泛 → 修，而不是删。"""
        verdict, new = _decide(_judge(specific=4, discriminator=1, verdict="keep"), _q())
        self.assertEqual(verdict, "fix")

    def test_incomplete_rewrite_dropped(self):
        """改写但没给要点 → 不留半成品。"""
        verdict, _ = _decide(_judge(specific=3, discriminator=1, verdict="fix",
                                    question="RAG 的 chunk 怎么切？", points=[]), _q())
        self.assertEqual(verdict, "drop")

    def test_missing_points_becomes_fix(self):
        verdict, _ = _decide(_judge(points_quality=1, verdict="keep"), _q(points=[]))
        self.assertEqual(verdict, "fix")

    def test_bad_numbers_marked_review(self):
        """模型吐了脏数据就交人工复核，不要猜。"""
        verdict, _ = _decide({"specific": "很好", "verdict": "keep"}, _q())
        self.assertEqual(verdict, "review")


class TestApply(unittest.TestCase):
    def test_dropped_removed_and_updates_applied(self):
        items = [_q(id="A"), _q(id="B"), _q(id="C", question="旧问题表述？")]
        result = {
            "stats": {},
            "drop_ids": {"A"},
            "updates": {
                "C": _q(id="C", question="新的更具体的问题表述是什么？", reviewed=True,
                        source="generated+ai_fixed"),
            },
        }
        out = apply_result(items, result)
        ids = [q["id"] for q in out]
        self.assertNotIn("A", ids)
        self.assertIn("C", ids)
        self.assertTrue(next(q for q in out if q["id"] == "C")["question"].startswith("新的"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

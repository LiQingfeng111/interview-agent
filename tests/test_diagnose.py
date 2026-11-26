"""短板分类测试。

核心断言：同一类短板，「不会」和「没意识到」的训练动作必须不同。
这是对标 llx9826/llm-interview-coach 的 diagnostic-output.md 学到的关键设计——
把"补知识"和"改表达结构"分开，否则反馈永远是"多练习"这种废话。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnose import (
    WEAKNESS_CATEGORIES,
    diagnose,
    render_diagnosis,
)


class TestDiagnoseLevels(unittest.TestCase):
    def test_evaded_maps_to_unaware(self):
        items = diagnose({"项目A": {"baseline": "回避（问2次）"}}, {})
        self.assertTrue(items)
        self.assertEqual(items[0]["category"], "指标意识")
        self.assertEqual(items[0]["level"], "没意识")

    def test_unknown_maps_to_dont_know(self):
        items = diagnose({"项目A": {"baseline": "未答出（问2次）"}}, {})
        self.assertEqual(items[0]["level"], "不会")

    def test_action_differs_by_level(self):
        unaware = diagnose({"项目A": {"baseline": "回避（问2次）"}}, {})[0]["action"]
        dontknow = diagnose({"项目A": {"baseline": "未答出（问2次）"}}, {})[0]["action"]
        self.assertNotEqual(unaware, dontknow, "两种情况的训练动作必须不同")

    def test_not_asked_is_not_a_weakness(self):
        items = diagnose({"项目A": {"目标": "未问到"}}, {})
        self.assertEqual(items, [], "没问过不能算短板")

    def test_answered_is_not_a_weakness(self):
        items = diagnose({"项目A": {"目标": "已答到", "指标": "已答到（顺带提及）"}}, {})
        self.assertEqual(items, [])


class TestDiagnoseMapping(unittest.TestCase):
    def test_element_to_category(self):
        cases = {
            "目标": "业务视角",
            "失败方案": "失败复盘",
            "个人贡献": "项目 ownership",
            "指标": "指标意识",
        }
        for element, category in cases.items():
            items = diagnose({"项目A": {element: "未答出（问2次）"}}, {})
            self.assertEqual(items[0]["category"], category, element)

    def test_low_dims_add_categories(self):
        items = diagnose({}, {"tradeoff": 2.3, "boundary": 1.8, "accuracy": 4.2})
        cats = {i["category"] for i in items}
        self.assertIn("tradeoff", cats)
        self.assertIn("系统视角", cats)
        self.assertNotIn("基础概念", cats, "accuracy 高于 3 不该报基础概念")

    def test_dedup_same_category(self):
        items = diagnose(
            {"项目A": {"baseline": "未答出（问2次）"}, "项目B": {"指标": "回避（问2次）"}},
            {},
        )
        cats = [i["category"] for i in items]
        self.assertEqual(len(cats), len(set(cats)), f"同类短板应合并: {cats}")

    def test_all_categories_from_fixed_set(self):
        items = diagnose(
            {"项目A": {e: "未答出（问2次）" for e in ("目标", "baseline", "指标", "失败方案", "个人贡献")}},
            {"accuracy": 1.5, "depth": 1.4, "tradeoff": 1.3, "boundary": 1.2, "clarity": 1.1},
        )
        for it in items:
            self.assertIn(it["category"], WEAKNESS_CATEGORIES, "不能自创标签")

    def test_render_is_readable(self):
        items = diagnose({"项目A": {"baseline": "未答出（问2次）"}}, {})
        text = render_diagnosis(items)
        self.assertIn("指标意识", text)
        self.assertIn("证据", text)
        self.assertIn("动作", text)

    def test_empty_render(self):
        self.assertIn("没有明显短板", render_diagnosis([]))


if __name__ == "__main__":
    unittest.main(verbosity=2)

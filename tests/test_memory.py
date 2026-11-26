"""事实账本测试：抽取准确性、陷阱过滤、矛盾检出、失效语义。

这一层的风险是"误报"：把"从 0.9 秒涨到 1.8 秒"判成自相矛盾，
用户会觉得这 AI 在找茬。所以测试分成两半：
  - 该抽的必须抽到（10 句里 ≥8 句）；
  - 不该抽的必须抽不到（时间/版本/对比句式 0 误报）。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import Memory, extract_facts


class TestExtraction(unittest.TestCase):
    GOOD = [
        "我们覆盖了 300 份课件",
        "一共 1000 多份文档",
        "召回率 71%",
        "召回率从 71% 提升到 82%",
        "延迟 0.6 秒",
        "响应时间从 0.9 秒涨到 1.8 秒",
        "首屏 4.2s 降到 1.6s",
        "支撑 20 个业务页面",
        "测试集 500 条",
        "准确率 92%",
    ]

    def test_extract_rate(self):
        hit = 0
        for s in self.GOOD:
            if extract_facts(s, "P", 1):
                hit += 1
        self.assertGreaterEqual(hit, 8, f"只抽到 {hit}/10 句")

    def test_trap_no_scale(self):
        """时间/版本类数字不能当规模。"""
        facts = extract_facts("我花了 3 个月，每天 2 小时，用的是 v2 版本", "P", 1)
        self.assertFalse([f for f in facts if f.key == "规模"], facts)

    def test_range_takes_last_value(self):
        """「从 71% 提升到 82%」应记 82%，不是两个值都记。"""
        facts = [f for f in extract_facts("召回率从 71% 提升到 82%", "P", 1)]
        pcts = [f.value for f in facts if f.unit == "%"]
        self.assertEqual(pcts, [82.0])

    def test_millisecond_normalized(self):
        facts = extract_facts("响应 200 毫秒", "P", 1)
        self.assertTrue(facts)
        self.assertAlmostEqual(facts[0].value, 0.2, places=3)


class TestContradiction(unittest.TestCase):
    def _mem(self):
        return Memory()

    def test_three_groups_detected(self):
        """3 组构造矛盾必须检出 3/3（提案 S3 的验收线）。"""
        m = self._mem()
        m.observe("覆盖了 300 份课件，召回率 71%，延迟 0.6 秒。", "问答助手", 1)
        m.observe("其实一共 1000 多份文档。", "问答助手", 3)
        m.observe("召回率能到 82%。", "问答助手", 5)
        m.observe("延迟大概 1.8 秒。", "问答助手", 7)
        keys = {c.key for c in m.contradictions}
        self.assertEqual(keys, {"规模", "召回率", "延迟"}, m.render_section())

    def test_same_value_not_contradiction(self):
        m = self._mem()
        m.observe("召回率 71%", "P", 1)
        m.observe("召回率还是 71%", "P", 2)
        self.assertEqual(m.contradictions, [])

    def test_cross_project_not_contradiction(self):
        m = self._mem()
        m.observe("召回率 71%", "项目A", 1)
        m.observe("召回率 82%", "项目B", 2)
        self.assertEqual(m.contradictions, [], "不同项目的数字不是矛盾")

    def test_same_turn_not_contradiction(self):
        """同一轮里前后两个值是描述变化，不判矛盾。"""
        m = self._mem()
        m.observe("延迟从 0.6 秒到 1.8 秒，召回率从 71% 到 82%", "P", 1)
        self.assertEqual(m.contradictions, [])

    def test_old_fact_invalidated_but_kept(self):
        m = self._mem()
        m.observe("规模 300 份", "P", 1)
        m.observe("规模 1000 份", "P", 2)
        old = [f for f in m.facts if f.key == "规模"][0]
        self.assertEqual(old.value, 300)
        self.assertFalse(old.active, "旧事实要标失效")
        self.assertTrue(old.superseded_by, "要记录被谁取代")

    def test_pending_and_mark_questioned(self):
        m = self._mem()
        m.observe("召回率 71%", "P", 1)
        m.observe("召回率 82%", "P", 2)
        c = m.pending_contradiction()
        self.assertIsNotNone(c)
        self.assertIn("哪个是准的", c.question())
        m.mark_questioned(c)
        self.assertIsNone(m.pending_contradiction())

    def test_duplicate_pair_deduped(self):
        """反复改口只记一次，报告别变噪音。"""
        m = self._mem()
        for i, v in enumerate([71, 82, 71, 82, 71]):
            m.observe(f"召回率 {v}%", "P", i + 1)
        self.assertLessEqual(len(m.contradictions), 2)

    def test_digest(self):
        m = self._mem()
        m.observe("召回率 71%", "P", 1)
        m.observe("召回率 82%", "P", 2)
        digest = m.prompt_digest()
        self.assertIn("召回率", digest)
        self.assertIn("矛盾", digest)
        self.assertLessEqual(len(digest), 300)


class TestReportSection(unittest.TestCase):
    def test_section_rendered(self):
        m = Memory()
        m.observe("召回率 71%", "P", 1)
        m.observe("召回率 82%", "P", 2)
        text = m.render_section()
        self.assertIn("【矛盾追问记录】", text)
        self.assertIn("哪个是准的", text)

    def test_section_empty_when_clean(self):
        self.assertEqual(Memory().render_section(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

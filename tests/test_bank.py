"""题库测试：结构、校验、选题策略、时效。

为什么这些必须测：
  题库是"看起来有、实际没用"最容易发生的地方。
  下面每条都对应一个真实失败模式：
    - 字段缺失 → 评分没有要点，退化成靠感觉
    - 全是 basic → 整场技术环节没有难度梯度
    - 只按话题轮转 → 连续问 5 个基础题（实测就是这样）
    - 没有时间戳 → 题库永远"看起来是新的"
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bank import (
    LEVELS,
    bank_stats,
    load_bank,
    pick_questions,
    render_bank_report,
    save_bank,
    validate_bank,
)


def make_q(id_, topic, level, role="agent_app"):
    return {
        "id": id_, "role": role, "topic": topic, "level": level,
        "question": f"{topic} 的 {level} 题？", "points": ["要点1", "要点2"],
        "source": "manual", "reviewed": True, "updated_at": time.strftime("%Y-%m-%d"),
    }


class TestValidate(unittest.TestCase):
    def test_missing_field(self):
        bad = {"id": "x", "role": "agent_app", "topic": "t", "level": "basic"}
        errors, _ = validate_bank([bad])
        self.assertTrue(any("question" in e for e in errors))

    def test_bad_level(self):
        q = make_q("a", "t", "extreme")
        errors, _ = validate_bank([q])
        self.assertTrue(any("level" in e for e in errors))

    def test_duplicate_id(self):
        q = make_q("dup", "t", "basic")
        errors, _ = validate_bank([q, dict(q)])
        self.assertTrue(any("重复" in e for e in errors))

    def test_missing_points_is_warning_not_error(self):
        q = make_q("a", "t", "basic")
        q["points"] = []
        errors, warnings = validate_bank([q])
        self.assertFalse(errors)
        self.assertTrue(any("要点" in w for w in warnings))


class TestPick(unittest.TestCase):
    def _bank(self):
        items = []
        for t in ("甲", "乙"):
            for lv in LEVELS:
                items.append(make_q(f"{t}-{lv}", t, lv))
        return items

    def test_rotates_both_level_and_topic(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.jsonl")
            save_bank(self._bank(), path)
            picked = pick_questions("agent_app", limit=6, path=path)
            levels = [q["level"] for q in picked]
            self.assertEqual(len(picked), 6)
            self.assertGreater(len(set(levels)), 1, f"难度没有轮转: {levels}")
            self.assertEqual(set(levels), set(LEVELS))

    def test_skips_asked(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.jsonl")
            save_bank(self._bank(), path)
            first = pick_questions("agent_app", limit=2, path=path)
            asked = {q["id"] for q in first}
            nxt = pick_questions("agent_app", limit=2, asked_ids=asked, path=path)
            self.assertFalse(asked & {q["id"] for q in nxt}, "已问过的题不该再出现")

    def test_empty_bank_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(pick_questions("agent_app", path=os.path.join(d, "none.jsonl")), [])


class TestStats(unittest.TestCase):
    def _write(self, d, items):
        path = os.path.join(d, "b.jsonl")
        save_bank(items, path)
        return path

    def test_stale_detection(self):
        with tempfile.TemporaryDirectory() as d:
            old = make_q("old", "t", "basic")
            old["updated_at"] = "2020-01-01"
            path = self._write(d, [old])
            s = bank_stats(load_bank(path))
            self.assertTrue(s["stale"], f"老题库应判过期, oldest={s['oldest_days']}")

    def test_fresh_not_stale(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._write(d, [make_q("new", "t", "basic")])
            self.assertFalse(bank_stats(load_bank(path))["stale"])

    def test_report_empty_bank(self):
        self.assertIn("题库为空", render_bank_report([]))


class TestRealBankIntegrity(unittest.TestCase):
    """对仓库里真实的题库做基本体检（防止有人手滑写坏）。"""

    def test_real_bank_valid(self):
        items = load_bank()
        if not items:
            self.skipTest("题库为空（未生成）")
        errors, _ = validate_bank(items)
        self.assertEqual(errors, [], f"题库有错: {errors[:3]}")

    def test_real_bank_has_all_levels(self):
        items = load_bank()
        if not items:
            self.skipTest("题库为空")
        levels = {q.get("level") for q in items}
        self.assertEqual(levels, set(LEVELS))


if __name__ == "__main__":
    unittest.main(verbosity=2)

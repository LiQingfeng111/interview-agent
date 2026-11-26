"""会话持久化测试：重启不丢、能接着答、旧分数不变。

为什么这三条都要测：
  1. 不丢 —— 这是加 SQLite 的唯一理由，丢了就等于没做；
  2. 能接着答 —— 恢复出来的会话必须是"活的"，不是只读快照；
  3. 旧分数不变 —— 恢复不能重放模型调用，否则模型漂移会把历史分数改掉，
     报告就成了"每次打开都不一样"的东西。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import persist
from llm import MockLLM
from resume import parse_resume_best
from session import InterviewSession

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESUME = os.path.join(ROOT, "data", "test_resumes", "our_sample.md")


class TestPersist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        self.projects = parse_resume_best(open(RESUME, encoding="utf-8").read())

    def _session(self, turns: int = 2):
        s = InterviewSession(llm=MockLLM(), projects=self.projects, role_key="ai_algo",
                             max_turns=10, verbose=False, save=False, seed=5, audit=True)
        s.start()
        for _ in range(turns):
            s.step("目标 300 份 PDF，召回 71%。")
        return s

    def test_save_and_load(self):
        s = self._session()
        sid = persist.save(s, self.db)
        self.assertTrue(sid)
        data = persist.load(sid, self.db)
        self.assertIsNotNone(data)
        self.assertEqual(len(data["turns"]), len(s.turns))
        self.assertEqual(data["role"], "ai_algo")

    def test_list_sessions(self):
        for _ in range(3):
            persist.save(self._session(), self.db)
        rows = persist.list_sessions(path=self.db)
        self.assertGreaterEqual(len(rows), 3)
        self.assertIn("avg_score", rows[0])

    def test_restore_keeps_history_and_verdicts(self):
        s = self._session(turns=3)
        sid = persist.save(s, self.db)
        original = [(t.question, t.answer,
                     t.evaluation.verdict if t.evaluation else "") for t in s.turns]

        restored = persist.restore(sid, MockLLM(), self.projects, path=self.db)
        self.assertIsNotNone(restored)
        got = [(t.question, t.answer,
                t.evaluation.verdict if t.evaluation else "") for t in restored.turns]
        self.assertEqual(got, original, "恢复后的历史必须逐字段一致（不重算分数）")

    def test_restore_can_continue(self):
        s = self._session(turns=2)
        sid = persist.save(s, self.db)
        restored = persist.restore(sid, MockLLM(), self.projects, path=self.db)
        before = len(restored.turns)
        restored.step("重排让延迟从 0.9 涨到 1.8 秒。")
        self.assertEqual(len(restored.turns), before + 1, "恢复后应能继续答题")

    def test_delete(self):
        s = self._session(turns=1)
        sid = persist.save(s, self.db)
        self.assertTrue(persist.delete(sid, self.db))
        self.assertIsNone(persist.load(sid, self.db))

    def test_missing_sid_returns_none(self):
        self.assertIsNone(persist.load("不存在的会话", self.db))

    def test_trend_aggregates_dims(self):
        """跨会话趋势：按用户聚合均分与维度，且只读库、不调模型。"""
        s = self._session(turns=1)
        for _ in range(3):
            persist.save(s, self.db, user="alice")
        persist.save(self._session(turns=1), self.db, user="bob")
        alice = persist.trend("alice", path=self.db)
        self.assertGreaterEqual(alice["count"], 1)
        self.assertTrue(alice["avg_series"], "应有均分走势")
        self.assertTrue(alice["dim_avg"], "维度均值不应为空（payload.meta.stats 必须存）")
        everybody = persist.trend("", path=self.db)
        self.assertGreaterEqual(everybody["count"], alice["count"])

    def test_finish_state_saved(self):
        s = self._session(turns=1)
        s.end_early()
        sid = persist.save(s, self.db)
        data = persist.load(sid, self.db)
        self.assertEqual(data["finish_reason"], "user_quit")
        self.assertEqual(data["phase"], "FEEDBACK")


if __name__ == "__main__":
    unittest.main(verbosity=2)

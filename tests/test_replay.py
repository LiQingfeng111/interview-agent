"""重放测试：cassette 命中、快照比对、事件流回读。

为什么值得测：报告里要写"重放一致 ✓"，这句话必须能被执行证明。
如果 cassette 命中率不是 100%，重放就会偷偷调模型——那"一致"是假的。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import E, Ledger, load_ledger
from replay import Cassette, ReplayLLM, diff_snapshots, fingerprint, replay


class TestCassette(unittest.TestCase):
    def test_record_and_hit(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.jsonl")
            c = Cassette(path)
            key = fingerprint("你好", "interviewer")
            c.record(key, "问题一")
            c2 = Cassette(path)
            self.assertEqual(c2.get(key), "问题一")
            self.assertIsNone(c2.get(fingerprint("别的", "interviewer")))

    def test_hit_rate(self):
        c = Cassette()
        k = fingerprint("a", "t")
        c.record(k, "x")
        c.get(k)
        c.get("missing")
        self.assertEqual(c.stats()["hits"], 1)
        self.assertEqual(c.stats()["misses"], 1)


class TestReplayLLM(unittest.TestCase):
    def test_hits_cassette_without_calling_inner(self):
        class Inner:
            is_mock = False
            calls = 0

            def chat(self, messages, **kw):
                Inner.calls += 1
                return "真调用"

        c = Cassette()
        key = fingerprint("提示词", "interviewer")
        c.record(key, "录像里的回答")
        llm = ReplayLLM(c)
        out = llm.chat([{"role": "user", "content": "提示词"}], task="interviewer")
        self.assertEqual(out, "录像里的回答")
        self.assertEqual(Inner.calls, 0)


class TestSnapshotDiff(unittest.TestCase):
    def test_ignore_timestamps(self):
        a = {"session_id": "x", "turns": 3, "ts": "1"}
        b = {"session_id": "y", "turns": 3, "ts": "2"}
        self.assertEqual(diff_snapshots(a, b), [])

    def test_detect_field_change(self):
        a = {"turns": 3, "evals": [{"verdict": "pass"}]}
        b = {"turns": 3, "evals": [{"verdict": "fail"}]}
        diffs = diff_snapshots(a, b)
        self.assertTrue(any("verdict" in d for d in diffs))

    def test_detect_length_change(self):
        self.assertTrue(diff_snapshots({"x": [1, 2]}, {"x": [1]}))


class TestRoundTrip(unittest.TestCase):
    def test_ledger_file_roundtrip_and_fold(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            ledger = Ledger(path=path, session_id="s1")
            ledger.append(E.TURN_ASKED, {"question": "q1"})
            ledger.append(E.TURN_ASKED, {"question": "q2"})
            ledger.append(E.FINISH, {"reason": "max_turns"})

            loaded = load_ledger(path)
            self.assertEqual(len(loaded.events), 3)
            self.assertEqual(loaded.fold(), ledger.fold())

            snap = replay(path)
            self.assertEqual(snap["turns"], 2)
            self.assertEqual(snap["finish_reason"], "max_turns")

    def test_replay_up_to_seq(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "events.jsonl")
            ledger = Ledger(path=path, session_id="s2")
            for i in range(4):
                ledger.append(E.TURN_ASKED, {"question": f"q{i}"})
            self.assertEqual(replay(path, up_to_seq=1)["turns"], 2)

    def test_actions_serializable(self):
        ledger = Ledger()
        ledger.append(E.TURN_ASKED, {"question": "q"})
        line = json.dumps(ledger.events[0].to_dict(), ensure_ascii=False)
        self.assertIn("turn.asked", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)

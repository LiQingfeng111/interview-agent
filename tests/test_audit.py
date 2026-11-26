"""审计层测试：账本 / 不变量 / 预算 / 闭环校验。

为什么要这么多测试：
  审计层的价值全部建立在"数字可信"上。如果账本丢事件、不变量误报、
  预算算错单位，那么报告上那些漂亮数字就是假的——**比没有更糟**。
  这一层已经亲手抓出过两个真 bug（要素计数失真、evidence 写死"未作答"），
  所以每条规则都要有能复现的 case。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from budget import Budget, estimate_tokens, price_of
from invariants import INVARIANTS, check_all, render
from ledger import E, EVENT_TYPES, Event, Ledger, _jsonable
from schemas import Project
from verifier import (FINISH_REASONS, coverage_ratio, evidence_audit,
                      pending_elements, should_finish)


class TestLedger(unittest.TestCase):
    def test_type_closed_set(self):
        ledger = Ledger()
        with self.assertRaises(ValueError):
            ledger.append("made.up.event", {})

    def test_append_and_seq(self):
        ledger = Ledger()
        for i in range(3):
            ledger.append(E.TURN_ASKED, {"i": i})
        self.assertEqual([e.seq for e in ledger.iter_events()], [0, 1, 2])
        self.assertEqual(len(ledger.of_type(E.TURN_ASKED)), 3)

    def test_fold_idempotent(self):
        ledger = Ledger()
        ledger.append(E.TURN_ASKED, {"question": "q1"})
        ledger.append(E.EVAL_RETURNED, {"verdict": "pass", "score": 4})
        ledger.append(E.PROBE_LEVEL_UP, {})
        self.assertEqual(ledger.fold(), ledger.fold())

    def test_fold_counts(self):
        ledger = Ledger()
        ledger.append(E.TURN_ASKED, {"question": "q"})
        ledger.append(E.EVAL_RETURNED, {"verdict": "pass", "score": 4})
        ledger.append(E.INVARIANT_VIOLATED, {"id": "INV-02", "severity": "fatal"})
        ledger.append(E.FINISH, {"reason": "max_turns"})
        snap = ledger.fold()
        self.assertEqual(snap["turns"], 1)
        self.assertEqual(snap["finish_reason"], "max_turns")
        self.assertEqual(ledger.fatal_count(), 1)

    def test_verify_chain_detects_gap(self):
        ledger = Ledger()
        for _ in range(3):
            ledger.append(E.TURN_ASKED, {})
        del ledger.events[1]
        ledger.events[1] = Event(**{**ledger.events[1].to_dict(), "seq": 2})
        problems = ledger.verify_chain()
        self.assertTrue(problems, "seq 断了必须报出来")

    def test_jsonable_survives_weird_payload(self):
        ledger = Ledger()
        ledger.append(E.TURN_ASKED, {
            "set": {"a", "b"},
            "proj": Project(name="X", stack=["Python"]),
            "err": ValueError("boom"),
        })
        self.assertTrue(ledger.events, "含 set/dataclass/异常也必须能记账")
        self.assertEqual(_jsonable({"a"}), ["a"])

    def test_event_types_declared(self):
        self.assertIn("turn.asked", EVENT_TYPES)
        self.assertIn("invariant.violated", EVENT_TYPES)


class TestInvariants(unittest.TestCase):
    def test_no_violation_on_clean_turn(self):
        ctx = {"question": "你为什么选 FAISS 而不是 BM25？", "plan_empty": True,
               "recent_questions": []}
        self.assertEqual(check_all(ctx, only="turn.asked"), [])

    def test_inv01_element_over_attempt(self):
        v = check_all({"element_attempts": 3, "project": "P", "element": "指标"})
        self.assertTrue(any(x.id == "INV-01" and x.severity == "fatal" for x in v))

    def test_inv02_repeat(self):
        q = "你说召回 82%，这个数字是怎么测出来的？"
        v = check_all({"question": q, "recent_questions": ["", "", q]})
        self.assertTrue(any(x.id == "INV-02" for x in v))

    def test_inv03_short_and_monologue(self):
        self.assertTrue(any(x.id == "INV-03" for x in check_all({"question": "然后呢"})))
        self.assertTrue(any(x.id == "INV-03"
                            for x in check_all({"question": "好的，让我想想再问你一个问题"})))

    def test_inv04_fake_evidence_is_fatal(self):
        v = check_all({"eval": {"evidence": "候选人说用了 BM25 倒排"},
                       "answer": "我用了 FAISS 做向量检索"})
        self.assertTrue(any(x.id == "INV-04" and x.severity == "fatal" for x in v))

    def test_inv04_real_evidence_passes(self):
        v = check_all({"eval": {"evidence": "我用了 FAISS"},
                       "answer": "我用了 FAISS 做向量检索，召回 82%"})
        self.assertFalse(any(x.id == "INV-04" for x in v))

    def test_inv05_verdict_score_mismatch(self):
        v = check_all({"eval": {"verdict": "pass", "score": 2}})
        self.assertTrue(any(x.id == "INV-05" for x in v))

    def test_inv07_reason_whitelist(self):
        self.assertTrue(any(x.id == "INV-07"
                            for x in check_all({"transition_reason": "随便换个阶段"})))
        self.assertFalse(any(x.id == "INV-07" for x in check_all({"transition_reason": "阶段完成"})))

    def test_inv08_budget_without_degrade(self):
        v = check_all({"budget_level": 2, "degraded_noted": False})
        self.assertTrue(any(x.id == "INV-08" and x.severity == "fatal" for x in v))

    def test_render(self):
        v = check_all({"question": "然后呢"})
        self.assertIn("warn", render(v))

    def test_all_eight_declared(self):
        self.assertEqual(len(INVARIANTS), 8)


class TestBudget(unittest.TestCase):
    def test_level_thresholds(self):
        b = Budget(tokens=1000, cost_cny=1.0)
        self.assertEqual(b.level(), 0)
        b.used_tokens = 800
        self.assertEqual(b.level(), 1)
        b.used_tokens = 1200
        self.assertEqual(b.level(), 2)

    def test_cost_unit_is_yuan_not_fen(self):
        """价格表是分/千 token，一场 10k token 应该是几角钱，不是一块多。"""
        b = Budget()
        b.spend(prompt="x" * 4000, completion="y" * 4000, model="deepseek-chat")
        self.assertLess(b.used_cost, 0.05, f"成本单位算错了：{b.used_cost}")

    def test_mock_costs_nothing(self):
        b = Budget()
        b.spend(prompt="一段中文" * 100, completion="回答" * 100, model="mock")
        self.assertEqual(b.used_cost, 0.0)
        self.assertGreater(b.used_tokens, 0)

    def test_unknown_model_estimated(self):
        pin, pout, est = price_of("some-unknown-model")
        self.assertTrue(est)

    def test_estimate_tokens(self):
        self.assertGreater(estimate_tokens("中文五个字"), 2)


class _T:
    def __init__(self, idx, answer, evidence):
        self.idx = idx
        self.answer = answer
        self.evaluation = type("E", (), {"evidence": evidence})()


class TestVerifier(unittest.TestCase):
    def test_evidence_audit_flags_fake(self):
        turns = [_T(1, "我用了 FAISS", "我用了 FAISS"),
                 _T(2, "不知道", "候选人说用了 BM25")]
        audit = evidence_audit(turns)
        self.assertEqual(audit["verified"], 1)
        self.assertEqual(audit["fake_count"], 1)

    def test_finish_reasons_closed(self):
        class SM:
            phase = "TECH_QA"
            total_turns = 5
            max_turns = 10
        done, reason = should_finish(SM())
        self.assertFalse(done)
        self.assertEqual(reason, "")

        class SM2(SM):
            total_turns = 10
        done, reason = should_finish(SM2())
        self.assertTrue(done)
        self.assertIn(reason, FINISH_REASONS)

    def test_budget_finish(self):
        class SM:
            phase = "TECH_QA"
            total_turns = 1
            max_turns = 10
        b = Budget(tokens=100)
        b.used_tokens = 200
        done, reason = should_finish(SM(), b)
        self.assertTrue(done)
        self.assertEqual(reason, "budget_exhausted")

    def test_coverage_and_pending(self):
        progress = {"P": {"目标": "已答到", "指标": "未问到", "失败方案": "未答出（问2次）"}}
        self.assertAlmostEqual(coverage_ratio(progress), 2 / 3, places=3)
        self.assertEqual(pending_elements(progress), ["指标"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

class TestChatJsonTracing(unittest.TestCase):
    """评分官/教练走的是 chat_json，必须和 chat 一样进账本。

    这条是给一个真实审计黑洞上的锁：曾经 chat_json 不记账本、不花预算、
    不录 cassette，于是"调了几次模型、花了多少钱"只统计了面试官那一半，
    而账本看起来完全正常。
    """

    def test_chat_json_logs_event(self):
        from llm import MockLLM
        from tracing import TracedLLM

        lg = Ledger(session_id="t_json")
        traced = TracedLLM(MockLLM(), lg, Budget())
        traced.chat_json([{"role": "user", "content": "打分"}], task="evaluator")
        calls = list(lg.of_type(E.LLM_CALL))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].op, "evaluator")

    def test_chat_and_chat_json_both_counted(self):
        from llm import MockLLM
        from tracing import TracedLLM

        lg = Ledger(session_id="t_both")
        traced = TracedLLM(MockLLM(), lg, Budget())
        traced.chat([{"role": "user", "content": "提问"}], task="interviewer")
        traced.chat_json([{"role": "user", "content": "打分"}], task="evaluator")
        self.assertEqual(len(list(lg.of_type(E.LLM_CALL))), 2)
        self.assertEqual(traced.calls, 2)
class TestVerdictAuthority(unittest.TestCase):
    """判定权必须在阈值，不在模型。

    这是本项目最贵的一课：之前模型给了合法 verdict 就直接采用，
    于是校准出来的阈值只在"模型没给 verdict"时才生效（实测 12 条里 10 条被覆盖），
    "尺子可校准"变成空话。
    """

    def test_threshold_wins_over_model(self):
        from schemas import normalize_eval
        d = {"accuracy": 4, "depth": 4, "tradeoff": 4, "boundary": 4,
             "clarity": 4, "verdict": "fail"}
        out = normalize_eval(d, "real")
        self.assertEqual(out["verdict"], "pass")
        self.assertTrue(out["verdict_conflict"])
        self.assertEqual(out["model_verdict"], "fail")

    def test_low_score_cannot_be_passed_by_model(self):
        from schemas import normalize_eval
        d = {"accuracy": 1, "depth": 1, "tradeoff": 1, "boundary": 1,
             "clarity": 1, "verdict": "pass"}
        out = normalize_eval(d, "real")
        self.assertEqual(out["verdict"], "fail")
        self.assertTrue(out["verdict_conflict"])

    def test_conflict_raises_invariant(self):
        import invariants as inv
        ctx = {"eval": {"verdict": "pass", "score": 4.0},
               "verdict_conflict": True, "model_verdict": "fail"}
        ids = [v.id for v in inv.check_all(ctx)]
        self.assertIn("INV-05", ids)

    def test_inv05_follows_config_thresholds(self):
        import invariants as inv
        from config import VERDICT_THRESHOLDS
        pass_min, _ = VERDICT_THRESHOLDS["real"]
        # 刚好低于合格线 → 必须报
        ctx = {"eval": {"verdict": "pass", "score": pass_min - 0.1}}
        self.assertIn("INV-05", [v.id for v in inv.check_all(ctx)])
        # 刚好达到 → 不报
        ctx2 = {"eval": {"verdict": "pass", "score": pass_min}}
        self.assertNotIn("INV-05", [v.id for v in inv.check_all(ctx2)])
"""OTel 导出测试：结构能被看板吃、且不丢事件。

为什么要测结构而不是"跑通"：
  导出格式错了不会有异常——它会安静地导出一个看板看不懂的 JSON，
  等你接上 Langfuse 才发现"没数据"。所以这里断言 OTLP 的必备字段齐、span 数和事件数对得上。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ledger import E, Ledger
from otel_export import build_langfuse, build_otlp


def _ledger() -> Ledger:
    lg = Ledger(session_id="aud_test")
    lg.append(E.TURN_ASKED, {"question": "为什么要检索？", "turn": 1, "level": 1,
                             "project": "P", "element": "目标"})
    lg.append(E.LLM_CALL, {"chars": 20}, request_id="r1", op="interviewer",
              model="deepseek-chat", tokens_in=100, tokens_out=50, cost_cny=0.01, latency_ms=800)
    lg.append(E.EVAL_RETURNED, {"verdict": "pass", "score": 4})
    lg.append(E.INVARIANT_VIOLATED, {"id": "INV-02", "severity": "fatal", "desc": "复读"})
    lg.append(E.FINISH, {"reason": "max_turns"})
    return lg


class TestOtlp(unittest.TestCase):
    def test_structure(self):
        otlp = build_otlp(_ledger())
        self.assertIn("resourceSpans", otlp)
        rs = otlp["resourceSpans"][0]
        self.assertIn("resource", rs)
        spans = rs["scopeSpans"][0]["spans"]
        root = spans[0]
        self.assertEqual(root["parentSpanId"], "")
        for key in ("traceId", "spanId", "name", "startTimeUnixNano",
                    "endTimeUnixNano", "attributes", "status"):
            self.assertIn(key, root)

    def test_trace_and_span_ids_shape(self):
        otlp = build_otlp(_ledger())
        spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual(len({s["traceId"] for s in spans}), 1)
        self.assertEqual(len(spans[0]["traceId"]), 32)
        self.assertEqual(len(spans[0]["spanId"]), 16)

    def test_counts(self):
        otlp = build_otlp(_ledger())
        spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
        # 1 根 + 1 轮次 + 1 LLM + 1 违规
        self.assertEqual(len(spans), 4)

    def test_fatal_marked_error(self):
        otlp = build_otlp(_ledger())
        spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
        inv = [s for s in spans if s["name"] == "invariant.violated"][0]
        self.assertEqual(inv["status"]["code"], 2, "fatal 必须标红，否则看板上看不见")

    def test_genai_attributes(self):
        otlp = build_otlp(_ledger())
        spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
        llm = [s for s in spans if s["name"].startswith("gen_ai.chat")][0]
        keys = {a["key"] for a in llm["attributes"]}
        self.assertIn("gen_ai.request.model", keys)
        self.assertIn("gen_ai.usage.input_tokens", keys)

    def test_langfuse_rows(self):
        rows = build_langfuse(_ledger())
        self.assertEqual(len(rows), 5)
        self.assertIn("timestamp", rows[0])
        self.assertIn("payload", rows[0]["metadata"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

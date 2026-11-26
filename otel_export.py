"""把事件账本导出成 OTel / Langfuse 能直接吃的格式。

为什么要这一步：
  账本现在是本地的 jsonl，可视化得靠 `render_report`。想接看板（Langfuse/Phoenix）
  就得输出标准 trace。既然零依赖不再当硬约束，这里**输出 OTLP JSON**，
  不装 SDK——写文件比装包稳，而且文件可用于离线比对与归档。

字段对齐 OTel GenAI semconv（`gen_ai.*`），所以 Langfuse / Phoenix / Tempo
都能直接映射；另外给一份 Langfuse 的 NDJSON（它自己的 ingestion 格式）。

用法：
  python -m otel_export runs/events_xxx.jsonl -o runs/trace.json
  python run.py --demo --audit --otel      # 面试结束自动导出
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ledger import E, load_ledger  # noqa: E402

SCOPE = {"name": "interview_agent", "version": "1.0"}


def _trace_id(session_id: str) -> str:
    return hashlib.sha1(session_id.encode()).hexdigest()[:32]


def _span_id(seed: str) -> str:
    return hashlib.sha1(seed.encode()).hexdigest()[:16]


def _ts_nano(ts: str) -> int:
    """把 "YYYY-mm-dd HH:MM:SS" 转成纳秒时间戳（解析失败就用当前时间）。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(ts, fmt)) * 1e9)
        except Exception:
            continue
    return int(time.time() * 1e9)


def _attr(key: str, value) -> dict:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"key": key, "value": {"doubleValue": float(value)}}
    return {"key": key, "value": {"stringValue": str(value)[:400]}}


def build_otlp(ledger, service: str = "interview_agent") -> dict:
    """账本 → OTLP JSON（一次会话 = 一条 trace，轮次 = 子 span）。"""
    tid = _trace_id(ledger.session_id)
    spans: list[dict] = []
    root_id = _span_id(f"{ledger.session_id}:root")
    root_start = _ts_nano(ledger.events[0].ts) if ledger.events else int(time.time() * 1e9)

    turn_span: dict[str, str] = {}      # 轮次 → span id
    for ev in ledger.events:
        ts = _ts_nano(ev.ts)
        if ev.type == E.TURN_ASKED:
            sid = _span_id(f"{ledger.session_id}:{ev.seq}")
            turn_span[str(ev.payload.get("turn", ev.seq))] = sid
            spans.append({
                "traceId": tid, "spanId": sid, "parentSpanId": root_id,
                "name": f"interview.turn {ev.payload.get('phase', '')}".strip(),
                "kind": 1,
                "startTimeUnixNano": ts, "endTimeUnixNano": ts,
                "attributes": [
                    _attr("gen_ai.operation.name", "interview.turn"),
                    _attr("interview.project", ev.payload.get("project", "")),
                    _attr("interview.level", ev.payload.get("level", 0)),
                    _attr("interview.element", ev.payload.get("element", "")),
                    _attr("interview.question", ev.payload.get("question", "")),
                ],
                "status": {"code": 0},
            })
        elif ev.type == E.LLM_CALL:
            sid = _span_id(f"{ledger.session_id}:llm:{ev.seq}")
            spans.append({
                "traceId": tid, "spanId": sid, "parentSpanId": root_id,
                "name": f"gen_ai.chat {ev.op or 'llm'}",
                "kind": 3,       # CLIENT
                "startTimeUnixNano": ts - int(ev.latency_ms * 1e6),
                "endTimeUnixNano": ts,
                "attributes": [
                    _attr("gen_ai.system", "openai_compatible"),
                    _attr("gen_ai.request.model", ev.model or "unknown"),
                    _attr("gen_ai.usage.input_tokens", ev.tokens_in),
                    _attr("gen_ai.usage.output_tokens", ev.tokens_out),
                    _attr("cost.cny", ev.cost_cny),
                    _attr("request_id", ev.request_id),
                    _attr("task", ev.op or ""),
                ],
                "status": {"code": 0 if ev.ok else 2},
            })
        elif ev.type == E.INVARIANT_VIOLATED:
            sid = _span_id(f"{ledger.session_id}:inv:{ev.seq}")
            spans.append({
                "traceId": tid, "spanId": sid, "parentSpanId": root_id,
                "name": "invariant.violated",
                "kind": 1,
                "startTimeUnixNano": ts, "endTimeUnixNano": ts,
                "attributes": [
                    _attr("invariant.id", ev.payload.get("id", "")),
                    _attr("invariant.severity", ev.payload.get("severity", "")),
                    _attr("invariant.desc", ev.payload.get("desc", "")),
                ],
                # fatal 标成错误，看板上会红
                "status": {"code": 2 if ev.payload.get("severity") == "fatal" else 0},
            })

    snap = ledger.fold()
    spans.insert(0, {
        "traceId": tid, "spanId": root_id, "parentSpanId": "",
        "name": f"interview.session {ledger.session_id}",
        "kind": 1,
        "startTimeUnixNano": root_start,
        "endTimeUnixNano": _ts_nano(ledger.events[-1].ts) if ledger.events else root_start,
        "attributes": [
            _attr("session.id", ledger.session_id),
            _attr("interview.turns", snap["turns"]),
            _attr("interview.finish_reason", snap["finish_reason"] or ""),
            _attr("interview.fatal", ledger.fatal_count()),
            _attr("interview.warn", ledger.warn_count()),
            _attr("gen_ai.usage.total_tokens", snap["tokens"]),
            _attr("cost.cny", snap["cost_cny"]),
        ],
        "status": {"code": 0},
    })

    return {
        "resourceSpans": [{
            "resource": {"attributes": [_attr("service.name", service)]},
            "scopeSpans": [{"scope": SCOPE, "spans": spans}],
        }]
    }


def build_langfuse(ledger) -> list[dict]:
    """Langfuse ingestion 用的扁平行（便于直接 POST /api/public/ingestion）。"""
    tid = _trace_id(ledger.session_id)
    rows = []
    for ev in ledger.events:
        rows.append({
            "id": f"{tid}-{ev.seq}",
            "traceId": tid,
            "name": ev.type,
            "timestamp": ev.ts,
            "metadata": {
                "seq": ev.seq, "op": ev.op, "model": ev.model,
                "tokens_in": ev.tokens_in, "tokens_out": ev.tokens_out,
                "cost_cny": ev.cost_cny, "latency_ms": ev.latency_ms,
                "ok": ev.ok, "payload": ev.payload,
            },
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ledger", help="事件流 jsonl 路径")
    ap.add_argument("-o", "--out", default="", help="输出文件（默认 runs/trace_<sid>.json）")
    ap.add_argument("--langfuse", default="", help="额外输出 Langfuse NDJSON")
    args = ap.parse_args()

    ledger = load_ledger(args.ledger)
    otlp = build_otlp(ledger)
    out = args.out or os.path.join(
        os.path.dirname(args.ledger), f"trace_{ledger.session_id}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(otlp, f, ensure_ascii=False, indent=2)

    spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
    print(f"[otel] 已导出 {len(spans)} 个 span → {out}")
    print(f"[otel] 会话 {ledger.session_id} · fatal {ledger.fatal_count()} · "
          f"warn {ledger.warn_count()}")

    if args.langfuse:
        rows = build_langfuse(ledger)
        with open(args.langfuse, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"[otel] Langfuse NDJSON {len(rows)} 行 → {args.langfuse}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

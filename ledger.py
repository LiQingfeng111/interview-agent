"""事件账本：append-only 事件流 + 折叠快照。

为什么要有这一层（面试怎么讲）：
  现在一场面试只留一份最终 JSON，"第 7 轮为什么换项目"根本查不到。
  改成事件流之后，任何一轮的问题、回答、评分、不变量违规、升降层都留痕，
  于是"可重放、可审计、可统计"才有落脚点。

设计取舍：
  - 事件类型是**闭集**（E 里列全），新增必须改这里，防止事件类型膨胀成垃圾桶；
  - `fold()` 幂等：同一事件流折叠两次结果相同，这是重放能对齐的前提；
  - 字段名向 OTel GenAI semconv 靠（model/tokens/op），但**不装 SDK**，
    保持零依赖——将来要接看板，把这几列直接映射成 span 就行。
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from typing import Any, Iterator


class E:
    """事件类型闭集。加新类型前先问：它会不会只是另一个类型的字段？"""
    RESUME_PARSED = "resume.parsed"
    PLAN_BUILT = "plan.built"
    PLAN_VALIDATED = "plan.validated"
    TURN_ASKED = "turn.asked"
    TURN_ANSWERED = "turn.answered"
    EVAL_RETURNED = "eval.returned"
    EVAL_DEGRADED = "eval.degraded"
    PROBE_LEVEL_UP = "probe.level_up"
    PROBE_SWITCH = "probe.switch"
    STATE_TRANSITION = "state.transition"
    MEMORY_FACT = "memory.fact"
    MEMORY_CONTRADICTION = "memory.contradiction"
    INVARIANT_VIOLATED = "invariant.violated"
    BUDGET_EXCEEDED = "budget.exceeded"
    FINISH = "finish"
    LLM_CALL = "llm.call"


EVENT_TYPES = frozenset(
    v for k, v in vars(E).items()
    if not k.startswith("_") and isinstance(v, str)
)


def _jsonable(obj: Any) -> Any:
    """把任意对象变成可 JSON 序列化的结构。

    为什么必须有：事件里会混进 dataclass（Turn/Evaluation）、set（covered 要素）、
    甚至 Exception（降级原因）。不兜底就会在"最需要日志的那一刻"抛 TypeError——
    这个坑在 llm.py 的日志模块里已经踩过一次了。
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_jsonable(x) for x in obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if dataclasses.is_dataclass(obj):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, BaseException):
        return f"{type(obj).__name__}: {obj}"
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        try:
            return _jsonable(obj.to_dict())
        except Exception:
            pass
    return repr(obj)


@dataclasses.dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    session_id: str
    type: str
    payload: dict
    causation_id: str = ""
    request_id: str = ""
    op: str = ""
    model: str = ""
    prompt_version: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_cny: float = 0.0
    latency_ms: int = 0
    ok: bool = True

    def to_dict(self) -> dict:
        return _jsonable(dataclasses.asdict(self))

    @staticmethod
    def from_dict(d: dict) -> "Event":
        known = {f.name for f in dataclasses.fields(Event)}
        return Event(**{k: v for k, v in d.items() if k in known})


class Ledger:
    """事件账本。落盘失败自动降级为内存模式，绝不因为日志问题中断面试。"""

    def __init__(self, path: str = "", session_id: str = ""):
        self.session_id = session_id or f"s_{int(time.time() * 1000)}"
        self.path = path
        self.events: list[Event] = []
        self.degraded = False
        self.error = ""

    # ---------------------------------------------------------- 写
    def append(self, type_: str, payload: dict | None = None, **meta) -> Event:
        if type_ not in EVENT_TYPES:
            raise ValueError(f"未知事件类型：{type_}（先加到 E 里）")
        ev = Event(
            seq=len(self.events),
            ts=time.strftime("%Y-%m-%d %H:%M:%S"),
            session_id=self.session_id,
            type=type_,
            payload=_jsonable(payload or {}),
            **{k: _jsonable(v) for k, v in meta.items() if k in
               {"causation_id", "request_id", "op", "model", "prompt_version",
                "tokens_in", "tokens_out", "cost_cny", "latency_ms", "ok"}},
        )
        self.events.append(ev)
        self._flush(ev)
        return ev

    def _flush(self, ev: Event) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:                 # 日志坏了不能让面试停
            self.degraded = True
            self.error = f"{type(e).__name__}: {e}"

    # ---------------------------------------------------------- 读
    def iter_events(self, since_seq: int = 0) -> Iterator[Event]:
        for ev in self.events:
            if ev.seq >= since_seq:
                yield ev

    def of_type(self, type_: str) -> list[Event]:
        return [e for e in self.events if e.type == type_]

    def verify_chain(self) -> list[str]:
        """链完整性：seq 必须从 0 连续，否则说明丢事件/重复写。"""
        problems: list[str] = []
        for i, ev in enumerate(self.events):
            if ev.seq != i:
                problems.append(f"seq 不连续：第 {i} 条记录的 seq={ev.seq}")
                break
        ids = {e.request_id for e in self.events if e.request_id}
        for ev in self.events:
            if ev.causation_id and ev.causation_id not in ids:
                problems.append(f"seq={ev.seq} 的 causation_id={ev.causation_id} 找不到来源")
        return problems

    # ---------------------------------------------------------- 折叠
    def fold(self) -> dict:
        """事件流 → 会话快照。幂等：同样的流折两次结果必须相同。"""
        snap = {
            "session_id": self.session_id,
            "turns": 0,
            "questions": [],
            "evals": [],
            "level_ups": 0,
            "switches": 0,
            "violations": [],
            "facts": [],
            "contradictions": [],
            "tokens": 0,
            "cost_cny": 0.0,
            "finish_reason": "",
            "phases": [],
        }
        for ev in self.events:
            p = ev.payload
            if ev.type == E.TURN_ASKED:
                snap["turns"] += 1
                snap["questions"].append(p.get("question", ""))
            elif ev.type == E.EVAL_RETURNED:
                snap["evals"].append({
                    "verdict": p.get("verdict", ""),
                    "score": p.get("score"),
                    "evidence": p.get("evidence", ""),
                })
            elif ev.type == E.PROBE_LEVEL_UP:
                snap["level_ups"] += 1
            elif ev.type == E.PROBE_SWITCH:
                snap["switches"] += 1
            elif ev.type == E.INVARIANT_VIOLATED:
                snap["violations"].append({"id": p.get("id"), "severity": p.get("severity")})
            elif ev.type == E.MEMORY_FACT:
                snap["facts"].append(p)
            elif ev.type == E.MEMORY_CONTRADICTION:
                snap["contradictions"].append(p)
            elif ev.type == E.STATE_TRANSITION:
                snap["phases"].append(p.get("to", ""))
            elif ev.type == E.FINISH:
                snap["finish_reason"] = p.get("reason", "")
            snap["tokens"] += int(ev.tokens_in or 0) + int(ev.tokens_out or 0)
            snap["cost_cny"] = round(snap["cost_cny"] + float(ev.cost_cny or 0), 6)
        return snap

    def fatal_count(self) -> int:
        return sum(1 for e in self.events
                   if e.type == E.INVARIANT_VIOLATED and e.payload.get("severity") == "fatal")

    def warn_count(self) -> int:
        return sum(1 for e in self.events
                   if e.type == E.INVARIANT_VIOLATED and e.payload.get("severity") == "warn")

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "events": len(self.events),
            "fatal": self.fatal_count(),
            "warn": self.warn_count(),
            "degraded": self.degraded,
            "chain_problems": self.verify_chain(),
        }


def load_ledger(path: str) -> Ledger:
    """从 JSONL 恢复账本（重放用）。"""
    ledger = Ledger(path="", session_id="")
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = Event.from_dict(json.loads(line))
            except Exception:
                continue
            # 会话号以事件里记的为准：Ledger 构造时会自动生成一个，
            # 不能靠"为空才赋值"，否则重放出来的快照 session_id 永远对不上。
            if ev.session_id:
                ledger.session_id = ev.session_id
            ledger.events.append(ev)
    return ledger


def looks_like_ledger(path: str) -> bool:
    """粗判一个文件是不是事件流（有 type/seq 字段）。"""
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline()
        d = json.loads(first)
        return "seq" in d and "type" in d and bool(re.match(r"^[a-z_.]+$", str(d.get("type", ""))))
    except Exception:
        return False

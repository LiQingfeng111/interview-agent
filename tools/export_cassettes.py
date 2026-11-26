"""导出 parity 用例（cassette）：把 Python 版纯逻辑的输入/输出冻结成 JSON。

用途：Java 版重放同一批输入，逐字段比对，证明"换语言没改行为"。
这是迁移的第一道闸门——对不上就停下修，不继续搬。

不含任何模型调用，全部是确定性逻辑，所以可以逐字节对齐：
  · 阶段预算分配（allocate_phase_budget）
  · 评分规整（normalize_eval：阈值判定 / 裁剪 / 要素归一）
  · 不变量（8 条规则）
  · 状态机（阶段推进 + 木梯升降）
  · 账本（fold 快照 / fatal 计数）

用法：
    python tools/export_cassettes.py <输出路径.json>
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from config import allocate_phase_budget  # noqa: E402
from invariants import check_all  # noqa: E402
from ledger import E, Ledger  # noqa: E402
from schemas import normalize_eval  # noqa: E402
from state_machine import InterviewStateMachine  # noqa: E402


def budgets() -> dict:
    out = {}
    for total in (24, 10, 17, 3, 48):
        plan = allocate_phase_budget(total)
        out[str(total)] = {ph: int(cfg["max_turns"]) for ph, cfg in plan.items()}
    return out


def evals() -> list:
    """覆盖：模型判定被阈值覆盖、脏分数裁剪、要素别名、长文本截断、weakest 兜底。"""
    samples = [
        {"accuracy": 3, "depth": 2, "tradeoff": 2, "boundary": 2, "clarity": 3,
         "verdict": "pass", "evidence": "71%", "covered": ["指标"]},
        {"accuracy": 0, "depth": 9, "tradeoff": "不是数字", "boundary": 4.5, "clarity": 3,
         "verdict": "PASSED", "evidence": "字" * 100, "followup_hint": "问" * 100},
        {"accuracy": 5, "depth": 5, "tradeoff": 5, "boundary": 5, "clarity": 5,
         "verdict": "fail", "weakest": "不存在的维度",
         "covered": ["基线", "衡量指标", "踩坑", "贡献", "乱写的"]},
        {"accuracy": 4, "depth": 3, "tradeoff": 3, "boundary": 2, "clarity": 4,
         "verdict": "", "evidence": "召回从 71% 到 82%"},
        {},
    ]
    out = []
    for s in samples:
        n = normalize_eval(dict(s), "real")
        out.append({
            "in": s,
            "out": {
                "dims": {d: n[d] for d in ("accuracy", "depth", "tradeoff", "boundary", "clarity")},
                "weakest": n["weakest"],
                "verdict": n["verdict"],
                "model_verdict": n["model_verdict"],
                "verdict_conflict": n["verdict_conflict"],
                "evidence": n["evidence"],
                "followup_hint": n["followup_hint"],
                "covered": n["covered"],
            },
        })
    return out


def invariants() -> list:
    cases = [
        {"when": "turn.asked",
         "ctx": {"element_attempts": 3, "project": "项目A", "element": "指标"}},
        {"when": "turn.asked",
         "ctx": {"question": "你说的 82% 是怎么测出来的？",
                 "recent_questions": ["你说的 82% 是怎么测出来的"]}},
        {"when": "turn.asked", "ctx": {"question": "嗯？"}},
        {"when": "turn.asked", "ctx": {"question": "好的，让我想想该问你什么"}},
        {"when": "eval.returned",
         "ctx": {"eval": {"evidence": "候选人说用了 BM25"},
                 "answer": "我用的是向量检索加规则过滤"}},
        {"when": "eval.returned", "ctx": {"eval": {}, "answer": "任意回答"}},
        {"when": "eval.returned",
         "ctx": {"eval": {"verdict": "pass", "score": 2.0, "evidence": "71%"},
                 "answer": "基线 71%"}},
        {"when": "eval.returned",
         "ctx": {"eval": {"verdict": "pass", "score": 3.5, "evidence": "71%"},
                 "verdict_conflict": True, "model_verdict": "fail", "answer": "基线 71%"}},
        {"when": "turn.asked", "ctx": {"element": "指标"}},
        {"when": "state.transition", "ctx": {"transition_reason": "随便跳一下"}},
        {"when": "state.transition", "ctx": {"transition_reason": "阶段完成"}},
        {"when": "budget", "ctx": {"budget_level": 1}},
        {"when": "budget", "ctx": {"budget_level": 1, "degraded_noted": True}},
        {"when": "",
         "ctx": {"question": "你说的 71% 基线是在多少条标注样本上测的？",
                 "recent_questions": ["上一问完全不同的话题内容"],
                 "element": "baseline", "in_plan": True,
                 "eval": {"verdict": "partial", "score": 2.2, "evidence": "71%"},
                 "answer": "基线 71%，是我在 200 条标注上测的。"}},
    ]
    out = []
    for c in cases:
        vs = check_all(c["ctx"], c["when"])
        out.append({
            "when": c["when"],
            "ctx": c["ctx"],
            "violations": sorted([{"id": v.id, "severity": v.severity} for v in vs],
                                 key=lambda x: (x["id"], x["severity"])),
        })
    return out


def state_machine() -> dict:
    """固定脚本：记录每一步之后的相位/轮次/层级，用于逐字段比对。"""
    sm = InterviewStateMachine(["项目A", "项目B"], ["主题1"], max_turns=10)
    steps = []

    def snap(op: str) -> dict:
        return {
            "op": op,
            "phase": sm.phase,
            "total_turns": sm.total_turns,
            "turn_in_phase": sm.turn_in_phase,
            "ladder_level": sm.probe.ladder_level,
            "strikes": sm.probe.strikes,
            "current": sm.probe.current,
            "exhausted": sm.probe.exhausted,
        }

    ops = [
        ("record", ("开场问题", "回答", "")),
        ("maybe_advance", ("阶段完成",)),
        ("record", ("自我介绍1", "回答", "")),
        ("record", ("自我介绍2", "回答", "")),
        ("maybe_advance", ("阶段完成",)),
        ("note_eval", ("pass", "指标", "")),
        ("note_eval", ("partial", "指标", "")),
        ("note_eval", ("partial", "指标", "")),
        ("note_eval", ("fail", "指标", "")),
        ("note_eval", ("fail", "指标", "")),
        ("note_eval", ("pass", "指标", "")),
    ]
    for name, args in ops:
        if name == "record":
            sm.record(*args)
        elif name == "maybe_advance":
            sm.maybe_advance(*args)
        elif name == "note_eval":
            sm.note_eval(*args)
        steps.append(snap(name))
    return {"steps": steps, "transitions": [[str(x[0]), x[1], x[2], x[3]] for x in sm.transitions]}


def ledger() -> dict:
    led = Ledger(session_id="s_parity")
    events = [
        (E.TURN_ASKED, {"question": "指标怎么测的？"}),
        (E.EVAL_RETURNED, {"verdict": "partial", "score": 2.4, "evidence": "71%"}),
        (E.PROBE_LEVEL_UP, {}),
        (E.STATE_TRANSITION, {"to": "TECH_QA"}),
        (E.INVARIANT_VIOLATED, {"id": "INV-02", "severity": "fatal"}),
        (E.INVARIANT_VIOLATED, {"id": "INV-05", "severity": "warn"}),
        (E.FINISH, {"reason": "max_turns"}),
    ]
    for t, p in events:
        led.append(t, p)
    led.append(E.LLM_CALL, {}, request_id="req1", op="chat", model="m",
               tokens_in=100, tokens_out=50, cost_cny=0.0012345, latency_ms=800)
    led.append(E.LLM_CALL, {}, request_id="req2", op="chat", model="m",
               tokens_in=200, tokens_out=80, cost_cny=0.002, latency_ms=900)
    snap = led.fold()
    return {
        "session_id": snap["session_id"],
        "turns": snap["turns"],
        "questions": snap["questions"],
        "evals": snap["evals"],
        "level_ups": snap["level_ups"],
        "switches": snap["switches"],
        "violations": snap["violations"],
        "tokens": snap["tokens"],
        "cost_cny": snap["cost_cny"],
        "finish_reason": snap["finish_reason"],
        "phases": snap["phases"],
        "fatal": led.fatal_count(),
        "warn": led.warn_count(),
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python tools/export_cassettes.py <输出路径.json>")
        return 2
    data = {
        "version": 1,
        "budgets": budgets(),
        "evals": evals(),
        "invariants": invariants(),
        "state_machine": state_machine(),
        "ledger": ledger(),
    }
    out = sys.argv[1]
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"写出 {out}")
    print(f"  预算用例 {len(data['budgets'])} 组 · 评分 {len(data['evals'])} 组 · "
          f"不变量 {len(data['invariants'])} 组 · 状态机 {len(data['state_machine']['steps'])} 步")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

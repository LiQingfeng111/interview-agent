"""反问环节不参与技术评分。

## 为什么需要这个测试（端到端实跑发现的缺陷）

E2E 跑完 12 轮面试，第 9 轮是反问环节：

    问：你有哪些想问我的？
    答：切块 500 字是 300/500/800 三档实测选的，召回 71%→82%…
    评分：fail 1.9 分

**这段话作为"技术回答"确实答非所问，但反问环节本来就不该用技术维度评判。**

## 危害（为什么必须修）

1. **误拉低总分**：反问环节必扣分，所有候选人的均分都被压低
2. **报告建议无意义**：`最需要复盘的` 可能指向反问轮，
   建议变成"你要更认真回答问题" —— 但那一轮本来就不该回答技术问题
3. **weakest 维度失真**：指向 clarity/depth，但这些维度对反问不适用

## 修复后的语义

    verdict = "n/a"        ← 明确不是 pass/partial/fail
    scored  = False        ← 下游统计跳过
    avg     = 0.0

报告里仍然展示这一轮（不能凭空消失），但标题写"反问环节，不计分"。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.service import _report_from_turns


# ============================================================================
# 报告聚合：不把 n/a 计入统计
# ============================================================================

def _turn(seq, verdict, score, phase="TECH_QA", q="问题", a="回答"):
    return {
        "question": q,
        "answer": a,
        "verdict": verdict,
        "score": score,
        "phase": phase,
        "element": "",
        "dims": {"accuracy": score, "depth": score, "tradeoff": score,
                 "boundary": score, "clarity": score} if score else {},
    }


def test_candidate_ask_not_counted_in_verdict_distribution():
    """反问轮（n/a）不该出现在结论分布里。"""
    turns = [
        _turn(1, "pass", 3.5),
        _turn(2, "partial", 2.5),
        _turn(3, "n/a", 0.0, phase="CANDIDATE_ASK", q="你有哪些想问我的？"),
    ]
    r = _report_from_turns(turns)

    vc = r["verdict_counts"]
    assert "n/a" not in vc, f"反问轮不该计入结论分布：{vc}"
    assert vc.get("pass") == 1
    assert vc.get("partial") == 1


def test_candidate_ask_not_dragging_average():
    """反问轮不该拉低均分。

    这是最关键的断言：修复前 avg = (3.5+2.5+0)/3 = 2.0，
    修复后应该是 (3.5+2.5)/2 = 3.0。
    """
    turns = [
        _turn(1, "pass", 3.5),
        _turn(2, "partial", 2.5),
        _turn(3, "n/a", 0.0, phase="CANDIDATE_ASK"),
    ]
    r = _report_from_turns(turns)

    assert r["avg"] == 3.0, (
        f"均分应为 3.0（排除反问轮），实际 {r['avg']}。"
        f"若为 2.0 说明反问轮被计入了。"
    )


def test_candidate_ask_still_shown_in_markdown():
    """反问轮要在报告里展示，不能凭空消失。"""
    turns = [
        _turn(1, "pass", 3.5),
        _turn(2, "n/a", 0.0, phase="CANDIDATE_ASK",
              q="你有哪些想问我的？", a="我想了解团队的技术栈"),
    ]
    r = _report_from_turns(turns)
    md = r["markdown"]

    assert "你有哪些想问我的？" in md, "反问轮的问题要展示"
    assert "我想了解团队的技术栈" in md, "反问轮的回答要展示"
    assert "不计分" in md, "要明确标注不计分，否则读者以为漏了分数"


def test_candidate_ask_not_in_weakest_rounds():
    """反问轮不该被选为"最需要复盘的"。"""
    turns = [
        _turn(1, "pass", 3.5),
        _turn(2, "fail", 1.2),
        _turn(3, "n/a", 0.0, phase="CANDIDATE_ASK"),
    ]
    r = _report_from_turns(turns)
    md = r["markdown"]

    # 找到"最需要复盘的"那一行
    import re
    m = re.search(r"\*\*最需要复盘的\*\*：第 (\d+) 轮", md)
    assert m, f"报告里应该有'最需要复盘的'段落：{md[:300]}"
    assert m.group(1) == "2", (
        f"应该指向第 2 轮（真正的低分轮），实际指向第 {m.group(1)} 轮"
    )


def test_candidate_ask_dims_excluded():
    """反问轮的维度分（全 0）不该进入雷达图聚合。"""
    turns = [
        _turn(1, "pass", 4.0),
        _turn(2, "pass", 4.0),
        _turn(3, "n/a", 0.0, phase="CANDIDATE_ASK"),
    ]
    r = _report_from_turns(turns)

    dims = r["dims"]
    # 两轮都是 4.0；如果混入反问轮的 0 会变成 2.67
    assert dims.get("accuracy") == 4.0, (
        f"维度分应为 4.0（排除反问轮），实际 {dims.get('accuracy')}"
    )


def test_all_turns_scored_still_works():
    """全是正常轮次时，行为不变（不破坏原有逻辑）。"""
    turns = [
        _turn(1, "pass", 3.0),
        _turn(2, "partial", 2.0),
    ]
    r = _report_from_turns(turns)

    assert r["avg"] == 2.5
    assert r["verdict_counts"] == {"pass": 1, "partial": 1}
    assert r["turns"] == 2


def test_empty_turns():
    """空会话不崩。"""
    r = _report_from_turns([])
    assert r["avg"] == 0.0
    assert r["turns"] == 0


# ============================================================================
# 反问轮必须写进 history（E2E 实跑发现的第二层 bug）
# ============================================================================

def test_candidate_ask_still_recorded_to_state_machine():
    """反问环节虽然不评分，但**必须 record()**。

    ## 为什么（E2E 实测的连锁 bug）

    第一版在 `evaluate_answer()` 里遇到 CANDIDATE_ASK 直接 return，
    没调 `sm.record()`。后果是两层的：

    ① `sm.turn_in_phase` 永远 0
       → 阶段推不动 → CANDIDATE_ASK 的 max_turns=2 形同虚设

    ② `sm.history` 里没有本轮问答
       → 下一轮出题时拿不到"上一轮问了什么"
       → 模型重复问同一句话

    实测：同一句"好，我这边先问到这里。你有什么想了解的"被问了 4 次。

    这个测试用 mock 的 sm 验证 record 被调用。
    """
    from unittest.mock import MagicMock

    from app.core.runtime import SessionRuntime

    # 造一个最小可用的 runtime：只测 evaluate_answer 会调 record
    rt = SessionRuntime.__new__(SessionRuntime)

    sm = MagicMock()
    sm.phase = "CANDIDATE_ASK"
    sm.history = []

    session = MagicMock()
    session.sm = sm
    session.evaluator = MagicMock()   # 不该被调用
    rt._session = session

    result = rt.evaluate_answer("你有哪些想问我的？", "我想了解团队技术栈")

    # 断言 1：不评分
    assert result["verdict"] == "n/a"
    assert result["scored"] is False

    # 断言 2：**record 被调用了**
    assert sm.record.called, (
        "反问轮必须调 sm.record()，否则 turn_in_phase 不累加、"
        "history 缺这一轮 → 阶段推不动 + 重复出题"
    )
    args = sm.record.call_args[0]
    assert args[0] == "你有哪些想问我的？"
    assert args[1] == "我想了解团队技术栈"
    assert args[2] == "n/a"

    # 断言 3：评分器不该被调用（反问不评分）
    assert not session.evaluator.evaluate.called, "反问轮不该调评分器"

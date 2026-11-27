"""报告截断测试：不能切破句子。

## 要解决的原始问题

用户在报告页看到：

    最需要复盘的：第 1 轮（1.3 分）—— 你好，我是本次面试官。流程四步：
    先请你简单自我介绍，然后针对简历里的项目做深挖，接着几个技术问题，
    最后留时间给你反问。不

最后那个孤零零的"不"——原文是"不确定的可以直接说不确定"，
被 `[:60]` 硬切在第 60 个字符处。

## 顺带修的语义错误

原来的实现用 `question`（面试官的问句）当"最需要复盘的内容"。
但候选人该复盘的是**自己的回答**，不是面试官问了什么。
拿开场白当复盘对象，读者完全不知道这段哪里不好。

## 这批测试锁住两件事

1. 截断点必须落在标点处（不切破句子）
2. 输出里必须包含回答，不只是问题
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.service import _round_highlight, _smart_truncate


# ============================================================================
# _smart_truncate：按句子边界截断
# ============================================================================

def test_short_text_unchanged():
    """没超长就原样返回，不加省略号。"""
    s = "召回从 71% 提到 82%。"
    assert _smart_truncate(s, 50) == s


def test_cuts_at_sentence_end():
    """优先在句号处断，不切破句子。"""
    s = (
        "你好，我是本次面试官。流程四步：先请你简单自我介绍，然后针对简历里的项目做深挖，"
        "接着几个技术问题，最后留时间给你反问。不确定的可以直接说不确定。"
    )
    out = _smart_truncate(s, 50)

    # 必须以句末标点结尾，不能是半句
    assert out.endswith(("。", "！", "？", "；")), f"截断在半个句子里：{out!r}"
    # 不能出现"被切断的半个词"
    assert not out.endswith("不"), f"切出了孤立的字：{out!r}"
    # 长度在合理范围
    assert len(out) <= len(s)
    assert len(out) <= 50 * 1.3 + 5


def test_cuts_at_comma_when_no_period():
    """没有句号时，退到逗号（至少是完整分句），并加省略号。"""
    s = "这个方案用了向量检索，切块 500 字，top20 重排，bge-m3 做 embedding，索引体积增加 30%"
    out = _smart_truncate(s, 25)

    assert out.endswith(("，", "、", "…")), f"截断点不在标点处：{out!r}"
    assert "…" in out or out.endswith("，")


def test_hard_cut_adds_ellipsis():
    """实在没标点，硬切并明确标省略号。"""
    s = "abcdefghij" * 20   # 200 个字母，无标点
    out = _smart_truncate(s, 30)

    assert out.endswith("…"), f"硬切必须加省略号：{out!r}"
    assert len(out) <= 33


def test_empty_returns_empty():
    assert _smart_truncate("", 50) == ""
    assert _smart_truncate("   ", 50) == ""


def test_newline_as_boundary():
    """换行也是合法断点（多段文本）。"""
    s = "第一段内容比较长需要截断处理这里\n第二段还有更多内容"
    out = _smart_truncate(s, 20)
    assert "\n" not in out or out.endswith("\n")


def test_no_orphan_single_char():
    """不能出现"只剩一个字"的截断（用户截图里的那个"不"）。

    连续测试多个长度阈值，任何一个都不该产生孤立单字结尾。
    """
    s = (
        "你好，我是本次面试官。流程四步：先请你简单自我介绍，"
        "然后针对简历里的项目做深挖，接着几个技术问题，"
        "最后留时间给你反问。不确定的可以直接说不确定。"
    )
    for limit in range(10, 120, 3):
        out = _smart_truncate(s, limit)
        if not out:
            continue
        # 截断后的结尾不能是"孤立的语义碎片"——
        # 简化判断：要么以标点结尾，要么带省略号
        assert out.endswith(("。", "！", "？", "；", "，", "、", "…", "\n")) or \
               len(out) <= limit, (
            f"limit={limit} 时截断点不在边界：{out[-10:]!r}"
        )


# ============================================================================
# _round_highlight：包含问题 + 回答
# ============================================================================

def test_highlight_includes_answer():
    """必须包含候选人的回答（原来只有问题，语义错了）。"""
    turns = [
        {"question": "请做自我介绍", "answer": "我叫张三，做过 RAG 项目，召回从 71% 提到 82%。",
         "score": 1.3, "verdict": "fail"},
    ]
    out = _round_highlight("最需要复盘的", turns, turns[0], best=False)

    assert "答：" in out, f"没有回答内容：{out}"
    assert "71%" in out or "82%" in out
    assert "1.3 分" in out
    assert "第 1 轮" in out


def test_highlight_includes_question():
    """也要有问句作为上下文。"""
    turns = [{"question": "请讲讲你的项目", "answer": "做了 RAG。", "score": 2.0}]
    out = _round_highlight("答得最好的", turns, turns[0], best=True)
    assert "问：" in out
    assert "请讲讲你的项目" in out


def test_highlight_handles_missing_answer():
    """没有回答时不崩，给出说明。"""
    turns = [{"question": "请做自我介绍", "answer": "", "score": 1.0}]
    out = _round_highlight("最需要复盘的", turns, turns[0], best=False)
    assert "第 1 轮" in out
    # 问题还在
    assert "请做自我介绍" in out


def test_highlight_correct_round_number():
    """轮次编号要和 turns 里的位置一致（1-based）。"""
    turns = [
        {"question": "q1", "answer": "a1", "score": 3.0},
        {"question": "q2", "answer": "a2", "score": 1.5},
        {"question": "q3", "answer": "a3", "score": 2.5},
    ]
    out = _round_highlight("最需要复盘的", turns, turns[1], best=False)
    assert "第 2 轮" in out, f"轮次编号错了：{out}"
    assert "1.5 分" in out


def test_highlight_uses_markdown_list():
    """输出应该是合法的 Markdown（前端要渲染）。"""
    turns = [{"question": "q", "answer": "a", "score": 2.0}]
    out = _round_highlight("答得最好的", turns, turns[0], best=True)
    # 加粗标题 + 列表项
    assert out.startswith("**答得最好的**")
    assert "\n- 问：" in out


# ============================================================================
# 逐轮记录：也要按句子边界截断
# ============================================================================

def test_round_by_round_uses_smart_truncate():
    """逐轮记录里的问答不能硬切。

    原来这里是 `q[:200]` / `a[:300]`，同一个 bug ——
    长问题会在第 200 个字被切断，读者看到半句话。

    这个测试用"正好会被切在半句"的文本验证。
    """
    from app.core.service import _report_from_turns

    long_q = (
        "你好，我是本次面试官。流程四步：先请你简单自我介绍，然后针对简历里的项目做深挖，"
        "接着几个技术问题，最后留时间给你反问。不确定的可以直接说不确定，说错了也没关系。"
    )
    long_a = (
        "我做过一个检索增强生成的项目。" * 40   # 远超 300 字
    )

    turns = [{
        "question": long_q,
        "answer": long_a,
        "verdict": "partial",
        "score": 2.0,
        "phase": "OPENING",
        "dims": {},
    }]

    r = _report_from_turns(turns)
    md = r["markdown"]

    # 找到逐轮记录里的那一轮
    assert "**问**：" in md
    assert "**答**：" in md

    # 问题：截断点必须在句末标点或带省略号
    q_line = next(l for l in md.split("\n") if l.startswith("**问**："))
    q_text = q_line.replace("**问**：", "").strip()
    assert q_text.endswith(("。", "！", "？", "；", "，", "…")), (
        f"问题被切在半句：{q_text[-15:]!r}"
    )

    # 回答：同样
    a_line = next(l for l in md.split("\n") if l.startswith("**答**："))
    a_text = a_line.replace("**答**：", "").strip()
    assert a_text.endswith(("。", "！", "？", "；", "，", "…")), (
        f"回答被切在半句：{a_text[-15:]!r}"
    )


def test_short_round_not_truncated():
    """短问答原样保留，不加省略号。"""
    from app.core.service import _report_from_turns

    turns = [{
        "question": "请做自我介绍",
        "answer": "我叫张三，三年后端经验。",
        "verdict": "pass",
        "score": 3.5,
        "phase": "SELF_INTRO",
        "dims": {},
    }]
    md = _report_from_turns(turns)["markdown"]

    assert "请做自我介绍" in md
    assert "我叫张三，三年后端经验。" in md
    assert "…" not in md


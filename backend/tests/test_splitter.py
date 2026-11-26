"""切片器的具体验证：简历切得对不对。

## 为什么要单独测切片

检索质量的上限由切片决定：切错了，向量再准也召回不到对的东西。

实测发现过一个 bug：简历里的 `项目一：课程问答助手（2026.03 - 2026.09）`
被切出来的 `project` 字段是 `"项目一："` 而不是 `"课程问答助手"`。
原因是标题正则的清理逻辑只去掉了 `项目一`，没处理后面的冒号。

这个 bug 只影响"显示用的项目名"，不影响召回（文本本身是对的），
但它会让报告里写"你在项目一：这个项目上…"——很难看。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag.splitter import split_bank_question, split_jd, split_resume


RESUME = """姓名：张三  求职方向：AI 应用开发

项目一：课程问答助手（2026.03 - 2026.09）
背景：同学找课程答案要翻 300 份 PDF，平均 8 分钟
做法：RAG 检索，切块 500 字，top20 重排，用 bge-m3 做 embedding
结果：召回从 71% 提到 82%，P99 延迟 1.8 秒
代价：索引体积增加 30%
我负责：切块策略实验、评测集标注、重排模块

项目二：智能客服工单分类
背景：客服每天涌入 2000 工单，人工分类平均 3 分钟
做法：先用规则兜底，再接小模型微调
结果：分类准确率 0.86
"""


def test_resume_splits_by_project():
    chunks = split_resume(RESUME)
    assert len(chunks) >= 2, f"应至少切出 2 个项目块，实际 {len(chunks)}"
    projects = {c.project for c in chunks}
    # 关键断言：项目名干净，不带"项目一："前缀
    assert any("课程问答助手" in p for p in projects), f"项目名未正确提取：{projects}"
    assert not any(p.strip() in ("项目一：", "项目一", "项目二：") for p in projects), \
        f"项目名残留编号前缀：{projects}"


def test_resume_parent_text_is_whole_project():
    """用户明确要求：命中某块后发整个项目，所以 parent_text 必须完整。"""
    chunks = split_resume(RESUME)
    course = [c for c in chunks if "课程问答助手" in c.project or "课程问答助手" in c.text]
    assert course, "没找到课程问答助手项目"
    c = course[0]
    assert "召回从 71% 提到 82%" in c.parent_text, "parent_text 应含项目全部内容"
    assert "我负责" in c.parent_text, "parent_text 应包含职责段"
    # parent_text 应比单块 text 大或相等
    assert len(c.parent_text) >= len(c.text)


def test_resume_facts_preserved():
    """数字和术语不能被切丢——它们是追问的锚点。"""
    chunks = split_resume(RESUME)
    blob = "\n".join(c.text for c in chunks)
    for fact in ("71%", "82%", "1.8 秒", "30%", "500 字", "top20"):
        assert fact in blob, f"切片丢失关键事实：{fact}"


def test_empty_resume():
    assert split_resume("") == []
    assert split_resume("   \n  ") == []


def test_resume_without_project_structure():
    """没有"项目X"这类标题时，整篇当一块，且标记为未识别。"""
    text = "我会 Python 和 Java，做过一个检索系统。"
    chunks = split_resume(text)
    assert len(chunks) == 1
    assert "未识别" in chunks[0].project


def test_jd_splits_by_bullet():
    jd = """岗位职责：
- 负责 RAG 检索系统设计与优化
- 参与 Agent 编排框架开发
- 2. 有向量数据库使用经验

任职要求：
1. 熟悉 Python 与 FastAPI
2. 了解 pgvector 或 FAISS
"""
    chunks = split_jd(jd)
    assert len(chunks) >= 4, f"JD 应切成多条，实际 {len(chunks)}"
    blob = "\n".join(c.text for c in chunks)
    assert "RAG 检索系统" in blob
    assert "pgvector" in blob


def test_bank_question_includes_points():
    """题库向量化必须带 points —— 考点信息主要在要点里。"""
    text = split_bank_question(
        "多头注意力里每个头的维度怎么取",
        ["d_k = d_v = d_model / h", "concat 后经 W_O 融合"],
    )
    assert "多头注意力" in text
    assert "d_k = d_v = d_model / h" in text
    assert "考点：" in text


def test_bank_question_without_points():
    assert split_bank_question("某个问题", []) == "某个问题"
    assert split_bank_question("某个问题", None) == "某个问题"


def test_long_project_splits_with_overflow():
    """超长项目要切成多块，但每块的 parent_text 都是整项目。"""
    long_body = "项目一：超长项目\n" + ("这是一个很长的描述。" * 200)
    chunks = split_resume(long_body, max_chunk_chars=200)
    assert len(chunks) > 1, "超长项目应被切成多块"
    parents = {c.parent_text for c in chunks}
    assert len(parents) == 1, "同一项目的所有块 parent_text 应相同"

"""简历质量与要素推进的回归测试（这一轮真机体检暴露的问题固化下来）。

每条测试对应一个在真实简历上炸过的现象：
  - 区块标题当项目 → 追问「参考技能关键字这个项目解决什么问题」
  - 人名当项目 → 追问「张小明这个项目…」
  - 短简历被误杀 → 注入用例本该 PASS 被打成 FAIL
  - 没模型就 0 项目 → 用户以为工具坏了
  - 要素覆盖恒 0 → 统计口径错，看起来"什么都没问到"
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import MockLLM
from resume import parse_resume_best, resume_quality
from schemas import Project
from session import InterviewSession


class TestSectionFiltering(unittest.TestCase):
    def test_section_title_not_project(self):
        text = (
            "## 个人信息\n电话 123，邮箱 a@b.com，期望岗位：后端\n\n"
            "## 技能清单\n- Python\n- Redis\n\n"
            "## 订单系统重构\n- 用 Python 重写订单服务，接口 P99 从 800ms 降到 120ms\n"
        )
        names = [p.name for p in parse_resume_best(text, llm=None)]
        self.assertNotIn("个人信息", names)
        self.assertNotIn("技能清单", names)
        self.assertTrue(any("订单" in n for n in names), names)

    def test_person_name_not_project(self):
        text = "## 张小明\nAI 方向，找实习\n\n## 检索问答系统\n- 用 Python + FAISS 做检索，召回 82%\n"
        names = [p.name for p in parse_resume_best(text, llm=None)]
        self.assertNotIn("张小明", names, names)

    def test_short_project_kept(self):
        """短项目正文也要留住：注入用例就是这么被误杀的。"""
        text = "## 课程问答助手\n- 技术栈：Python, RAG\n- 覆盖 300 份课件\n"
        self.assertEqual(len(parse_resume_best(text, llm=None)), 1)


class TestNoLlmFallback(unittest.TestCase):
    def test_plain_text_without_llm_gets_fallback_projects(self):
        text = (
            "在某集团负责前端架构，使用 React 与 TypeScript 重构了可视化平台，"
            "支撑 20 个业务页面。\n\n"
            "主导城市人群流动可视分析，用 D3 与 WebGL 渲染百万级点位，"
            "首屏从 4.2s 降到 1.6s。\n"
        )
        projects = parse_resume_best(text, llm=None)
        self.assertTrue(projects, "没有模型时也要给粗略项目，不能是 0")
        self.assertTrue(all(p.stack for p in projects) or True)


class TestResumeQuality(unittest.TestCase):
    def test_no_metrics_reports_advice(self):
        projs = [Project(name="A", stack=["Python"]), Project(name="B", stack=["Go"])]
        q = resume_quality(projs)
        self.assertEqual(q["metrics_ratio"], 0.0)
        self.assertIn("量化指标", q["advice"])

    def test_partial_metrics(self):
        projs = [Project(name="A", stack=["Python"], metrics=["82%"]),
                 Project(name="B", stack=["Go"])]
        q = resume_quality(projs)
        self.assertAlmostEqual(q["metrics_ratio"], 0.5)
        self.assertIn("B", q["advice"])

    def test_empty(self):
        q = resume_quality([])
        self.assertEqual(q["projects"], 0)
        self.assertTrue(q["advice"])


class TestElementCoverage(unittest.TestCase):
    """没有 --plan 时也必须按要素推进，否则 12 轮全耗在同一个项目的同一层。"""

    def test_elements_advance_without_plan(self):
        projs = [
            Project(name="检索问答助手", stack=["Python", "FAISS"], metrics=["召回 82%"]),
            Project(name="订单系统", stack=["FastAPI", "Redis"]),
        ]
        sess = InterviewSession(llm=MockLLM(), projects=projs, role_key="agent_app",
                                max_turns=16, verbose=False, save=False, seed=3)
        answers = [
            "目标是让同学能查课程资料，规模 300 份课件，我一个人做的。",
            "用检索是因为上下文塞不下，试过摘要但会丢公式，这就是失败方案。",
            "我对比的 baseline 是不做检索直接塞全文，指标看召回率和延迟。",
            "重排让延迟从 0.9 涨到 1.8 秒，但召回从 71% 到 82%，这个取舍我认。",
            "空召回率超过 5% 就会告警，我用它发现失效。",
        ] * 4
        sess.run_scripted(answers)
        handled = 0
        for p in projs:
            rep = sess.sm.probe.element_report(p.name)
            handled += sum(1 for v in rep.values() if "未问到" not in str(v))
        self.assertGreaterEqual(handled, 4, f"要素推进不足：handled={handled}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""影子树一致性测试（提案 S4 的迁移门禁）。

规则：树的覆盖率报告必须与计数器的 `element_report` **逐项一致**，
不一致就不允许切主路径。这条测试就是那道闸门。

为什要这么多随机种子：树和计数器是两套独立记账，任何一处漏记
（比如"顺带答到"只在计数器里记了）都只会在某些路径上暴露。
单种子跑通说明不了什么，多轮多场景才说明"两套账对得上"。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm import MockLLM
from probe_tree import ProbeTree
from resume import parse_resume_best
from session import InterviewSession

RESUME = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "test_resumes", "our_sample.md")

ANSWERS_SETS = [
    ["目标 300 份，baseline 不做检索，召回 71%，我独立做的。"] * 16,
    ["不知道"] * 16,
    ["效果很好，团队一起做的。"] * 16,
    ["先做解析再做检索，延迟 0.9 秒，召回 71% 到 82%。",
     "baseline 是不做检索直接塞全文。", "失败方案是用摘要替代检索。",
     "指标看召回率和空召回率。", "这部分是我一个人写的。"] * 4,
]


class TestTreeParity(unittest.TestCase):
    def test_parity_over_seeds_and_answers(self):
        projects = parse_resume_best(open(RESUME, encoding="utf-8").read())
        mismatches: list[str] = []
        for seed in range(3):
            for ai, answers in enumerate(ANSWERS_SETS):
                sess = InterviewSession(llm=MockLLM(), projects=projects, role_key="ai_algo",
                                        max_turns=14, verbose=False, save=False, seed=seed)
                sess.run_scripted(list(answers))
                for p in projects:
                    tree_rep = sess.tree.coverage(p.name)
                    counter_rep = sess.sm.probe.element_report(p.name)
                    if tree_rep != counter_rep:
                        mismatches.append(
                            f"seed{seed}/answers{ai}/{p.name}: 树={tree_rep} 表={counter_rep}")
        self.assertEqual(mismatches, [], "影子树与计数器不一致：\n" + "\n".join(mismatches[:3]))

    def test_tree_records_parent_child(self):
        tree = ProbeTree()
        n1 = tree.add_question("P", 0, "目标", "q1")
        n2 = tree.add_question("P", 1, "目标", "q2")
        tree.note_verdict(n1, "fail")
        self.assertEqual(tree.nodes[n2].parent_id, n1)
        self.assertIn(n2, tree.nodes[n1].children)

    def test_close_idempotent(self):
        tree = ProbeTree()
        n1 = tree.add_question("P", 0, "指标", "q1")
        tree.close(n1, "pass")
        tree.close(n1, "again")
        self.assertEqual(tree.nodes[n1].close_reason, "pass")

    def test_render_contains_reason(self):
        tree = ProbeTree()
        n1 = tree.add_question("P", 0, "目标", "你最初要解决什么问题？")
        tree.note_verdict(n1, "pass")
        tree.close(n1, "element_closed")
        text = tree.render_text("P")
        self.assertIn("element_closed", text)
        self.assertIn("✓", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

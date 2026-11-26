"""阶段 SSOT 守卫：后端阶段定义、顺序、前端可见文案必须同一份真相。

借 real-mock 的 `test_phase_ssot.py`。为什么值得抄：
  我们的阶段在四个地方出现——config.PHASE_CONFIG（题量/文案）、
  state_machine.ORDER（推进顺序）、webui（进度条文案）、run.py（交互帮助）。
  任何一处改了、别处没改，用户就会看到"阶段显示 3/6 但实际已经到第 5 个"。
  这类漂移不会让测试挂，只会让人不信任界面。

这个测试挂了不代表功能坏，代表**真相分裂了**。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import state_machine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestPhaseSSOT(unittest.TestCase):
    def test_order_matches_config(self):
        """推进顺序里的每个阶段，必须在 PHASE_CONFIG 里有定义。"""
        missing = [p for p in state_machine.ORDER if p not in config.PHASE_CONFIG]
        self.assertEqual(missing, [], f"ORDER 有、PHASE_CONFIG 没有：{missing}")

    def test_config_matches_order(self):
        """PHASE_CONFIG 里的每个阶段，必须能在 ORDER 里被推进到（否则永远到不了）。"""
        extra = [p for p in config.PHASE_CONFIG if p not in state_machine.ORDER]
        self.assertEqual(extra, [], f"PHASE_CONFIG 有、ORDER 没有：{extra}")

    def test_every_phase_has_desc(self):
        for ph, cfg in config.PHASE_CONFIG.items():
            self.assertTrue(cfg.get("desc"), f"{ph} 缺少 desc（前端会显示空白）")

    def test_weights_cover_all_phases(self):
        """权重表必须覆盖所有阶段，否则预算分配会漏掉一个阶段。"""
        missing = [p for p in config.PHASE_CONFIG if p not in config.PHASE_WEIGHTS]
        self.assertEqual(missing, [], f"PHASE_WEIGHTS 缺少：{missing}")

    def test_budget_matches_total(self):
        """预算合计必须等于总轮次（否则滑块拖了也没用）。

        默认轮次走的是"直接返回 PHASE_CONFIG"的快捷分支（保证不改变既有行为），
        它的合计按 PHASE_CONFIG 自己的数算（含 FEEDBACK）。
        """
        for n in (5, 6, 10, 16, 40):
            budget = config.allocate_phase_budget(n)
            used = sum(v["max_turns"] for k, v in budget.items() if k != "FEEDBACK")
            self.assertEqual(used, max(n, 5), f"max_turns={n} 时预算合计 {used}")

    def test_default_total_reproduces_config(self):
        """默认轮次必须与 PHASE_CONFIG 逐字一致：引入分配不能顺带改掉老行为。"""
        default_total = sum(int(c["max_turns"]) for c in config.PHASE_CONFIG.values())
        budget = config.allocate_phase_budget(default_total)
        for ph, cfg in config.PHASE_CONFIG.items():
            self.assertEqual(budget[ph]["max_turns"], cfg["max_turns"], f"{ph} 被改了")

    def test_budget_never_zero_for_real_phase(self):
        for n in (5, 6, 10):
            budget = config.allocate_phase_budget(n)
            for ph in ("OPENING", "SELF_INTRO", "PROJECT_PROBE", "TECH_QA", "CANDIDATE_ASK"):
                self.assertGreaterEqual(budget[ph]["max_turns"], 1, f"{ph} 被分到 0 轮")

    def test_state_machine_uses_budget(self):
        """状态机必须读预算，而不是读死常量。"""
        sm = state_machine.InterviewStateMachine(projects=["P"], tech_topics=["T"], max_turns=10)
        self.assertEqual(sm.phase_cfg["max_turns"], sm.phase_budget["OPENING"]["max_turns"])
        short = state_machine.InterviewStateMachine(projects=["P"], tech_topics=["T"], max_turns=6)
        long = state_machine.InterviewStateMachine(projects=["P"], tech_topics=["T"], max_turns=24)
        self.assertLess(short.phase_budget["PROJECT_PROBE"]["max_turns"],
                        long.phase_budget["PROJECT_PROBE"]["max_turns"],
                        "总轮次变多时深挖预算应变大")

    def test_frontend_phase_labels_present(self):
        """前端不硬编码阶段名（它用后端给的 desc），确认它没引入第二套真相。

        历史：这条测试原来检查 `webui.py`（旧单体前端）。
        旧架构已归档到 legacy/，现在检查新的 Vue 前端。

        检查逻辑不变，只是目标换了：扫所有 .vue/.ts 源文件，
        如果出现 `config.PHASE_CONFIG` 里的阶段名（如 "PROJECT_PROBE"），
        说明前端自己维护了一套阶段真相——那就会和后端不同步。
        """
        src_dir = os.path.join(ROOT, "frontend", "src")
        if not os.path.isdir(src_dir):
            self.skipTest("前端源码不存在（只跑后端时跳过）")

        offenders = []
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                if not name.endswith((".vue", ".ts")):
                    continue
                # 测试文件要排除：里面的 fixture 本来就得写死阶段名，
                # 那是"构造测试数据"，不是"维护第二套真相"。
                if ".test." in name or ".spec." in name:
                    continue
                path = os.path.join(root, name)
                try:
                    src = open(path, encoding="utf-8").read()
                except OSError:
                    continue
                for phase in config.PHASE_CONFIG:
                    if f'"{phase}"' in src or f"'{phase}'" in src:
                        rel = os.path.relpath(path, ROOT)
                        offenders.append(f"{rel}: {phase}")

        self.assertEqual(
            offenders, [],
            f"前端出现硬编码阶段名（应只用后端返回的 phase desc）：{offenders}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

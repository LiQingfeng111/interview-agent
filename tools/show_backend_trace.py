"""打印一轮真实面试背后的后端动作链（给"后端干嘛的"这个问题看的证据）。

不做抽象描述，直接把一轮里发生的事按顺序列出来：
  哪个模块 → 干了什么 → 产生了什么可查的痕迹（事件/文件）。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ledger import E  # noqa: E402
from llm import MockLLM  # noqa: E402
from resume import parse_resume_best  # noqa: E402
from session import InterviewSession  # noqa: E402

MODULE = {
    E.RESUME_PARSED: "resume.parse_resume_best → 简历拆成项目（规则+模型兜底）",
    E.PLAN_BUILT: "planner / mock_plan → 生成提问计划（覆盖校验）",
    E.TURN_ASKED: "interviewer.next_question → 出题（木梯层级/要素/去重）",
    E.LLM_CALL: "tracing.TracedLLM → 每次模型调用：记 token/成本/延迟/指纹",
    E.EVAL_RETURNED: "evaluator.evaluate → 五维评分 + 判定 + 要素归类",
    E.MEMORY_FACT: "memory.observe → 从回答里抽事实（项目/指标/数值）",
    E.MEMORY_CONTRADICTION: "memory.pending_contradiction → 前后矛盾检测",
    E.INVARIANT_VIOLATED: "invariants.check_all → 8 条运行时不变量体检",
    E.BUDGET_EXCEEDED: "budget.spend → 预算/降级级别",
    E.FINISH: "verifier.should_finish → 结束原因闭集",
}


def main() -> int:
    resume_text = open(os.path.join(ROOT, "data", "test_resumes", "our_sample.md"),
                       encoding="utf-8").read()
    projects = parse_resume_best(resume_text)
    s = InterviewSession(llm=MockLLM(), projects=projects, role_key="ai_algo",
                         max_turns=6, verbose=False, save=False, seed=3,
                         coach=True, audit=True)
    s.start()
    s.step("目标是把找答案时间压到 1 分钟内，语料 300 份 PDF，召回 71%。")

    print("=" * 78)
    print("一轮面试背后发生了什么（按事件顺序）")
    print("=" * 78)
    for ev in s.ledger.events:
        desc = MODULE.get(ev.type, "")
        extra = ""
        if ev.type == E.LLM_CALL:
            extra = f" [{ev.op}] {ev.tokens_in}+{ev.tokens_out} token, ¥{ev.cost_cny:.6f}, {ev.latency_ms}ms"
        elif ev.type == E.TURN_ASKED:
            extra = f" 要素={ev.payload.get('element') or '-'} 层={ev.payload.get('level')}"
        elif ev.type == E.EVAL_RETURNED:
            extra = f" 判定={ev.payload.get('verdict')}"
        elif ev.type == E.MEMORY_FACT:
            extra = f" {ev.payload.get('project','')}·{ev.payload.get('attr','')}={ev.payload.get('value','')}"
        print(f"  {ev.seq:>2} {ev.type:<22} {desc}{extra}")

    print()
    print("落盘产物：")
    ledger_path = getattr(s.ledger, "path", "") or "（本次未落盘）"
    print(f"  事件流  {ledger_path}")
    print(f"  会话库  data/sessions.db（SQLite，每轮 upsert）")
    print(f"  报告    runs/ 或 reports/（Markdown/HTML/PNG）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

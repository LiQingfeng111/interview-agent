"""Prompt A/B：比较两版提问官 prompt，回答"改了到底有没有用"。

比较什么 / 不比较什么（这条界限是这份脚本的诚实之处）：
  比较：锚定率、承接率、重复问率、平均问题长度、多问合一率、复述项目名率
        —— 全部可自动计算，不依赖人工标注，也就不用求人。
  不比较：候选人得分。因为评分官在这里用 mock（省时间、降方差），
        它给出的分数只反映"回答长不长"，拿来比较 prompt 是自欺欺人。

跑法：
  python eval/ab_prompt.py --versions v1 v2 --cases 3 --verbose
  python eval/ab_prompt.py --versions v1 v2 --model lobsterai/deepseek-flash
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "eval"))

from llm import MockLLM, build_llm  # noqa: E402
from prompts_loader import list_versions, load_prompt  # noqa: E402
from quality import anchor_score, follow_score  # noqa: E402
from routing import RoutingLLM  # noqa: E402
from session import InterviewSession  # noqa: E402

from run_eval import STRONG_ANSWERS, build_projects, load_cases  # noqa: E402


def _multi_ask(question: str) -> int:
    """是否把多个问题串在一起问（"以及""另外""还有" + 多个问号）。"""
    q = question or ""
    marks = q.count("？") + q.count("?")
    glue = sum(1 for w in ("以及", "另外", "还有", "顺便", "同时", "并且") if w in q)
    return 1 if (marks >= 2 or glue >= 1) else 0


def _restates_project(question: str, projects: list) -> int:
    """是否用项目名开头复述（"关于你的XX项目…"）。"""
    q = question or ""
    if not re.match(r"^(关于|对于|那|好的|请你?说)", q):
        return 0
    for p in projects or []:
        name = getattr(p, "name", "")
        if name and name in q[:20]:
            return 1
    return 0


def _unique_ratio(questions: list[str]) -> float:
    if not questions:
        return 0.0
    return len({q.strip() for q in questions}) / len(questions)


def measure(version: str, cases: list[dict], use_real: bool, model: str | None) -> dict:
    """用某一版 prompt 跑若干用例，输出内容指标。"""
    real = build_llm(mode="auto", model=model) if use_real else MockLLM()
    rows = []

    for case in cases:
        if case.get("kind") not in (None, "probe"):
            continue  # 只比深挖类用例
        projs = build_projects(case.get("projects", []))
        answers = STRONG_ANSWERS
        llm = RoutingLLM({"interviewer": real}, default=MockLLM())

        sess = InterviewSession(
            llm=llm,
            projects=projs,
            role_key=case.get("role", "agent_app"),
            max_turns=case.get("max_turns", 18),
            verbose=False,
            save=False,
            seed=abs(hash(case["id"])) % 10000,
            prompt_version=version,
        )
        sess.run_scripted(list(answers))
        turns = sess.turns
        probe = [t for t in turns if t.phase == "PROJECT_PROBE"]
        if not probe:
            continue

        qs = [t.question for t in probe]
        anchors, follows, multi, restate = 0, 0, 0, 0
        for t in probe:
            anchors += anchor_score(t.question, projs)
            multi += _multi_ask(t.question)
            restate += _restates_project(t.question, projs)
        for i, t in enumerate(turns):
            if t.phase != "PROJECT_PROBE" or i == 0:
                continue
            follows += follow_score(t.question, turns[i - 1].answer)

        n = len(probe)
        rows.append({
            "case": case["id"],
            "n": n,
            "anchor": anchors / n,
            "follow": follows / n,
            "multi": multi / n,
            "restate": restate / n,
            "uniq": _unique_ratio(qs),
            "avg_len": sum(len(q) for q in qs) / n,
        })

    return {"version": version, "rows": rows, "prompt": load_prompt("interviewer", version)}


def summarize(result: dict) -> dict:
    rows = result["rows"]
    if not rows:
        return {"version": result["version"], "n": 0}
    n = len(rows)

    def avg(key):
        return sum(r[key] for r in rows) / n

    return {
        "version": result["version"],
        "cases": n,
        "questions": sum(r["n"] for r in rows),
        "anchor": avg("anchor"),
        "follow": avg("follow"),
        "multi": avg("multi"),
        "restate": avg("restate"),
        "uniq": avg("uniq"),
        "avg_len": avg("avg_len"),
    }


def render(sums: list[dict]) -> str:
    head = (
        f"{'版本':<8}{'用例':<6}{'问题数':<8}{'锚定率':<9}{'承接率':<9}"
        f"{'多问合一':<9}{'复述项目名':<10}{'不重复率':<9}{'平均字数':<8}"
    )
    lines = [head, "-" * len(head)]
    for s in sums:
        if not s.get("cases"):
            lines.append(f"{s['version']:<8}（无有效用例）")
            continue
        lines.append(
            f"{s['version']:<8}{s['cases']:<6}{s['questions']:<8}"
            f"{s['anchor']:<9.0%}{s['follow']:<9.0%}{s['multi']:<9.0%}"
            f"{s['restate']:<10.0%}{s['uniq']:<9.0%}{s['avg_len']:<8.0f}"
        )
    return "\n".join(lines)


def pick_winner(sums: list[dict]) -> dict | None:
    """综合打分：正向指标加权，负向指标扣分。

    权重为什么这么定：
      承接率最重（0.4）——"有没有在听人说话"是面试官的第一素质；
      锚定率其次（0.3）——问得空就是通用题库；
      多问合一与复述项目名是硬伤，直接扣（各 0.15）；
      不重复率作为基本盘加一点（0.1）。
    """
    scored = []
    for s in sums:
        if not s.get("cases"):
            continue
        score = (
            0.4 * s["follow"]
            + 0.3 * s["anchor"]
            + 0.1 * s["uniq"]
            - 0.15 * s["multi"]
            - 0.15 * s["restate"]
        )
        scored.append((score, s))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    best = dict(scored[0][1])
    best["score"] = round(scored[0][0], 3)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--versions", nargs="+", default=None,
                    help="要比较的 prompt 版本，默认列出 prompts/ 下所有版本")
    ap.add_argument("--cases", type=int, default=3, help="取前 N 条深挖用例")
    ap.add_argument("--model", default=None, help="真实模型名（默认自动探测）")
    ap.add_argument("--mock", action="store_true", help="用 mock 跑（只验证机制，不比质量）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    versions = args.versions or list_versions("interviewer") or ["default"]
    cases = [c for c in load_cases() if c.get("kind", "probe") == "probe"][: args.cases]
    if not cases:
        print("没有可用的深挖用例")
        return 1

    use_real = not args.mock
    if use_real:
        # 先确认能连上模型，否则 A/B 结果毫无意义
        probe_llm = build_llm(mode="auto", model=args.model)
        if getattr(probe_llm, "is_mock", False):
            print("[A/B] 没连上真实模型，无法比较 prompt 质量。请先启动中继或配置 Key。")
            return 1

    print(f"[A/B] 版本：{', '.join(versions)}｜用例 {len(cases)} 条｜后端：{'真实' if use_real else 'mock'}")
    if not use_real:
        print("[A/B] 注意：mock 是模板出题，版本之间不会有真实差异，本次仅验证机制。\n")

    results = [measure(v, cases, use_real, args.model) for v in versions]
    sums = [summarize(r) for r in results]
    print()
    print(render(sums))

    if args.verbose:
        for r in results:
            print(f"\n--- {r['version']} 的 prompt 前 80 字 ---")
            print("  " + r["prompt"][:80].replace("\n", " "))

    winner = pick_winner(sums)
    if winner:
        print()
        print(f"[A/B] 综合最优：{winner['version']}（score {winner['score']}）")
        print("      权重：承接0.4 锚定0.3 不重复0.1 −多问0.15 −复述0.15")
        print("      注意：本结论只覆盖内容指标，不包含候选人的得分——")
        print("            评分官在 A/B 中用 mock，它的分数不能用来比较 prompt。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

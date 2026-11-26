"""评估回归：量化"这个 Agent 是不是真的在好好面试"。

不做这一步，你的项目就只是个 demo。
做了这一步，你可以说："我有 5 条用例，状态机阶段命中率 100%，
深挖深度达标率 80%，换项目策略在敷衍场景下生效。"

指标定义：
  - phase_recall: 期望阶段中实际出现过的比例（衡量流程完整性）
  - depth_ok: 深挖最深层级是否 >= 期望值（衡量追问力度）
  - topic_hit: 技术问答是否命中期望话题（衡量岗位知识库生效）
  - no_deadlock: 敷衍场景下是否触发换项目（衡量防死磕）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from knowledge import role_topics  # noqa: F401  (保留导出，便于后续扩展话题覆盖统计)
from config import PERSONAS
from llm import MOCK_PERSONA_LEAD, MockLLM
from planner import build_plan, validate_plan
from resume import parse_resume
from schemas import Project
from session import InterviewSession
from state_machine import LADDER

CASES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.jsonl")
SAMPLE_RESUME = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "resume_sample.md"
)

STRONG_ANSWERS = [
    "我说一下这个 Agent 工具调用实验。目标是让模型能调用本地文件与检索工具，规模是 6 个工具、约 200 次实测调用，我一个人做的。",
    "为什么要自己写分发而不是用 LangChain：因为链式抽象的中间态不可观测，我要在每次调用前后插日志与校验，自写分发大概 120 行，可控性明显更好。",
    "怎么做的：工具用装饰器注册，参数走 JSON schema 校验，调用前做一次 schema 强校验，失败直接返回错误给模型重试。日志记录 token 数和耗时。",
    "取舍：重试上限设 2 次，超过就直接报错给用户。因为继续重试会放大延迟，实测 2 次能覆盖 90% 的可恢复错误，成本上更划算。",
    "边界：遇到带副作用且不幂等的工具，重复调用会出问题，所以我给写操作加了幂等键。监控上我统计错误码分布，超阈值告警。",
    "如果重做，我会保留 schema 校验和幂等键，砍掉自己写的日志，换成 OpenTelemetry。因为日志格式我改了三次，复用标准更省事。",
    "第二个项目是检索问答。目标是给同学查课程资料，覆盖 300 份课件，我独立做完了数据清洗到接口整条链路。",
    "为什么用检索而不是塞全文：器材上下文只有 8k，300 份课件远超上限；也试过先做摘要，但摘要把公式细节丢了，所以最终选检索。",
    "具体落地：按标题和段落切块，块大小 500 字、重叠 80 字，嵌入用通用模型，检索后加一层重排，接口层加了 3 秒超时。",
    "取舍：重排模型让延迟从 0.9 秒涨到 1.8 秒，但召回从 71% 提到 82%，我判断这个交换值得，因为用户更在意答案对不对。",
    "失效场景：提问用词和课件差异太大时召回不到，我监控空召回率，超过 5% 就补同义词表；另外课件更新后索引不同步也会失效。",
    "如果重做：保留检索加重排，砍掉自己写的 PDF 解析器，换成现成库，因为解析器我修了两周，投入产出比太低。",
    "技术方面我了解限流用令牌桶，也了解召回率要用带标注的测试集测，不能只凭感觉。",
    "我踩过参数幻觉的坑，模型传了不存在的字段，用 schema 校验后失败率从 11% 降到 3%。",
    "缓存一致性我倾向于先更新库再删缓存，因为反过来的话并发读会把旧值写回缓存。",
    "数据库索引我注意过最左前缀和回表代价，慢查询会先看执行计划。",
    "评估方面我搭了 20 条回归用例，用固定 seed 跑，指标变化超过阈值才允许合并。",
    "上下文工程上我做过对话压缩，把历史摘要成要点，因为全量塞进去 token 成本翻了三倍。",
    "我想问实习生会不会独立负责一个模块，以及平时怎么做代码评审。",
    "暂时没有了，谢谢。",
]

VAGUE_ANSWERS = [
    "我做过一些项目。",
    "就是做了一下，还行。",
    "具体记不清了。",
    "大概是用了那个技术。",
    "反正能跑。",
    "不太清楚。",
    "忘了。",
    "差不多吧。",
    "应该是。",
    "没有。",
]


def load_cases() -> list[dict]:
    cases = []
    with open(CASES, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


def build_projects(names: list[str]) -> list[Project]:
    try:
        with open(SAMPLE_RESUME, "r", encoding="utf-8") as f:
            parsed = parse_resume(f.read(), llm=None)
    except OSError:
        parsed = []
    by_name = {p.name: p for p in parsed}
    out = []
    for n in names:
        out.append(by_name.get(n, Project(name=n, stack=["Python"])))
    return out


def _eval_resume(case: dict) -> dict:
    """简历解析用例：只测解析，不跑面试。

    为什么单独一条：解析结果是后面一切的输入，它错了后面全错，
    但原来的 5 条用例都假设简历已经解析好了——等于最关键的一步没有测试。
    """
    text = case.get("resume_text", "")
    projects = parse_resume(text, llm=None)
    names = [p.name for p in projects]
    expect_n = case.get("expect_projects", 0)
    expect_names = case.get("expect_names_any", [])
    ok = len(projects) == expect_n
    if expect_names:
        ok = ok and all(any(k in n for n in names) for k in expect_names)
    return {
        "id": case["id"],
        "kind": "resume",
        "desc": case.get("desc", ""),
        "turns": 0,
        "final_phase": "RESUME_PARSE",
        "deepest_layer": "-",
        "deepest_index": 0,
        "phase_recall": 1.0,
        "depth_ok": True,
        "topic_hit": True,
        "no_deadlock": True,
        "no_repeat": True,
        "repeats": 0,
        "avg_score": 0.0,
        "persona_ok": None,
        "plan_ok": None,
        "resume_ok": ok,
        "parsed_projects": names,
        "passed": ok,
    }


def evaluate_case(case: dict, mock: bool = True) -> dict:
    kind = case.get("kind", "probe")
    if kind == "resume":
        return _eval_resume(case)

    projs = build_projects(case.get("projects", []))
    mode = case.get("answers_mode", "strong")
    answers = {"vague": VAGUE_ANSWERS, "strong": STRONG_ANSWERS}.get(mode, STRONG_ANSWERS)

    llm = MockLLM()
    persona = case.get("persona", "balanced")
    use_plan = bool(case.get("use_plan"))
    sess = InterviewSession(
        llm=llm,
        projects=projs,
        role_key=case.get("role", "agent_app"),
        max_turns=case.get("max_turns", 30),
        verbose=False,
        save=False,
        # 必须给固定 seed：pick_tech_topics 用 random.sample，不给 seed 的话
        # 每次跑抽到的考点都不同，回归结果不可比。
        seed=abs(hash(case["id"])) % 10000,
        persona=persona,
        use_plan=use_plan,
    )
    result = sess.run_scripted(list(answers))
    data = result.to_dict()

    phases = [t["phase"] for t in data["turns"]]
    expect_phases = case.get("expect_phases", [])
    seen = set(phases) | {sess.sm.phase}
    phase_recall = (
        sum(1 for p in expect_phases if p in seen) / len(expect_phases)
        if expect_phases else 1.0
    )

    deepest = 0
    for t in data["turns"]:
        if t["phase"] == "PROJECT_PROBE":
            deepest = max(deepest, t["ladder_level"])
    expect_depth = case.get("expect_min_depth", 0)
    depth_ok = deepest >= expect_depth

    topic_hit = True
    if case.get("expect_topics_any"):
        asked = list(sess.sm.tech.asked)
        topic_hit = any(
            kw in t for kw in case["expect_topics_any"] for t in asked
        )

    no_deadlock = True
    if "max_strikes" in case:
        switches = [tr for tr in sess.sm.transitions if str(tr[1]).startswith("PROBE:")]
        no_deadlock = len(switches) > 0

    # 新增：连续重复提问检测（用户实测发现的问题，固化成回归指标）
    qs = [t["question"].strip() for t in data["turns"]]
    repeats = sum(1 for i in range(1, len(qs)) if qs[i] and qs[i] == qs[i - 1])
    no_repeat = repeats == 0

    # 人设用例：mock 下人设会体现在措辞前缀上，可精确断言
    persona_ok = None
    if kind == "persona":
        lead = MOCK_PERSONA_LEAD.get(persona, "")
        probe_qs = [t["question"] for t in data["turns"] if t["phase"] == "PROJECT_PROBE"]
        if not probe_qs:
            persona_ok = False
        elif not lead:                     # balanced/efficient 无前缀，只能验流程
            persona_ok = True
        else:
            persona_ok = all(q.startswith(lead) for q in probe_qs)

    # 计划用例：校验覆盖 + 计划要素是否真被问到
    plan_ok = None
    if kind == "plan":
        ok_cover, _issues = validate_plan(sess.plan, projs)
        used = len(getattr(sess.interviewer, "asked_elements", set()))
        plan_ok = ok_cover and used >= case.get("expect_plan_min", 3)

    passed = bool(
        phase_recall >= 0.9 and depth_ok and topic_hit and no_deadlock and no_repeat
    )
    if persona_ok is False or plan_ok is False:
        passed = False

    return {
        "id": case["id"],
        "kind": kind,
        "desc": case.get("desc", ""),
        "turns": sess.sm.total_turns,
        "final_phase": sess.sm.phase,
        "deepest_layer": LADDER[min(deepest, len(LADDER) - 1)],
        "deepest_index": deepest,
        "phase_recall": round(phase_recall, 3),
        "depth_ok": depth_ok,
        "topic_hit": topic_hit,
        "no_deadlock": no_deadlock,
        "no_repeat": no_repeat,
        "repeats": repeats,
        "avg_score": data["meta"]["stats"].get("avg", 0),
        "persona_ok": persona_ok,
        "plan_ok": plan_ok,
        "resume_ok": None,
        "passed": passed,
    }


# ---------------------------------------------------------------- 多维视图 + 基线
BASELINE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "baseline.json"
)

# 维度定义：名称、取值方式、达标判定
# 取值返回 None 表示"本用例不适用"，渲染成 — ，不参与达标判定。
DIMENSIONS = [
    ("阶段完整", lambda r: r["phase_recall"], lambda r: r["phase_recall"] >= 0.9),
    ("深挖深度", lambda r: r["deepest_index"] / (len(LADDER) - 1), lambda r: r["depth_ok"]),
    ("话题命中", lambda r: 1.0 if r["topic_hit"] else 0.0, lambda r: r["topic_hit"]),
    ("防死磕", lambda r: 1.0 if r["no_deadlock"] else 0.0, lambda r: r["no_deadlock"]),
    ("不重复", lambda r: 1.0 if r["no_repeat"] else 0.0, lambda r: r["no_repeat"]),
    ("人设生效", lambda r: _tri(r.get("persona_ok")), lambda r: r.get("persona_ok") is not False),
    ("计划覆盖", lambda r: _tri(r.get("plan_ok")), lambda r: r.get("plan_ok") is not False),
    ("简历解析", lambda r: _tri(r.get("resume_ok")), lambda r: r.get("resume_ok") is not False),
]


def _tri(v):
    """三态 → 0/1/None（None 渲染成 —）。"""
    if v is None:
        return None
    return 1.0 if v else 0.0


def render_bars(r: dict, width: int = 5) -> str:
    """把 5 个维度画成一行字块图（ASCII 雷达图的替代）。

    为什么用字块而不是画真正的雷达图？
    因为这是终端工具，字块零依赖、可 diff、能进 CI 日志。
    真要做雷达图，那是 Web 面板该干的事（见 README 扩展方向）。
    """
    cells = []
    for name, getter, ok in DIMENSIONS:
        val = getter(r)
        if val is None:
            cells.append(f"{name}—")
            continue
        ratio = max(0.0, min(1.0, float(val)))
        filled = int(round(ratio * width))
        bar = "█" * filled + "░" * (width - filled)
        mark = "" if ok(r) else "!"
        cells.append(f"{name}{bar}{mark}")
    return " ".join(cells)


def save_baseline(results: list[dict], path: str = BASELINE_PATH) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({r["id"]: r for r in results}, f, ensure_ascii=False, indent=2)
    return path


def load_baseline(path: str = BASELINE_PATH) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def compare_with_baseline(results: list[dict], base: dict,
                          avg_tolerance: float = 0.3) -> list[dict]:
    """相对基线比较：这才是"阈值"该有的形态。

    绝对阈值的问题：mock/LLM 的评分本身有系统性偏差，卡死数字会把
    "评分官变松"误判成"候选人变强"。
    相对基线只回答一个问题：**这次改动有没有让指标退化？**
    """
    rows = []
    for r in results:
        b = base.get(r["id"])
        if not b:
            rows.append({"id": r["id"], "status": "NEW", "notes": ["新增用例"]})
            continue
        notes = []
        if b.get("passed") and not r["passed"]:
            notes.append("PASS→FAIL")
        if r.get("deepest_index", 0) < b.get("deepest_index", 0):
            notes.append(f"深度 {b['deepest_index']}→{r['deepest_index']}")
        if r.get("avg_score", 0) < b.get("avg_score", 0) - avg_tolerance:
            notes.append(f"均分 {b['avg_score']}→{r['avg_score']}")
        for key, label in (("no_repeat", "重复提问"), ("no_deadlock", "死磕"),
                           ("topic_hit", "话题未命中")):
            if b.get(key) and not r.get(key):
                notes.append(label)
        rows.append({
            "id": r["id"],
            "status": "REGRESS" if notes else "SAME",
            "notes": notes,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    ap.add_argument("--save-baseline", action="store_true",
                    help="把本次结果存为基线（用于之后的相对比较）")
    ap.add_argument("--compare", action="store_true",
                    help="与基线比较，检测指标是否退化")
    ap.add_argument("--baseline", default=BASELINE_PATH, help="基线文件路径")
    args = ap.parse_args()

    cases = load_cases()
    results = [evaluate_case(c) for c in cases]

    if args.save_baseline:
        path = save_baseline(results, args.baseline)
        print(f"[基线] 已保存 {len(results)} 条用例到 {path}\n")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("=" * 104)
        print(f"{'ID':<18}{'轮次':<6}{'最深层':<10}{'阶段':<7}{'深度':<6}{'话题':<6}{'防死磕':<7}{'不重复':<7}{'通过'}")
        print("-" * 104)
        for r in results:
            print(
                f"{r['id']:<18}{r['turns']:<6}{r['deepest_layer']:<10}"
                f"{r['phase_recall']:<7}{str(r['depth_ok']):<6}{str(r['topic_hit']):<6}"
                f"{str(r['no_deadlock']):<7}{str(r['no_repeat']):<7}"
                f"{'PASS' if r['passed'] else 'FAIL'}"
            )
        print("-" * 104)
        print("【多维视图】（█ 越高越好；末尾 ! 表示该维度未达标）")
        for r in results:
            print(f"  {r['id']:<18}{render_bars(r)}")
        print("-" * 104)

        regressions = 0
        if args.compare:
            base = load_baseline(args.baseline)
            if not base:
                print(f"[比较] 未找到基线 {args.baseline}，先跑一次 --save-baseline")
            else:
                rows = compare_with_baseline(results, base)
                print("【与基线比较】（相对退化判定，不看绝对分数）")
                for row in rows:
                    tag = {"SAME": "OK  ", "REGRESS": "退化", "NEW": "新增"}[row["status"]]
                    note = ("；".join(row["notes"])) if row["notes"] else "-"
                    print(f"  {row['id']:<18}{tag}  {note}")
                    if row["status"] == "REGRESS":
                        regressions += 1
                print("-" * 104)

        ok = sum(1 for r in results if r["passed"])
        print(f"通过 {ok}/{len(results)}")
        gate = 0.8
        gate_ok = ok / max(1, len(results)) >= gate
        print(f"回归门槛 {int(gate * 100)}%：" + ("PASS" if gate_ok else "FAIL"))
        if args.compare and regressions:
            print(f"相对基线退化：{regressions} 条 → 判定 FAIL")
            return 1
        if args.compare and not gate_ok:
            return 1
        if not args.compare and not gate_ok:
            return 1

    ok = sum(1 for r in results if r["passed"])
    return 0 if ok / max(1, len(results)) >= 0.8 else 1


if __name__ == "__main__":
    sys.exit(main())

"""用真实简历做体检：解析质量 + 追问质量 + 卡在哪。

为什么要有这个工具（而不是靠"我觉得还行"）：
  所有自测都用我们自己写的那份简历，模型当然表现好——因为那份简历
  的写法正好贴合解析规则（## 项目名 / - 技术栈）。换成别人的简历就会露馅：
  中英混排、项目写成段落、技术栈藏在正文里、指标只写"提升明显"。

用法：
  python -m eval.resume_probe                      # 跑 data/test_resumes/ 下全部
  python -m eval.resume_probe --real --turns 10    # 用真实模型跑（慢，但才是真实效果）
  python -m eval.resume_probe --file data/test_resumes/x.md --verbose

输出四块：
  1. 解析层：解析出几个项目、技术栈有没有、指标有没有
  2. 追问层：锚定率 / 承接率 / 到达的最深层级
  3. 覆盖层：五个深挖要素覆盖了几个
  4. 问题清单：能自动判定的缺陷（解析到的"项目"其实是个人信息之类）
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "eval"))

from knowledge import relevance_score, role_topics  # noqa: E402
from llm import MockLLM, build_llm  # noqa: E402
from quality import anchor_score, follow_score  # noqa: E402
from resume import parse_resume_best, resume_quality  # noqa: E402
from session import InterviewSession  # noqa: E402
from state_machine import LADDER  # noqa: E402

RESUME_DIR = os.path.join(ROOT, "data", "test_resumes")

# 解析器常见误判：把联系方式、教育经历当成"项目"
NON_PROJECT_WORDS = ("个人信息", "联系方式", "教育", "技能", "自我评价", "求职意向",
                     "工作经历", "获奖", "证书", "联系方式", "个人简介")

ANSWERS = [
    "这个项目是我独立负责的，目标是解决课程资料查不到的问题，规模大概 300 份课件。",
    "用检索是因为 8k 上下文塞不下这么多资料，也试过先做摘要但会丢公式细节。",
    "实现上按段落切块，块 500 字、重叠 80 字，检索后再加一层重排，接口加了 3 秒超时。",
    "取舍是重排把延迟从 0.9 秒涨到 1.8 秒，但召回从 71% 到 82%，我判断值得。",
    "失效场景是提问用词和原文差太多时召回不到，我们监控空召回率，超 5% 补同义词。",
    "如果重做我会保留重排，砍掉自己写的解析器，那两周投入产出比太低。",
    "第二个项目是二手交易平台，我负责商品检索和订单状态机。",
    "为什么用状态机：订单有 6 个状态、跳转规则复杂，用 if 写会乱。",
    "做法是把状态和事件做成表，非法跳转直接抛错，日志记录状态迁移。",
    "取舍上没用消息队列，因为量级小，引入后运维成本反而高。",
    "边界是并发下单会超卖，我用数据库乐观锁解决，冲突率约 3%。",
    "重做的话会把状态机抽成库，方便复用。",
]


def pick_role(text: str) -> str:
    """按简历内容猜岗位，避免拿"后端简历"去考"Agent 岗"。

    打分方式：话题名命中 + 该话题检索词里的英文技术词命中。
    （不要用 knowledge.relevance_score，那个是给搜索结果打分的，签名是 (result, query)，别搞混。）
    """
    from config import ROLE_KB
    from knowledge import topic_entry

    low = text.lower()
    best, best_score = "general", 0
    for role in ROLE_KB:
        score = 0
        for topic in role_topics(role):
            if topic and topic in text:
                score += 3
            entry = topic_entry(role, topic)
            queries = entry[2] if entry and len(entry) > 2 else []
            for q in queries:
                for tok in q.split():
                    if len(tok) >= 3 and tok.isascii() and tok.lower() in low:
                        score += 1
        if score > best_score:
            best, best_score = role, score
    return best


def _try_llm():
    """拿一个能用的模型来做解析（失败返回 None，退回规则解析）。

    为什么 mock 跑对话时也要用真模型解析：mock 只适合测"对话流程"，
    但脏简历的解析能力只存在于模型里。用规则解析测真实简历，
    得到的永远是"0 个项目"，然后得出"系统坏了"的错误结论。
    """
    try:
        llm = build_llm(mode="auto")
        return None if getattr(llm, "is_mock", False) else llm
    except Exception:
        return None


def probe_one(path: str, real: bool, turns: int, verbose: bool) -> dict:
    name = os.path.basename(path)
    with open(path, encoding="utf-8") as f:
        text = f.read()

    # 体检必须走"最好解析"路径：真实简历（无标题/职责式描述）规则解析常常 0-2 条，
    # 只测规则等于测了个寂寞，真正上线走的是 parse_resume_best。
    # 注意：解析用真模型（否则脏简历一律 0 项目），对话可以仍用 mock 省时间。
    parse_llm = build_llm(mode="auto") if real else _try_llm()
    projects = parse_resume_best(text, llm=parse_llm)
    rq = resume_quality(projects)
    issues: list[str] = []
    if not projects:
        issues.append("没解析出任何项目")
    if len(projects) < 2:
        issues.append(f"只解析出 {len(projects)} 个项目（真实简历通常 2-4 个）")
    for p in projects:
        if not p.stack:
            issues.append(f"项目「{p.name}」没有技术栈")
        if not p.metrics:
            issues.append(f"项目「{p.name}」没有指标（数字）")
        if any(w in p.name for w in NON_PROJECT_WORDS):
            issues.append(f"把「{p.name}」当成了项目（应该是简历区块标题）")

    role = pick_role(text)
    llm = build_llm(mode="auto") if real else MockLLM()
    sess = InterviewSession(
        llm=llm, projects=projects, role_key=role, max_turns=turns,
        verbose=False, save=False, seed=42,
    )
    sess.run_scripted(list(ANSWERS))

    probe_turns = [t for t in sess.turns if t.phase == "PROJECT_PROBE"]
    qs = [t.question for t in probe_turns]
    anchors = sum(anchor_score(q, projects) for q in qs) / max(1, len(qs))
    follows = 0
    for i, t in enumerate(sess.turns):
        if t.phase == "PROJECT_PROBE" and i > 0:
            follows += follow_score(t.question, sess.turns[i - 1].answer)
    follows /= max(1, len(qs))

    deepest = max([t.ladder_level for t in probe_turns], default=0)
    # 注意 1：element_report(project) 需要项目名参数；之前无参调用抛 AttributeError，
    #         被 hasattr 兜住后静默变成 {}，于是"要素覆盖"永远显示 0——典型的假绿。
    # 注意 2：不能把多个项目的报告 update 到同一个 dict（要素名相同会互相覆盖，
    #         最后只剩最后一个项目的结果），必须逐项目计数。
    covered = 0
    handled = 0
    per_project: dict[str, int] = {}
    for p in projects:
        try:
            rep = sess.sm.probe.element_report(p.name)
        except Exception:
            continue
        n = sum(1 for v in rep.values() if "已答到" in str(v))
        h = sum(1 for v in rep.values() if "未问到" not in str(v))
        per_project[p.name] = n
        covered += n
        handled += h

    if deepest < 2:
        issues.append(f"深挖只到「{LADDER[deepest]}」层（说明追问没深入）")
    if anchors < 0.5:
        issues.append(f"锚定率 {anchors:.0%} 偏低（问题没有落在简历事实上）")

    result = {
        "file": name,
        "role": role,
        "projects": [(p.name, len(p.stack), len(p.metrics)) for p in projects],
        "probe_turns": len(probe_turns),
        "anchor": anchors,
        "follow": follows,
        "deepest": LADDER[deepest],
        "covered": covered,
        "handled": handled,
        "covered_per_project": per_project,
        "metrics_ratio": rq["metrics_ratio"],
        "issues": issues,
        "sample_q": qs[:3],
    }
    if verbose:
        print(f"\n--- {name} ---")
        for p in projects:
            print(f"  项目: {p.name} | 栈 {p.stack[:4]} | 指标 {p.metrics[:2]}")
        for q in qs[:5]:
            print(f"  追问: {q[:70]}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None)
    ap.add_argument("--real", action="store_true")
    ap.add_argument("--turns", type=int, default=16)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    files = [args.file] if args.file else sorted(glob.glob(os.path.join(RESUME_DIR, "*.md")))
    if not files:
        print(f"没找到简历：{RESUME_DIR}")
        return 1

    print("=" * 88)
    print(f"真实简历体检｜对话后端：{'真实模型' if args.real else 'mock'}｜轮次 {args.turns}")
    if not args.real:
        print("  注意：mock 对话下「锚定/承接/评分」是模板产物，只能看解析与深挖深度，不能当真实质量。")
    print("=" * 88)
    rows = [probe_one(f, args.real, args.turns, args.verbose) for f in files]

    head = f"{'简历':<22}{'岗位':<12}{'项目':<6}{'指标':<8}{'深挖轮':<8}{'锚定':<8}{'承接':<8}{'最深层':<8}{'要素':<6}"
    print("\n" + head)
    print("-" * 88)
    for r in rows:
        print(f"{r['file'][:20]:<22}{r['role']:<12}{len(r['projects']):<6}{r['metrics_ratio']:<8.0%}{r['probe_turns']:<8}"
              f"{r['anchor']:<8.0%}{r['follow']:<8.0%}{r['deepest']:<8}{str(r['covered'])+'/'+str(r['handled']):<8}")

    print("\n【发现的问题】")
    total = 0
    for r in rows:
        if not r["issues"]:
            continue
        total += len(r["issues"])
        print(f"  {r['file']}：")
        for it in r["issues"]:
            print(f"    - {it}")
    if not total:
        print("  （本批没有自动判定的缺陷）")

    print("\n【追问样例】（判断问得准不准，最终还得看这个）")
    for r in rows:
        print(f"  {r['file']}：")
        for q in r["sample_q"]:
            print(f"    · {q[:76]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

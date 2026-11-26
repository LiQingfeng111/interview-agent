"""CLI 入口。

用法：
  python run.py --mock                     # 零依赖自测（mock 模型）
  python run.py --demo                     # 脚本化跑一整场并打印报告
  python run.py --resume examples/resume_sample.md   # 交互式面试
  python run.py --kb ai_algo               # 查看岗位考点树
  python run.py --kb ai_algo --enrich      # 联网备课并缓存
"""
from __future__ import annotations

import argparse
import os
import sys

from config import EXAMPLES_DIR, LLMConfig
from knowledge import enrich_role, kb_report, resolve_role
from llm import build_llm
from documents import DocumentError, load_document, supported_hint
from resume import load_jd, load_resume, parse_jd, resume_quality
from resume import parse_resume_best as parse_resume
from session import InterviewSession
from store import render_report

DEMO_ANSWERS = [
    "你好。我是 AI 专业大三，做过一个高校课件问答助手，用 RAG 做的；也做过校园二手交易平台，负责后端。最想被深挖的是问答助手。",
    "这个助手是给同学查课程资料用的，覆盖 300 份课件，我独立做的，从数据清洗到接口都写了。",
    "因为 300 份课件塞不进上下文，所以先用向量库检索再拼上下文。也考虑过把全文先做摘要，但摘要把公式细节丢了，所以最后选检索。",
    "切分按标题和段落做，500 字一块、重叠 80 字；嵌入用通用模型，检索之后加了一层重排。",
    "重排让延迟从 0.9 秒涨到 1.8 秒，但召回从 71% 提到 82%，我觉得这个交换值得，因为用户更在意答案对不对。",
    "失效主要是提问用词和课件差异太大时召回不到。我监控空召回率，超过 5% 就补同义词表；课件更新后索引不同步也会失效。",
    "重做的话会保留检索加重排，砍掉自己写的 PDF 解析器换成现成库，因为那个解析器我修了两周，投入产出比太低。",
    "第二个项目是二手交易平台，我负责商品检索和订单状态机，用 Redis 缓存热点商品，QPS 从 120 提到 900。",
    "并发下单冲突是用数据库乐观锁加深层重试解决的，代价是冲突高时重试次数会涨，我加了重试上限和降级提示。",
    "失效场景是缓存和库不一致，我用延迟双删加短 TTL 兜底，同时监控不一致告警。",
    "重做会把订单状态机改成事件驱动，因为现在的状态流转用 if 判断，加了三种状态之后已经很难维护了。",
    "技术方面我了解限流用令牌桶，也了解召回率要用带标注的测试集测，不能凭感觉。",
    "我踩过参数幻觉的坑，模型传了不存在的字段，用 schema 校验后失败率从 11% 降到 3%。",
    "缓存一致性我倾向于先更新库再删缓存，因为反过来的话并发读会把旧值写回缓存。",
    "数据库索引我注意过最左前缀和回表代价，慢查询会先看执行计划。",
    "评估方面我搭了 20 条回归用例，用固定 seed 跑，指标变化超过阈值才允许合并。",
    "我想问实习生一般会独立负责一个模块吗，以及团队怎么做代码评审。",
    "暂时没有了，谢谢。",
]


def _maybe_export_otel(session, args) -> None:
    """把本场事件流导出成 OTLP JSON（接 Langfuse/Phoenix 用）。"""
    if not getattr(args, "otel", False):
        return
    try:
        from otel_export import build_otlp
        import json as _json
        import os as _os

        otlp = build_otlp(session.ledger)
        out = _os.path.join("runs", f"trace_{session.ledger.session_id}.json")
        _os.makedirs("runs", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            _json.dump(otlp, f, ensure_ascii=False, indent=2)
        spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
        print(f"[otel] 已导出 {len(spans)} 个 span → {out}")
    except Exception as e:
        print(f"[otel] 导出失败（不影响面试结果）：{type(e).__name__}: {e}")


def _maybe_export_chart(session, args) -> None:
    """导出雷达图：优先在线 API 出 PNG，失败自动降级成本地 SVG + 单页 HTML。"""
    if not getattr(args, "chart", False):
        return
    try:
        from chart import local_svg, report_html, write_chart_files

        data = session.result().to_dict()
        meta = data.get("meta", {}) or {}
        dims = (meta.get("stats") or {}).get("dims") or {}
        if not dims:
            print("[chart] 本次没有评分维度，跳过出图")
            return
        stem = f"radar_{session.ledger.session_id}" if session.ledger else "radar"
        out = write_chart_files(dims, outdir="reports", stem=stem)
        html_path = os.path.join("reports", f"{stem}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(report_html(dims, meta.get("stats") or {},
                                (meta.get("audit") or {}),
                                title=f"面试报告 · {session.ledger.session_id}"
                                if session.ledger else "面试报告"))
        print(f"[chart] 图源={out['source']} · PNG={out['png'] or '（降级，未联网）'}")
        print(f"[chart] SVG={out['svg']} · HTML={html_path}")
    except Exception as e:
        print(f"[chart] 出图失败（不影响面试结果）：{type(e).__name__}: {e}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="模拟面试 Agent")
    ap.add_argument("--mock", action="store_true", help="强制使用确定性 mock 模型（离线）")
    ap.add_argument("--llm", default="auto", choices=["auto", "local", "remote", "mock"],
                    help="模型来源：auto(先本地后远程) / local / remote / mock")
    ap.add_argument("--model", default=None, help="模型名，如 qwen2.5:1.5b（auto 时自动选）")
    ap.add_argument("--base-url", default=None,
                    help="OpenAI 兼容端点，如 http://127.0.0.1:11434/v1")
    ap.add_argument("--show-llm", action="store_true",
                    help="打印每次模型调用的耗时与回复前 300 字（排障用）")
    ap.add_argument("--demo", action="store_true", help="脚本化跑完整场并输出报告")
    ap.add_argument("--resume", default=None, help="简历文本路径（.md/.txt）")
    ap.add_argument("--role", default="agent_app", help="岗位：ai_algo/backend/agent_app/general")
    ap.add_argument("--persona", default="balanced",
                    choices=["balanced", "strict", "warm", "academic", "engineering", "efficient"],
                    help="面试官风格（对标主流开源项目的六种风格）")
    ap.add_argument("--jd", default=None, help="目标岗位 JD 文本路径（.md/.txt），会据此调整考点")
    ap.add_argument("--plan", action="store_true",
                    help="面试前先生成问题计划（LLM 读简历，代码校验覆盖）")
    ap.add_argument("--resume-file", default=None,
                    help="直接读简历文件（PDF/DOCX/MD/TXT），免去先转 markdown")
    ap.add_argument("--coach", action="store_true",
                    help="评估模式：每轮给评分理由 + 缺什么 + 示范回答（多一次模型调用）")
    ap.add_argument("--chart", action="store_true",
                    help="导出报告图：雷达图 PNG（在线 API）+ SVG + 单页 HTML")
    ap.add_argument("--otel", action="store_true",
                    help="面试结束把事件流导出成 OTLP JSON（可接 Langfuse/Phoenix）")
    ap.add_argument("--audit", action="store_true",
                    help="跑完打印审计区块（事件数/不变量/证据/成本），并落盘事件流")
    ap.add_argument("--replay", default=None,
                    help="把一场事件流折叠成快照并校验链完整性")
    ap.add_argument("--web", action="store_true",
                    help="开启运行时联网：追问参考 + 回答事实核对（失败自动降级，不中断面试）")
    ap.add_argument("--history", action="store_true", help="查看历史与趋势后退出")
    ap.add_argument("--prompt-version", default="default",
                    help="提问官 prompt 版本（prompts/interviewer_<v>.txt，default=当前默认）")
    ap.add_argument("--max-turns", type=int, default=30)
    ap.add_argument("--kb", default=None, help="打印岗位考点树")
    ap.add_argument("--enrich", action="store_true", help="联网备课并缓存知识库")
    ap.add_argument("--save", action="store_true", default=True)
    args = ap.parse_args(argv)

    # 重放：把事件流读回来折叠快照（不重跑面试、不花 token）
    if args.replay:
        from ledger import load_ledger
        from verifier import REASON_CN

        ledger = load_ledger(args.replay)
        snap = ledger.fold()
        problems = ledger.verify_chain()
        print("=" * 62)
        print(f"重放 · {args.replay} · 会话 {snap['session_id']}")
        print("=" * 62)
        print(f"  轮次 {snap['turns']} · 升层 {snap['level_ups']} · 换项目 {snap['switches']}")
        print(f"  结束原因 {snap['finish_reason']}（{REASON_CN.get(snap['finish_reason'], '-')}）")
        print(f"  违规 {len(snap['violations'])} 条 · token {snap['tokens']} · ¥{snap['cost_cny']:.4f}")
        print(f"  事件链：{'完整 ✓' if not problems else '异常 ' + problems[0]}")
        print("=" * 62)
        return 0 if not problems else 1

    # 历史与趋势
    if args.history:
        from history import load_runs, render_history

        print("=" * 78)
        print("历史记录与趋势")
        print("=" * 78)
        print(render_history(load_runs()))
        return 0

    # 知识库查看 / 备课
    if args.kb:
        key = resolve_role(args.kb)
        if args.enrich:
            print("[备课] 正在联网检索…")
            data = enrich_role(key, use_search=True)
            print(f"[备课] 已缓存 {len(data.get('topics', {}))} 个话题资料\n")
        print(kb_report(key))
        return 0

    # 模型选择：auto 先探本地，再远程，最后才降级 mock
    try:
        llm = build_llm(
            mock=args.mock,
            mode=args.llm,
            base_url=args.base_url,
            model=args.model,
            echo=args.show_llm,
        )
    except Exception as e:
        print(f"[LLM] {e}")
        print("[LLM] 已降级到 mock 模式（仅验证流程，不代表真实效果）\n")
        llm = build_llm(mock=True)
    use_mock = bool(getattr(llm, "is_mock", False))
    if use_mock and not args.mock:
        print("[提示] 当前为 mock 模式：回复是模板生成的，不是真实模型输出。\n")
    resume_path = args.resume
    if resume_path is None:
        # demo / mock 也要有简历，否则项目集为空、深挖阶段直接跳空
        resume_path = EXAMPLES_DIR + "/resume_sample.md"

    # 支持直接喂 PDF/DOCX：真实用户手里基本不是 markdown
    if args.resume_file:
        try:
            with open(args.resume_file, "rb") as f:
                doc = load_document(f.read(), args.resume_file)
        except OSError as e:
            print(f"[错误] 读不到简历文件：{e}")
            return 1
        except DocumentError as e:
            print(f"[错误] {e}")
            print(f"       {supported_hint()}")
            return 1
        text = doc["text"]
        print(f"[简历] {args.resume_file} · {doc.get('chars', len(text))} 字 · 引擎 {doc.get('engine', '-')}")
    else:
        text = load_resume(resume_path)
    projects = parse_resume(text, llm=None if use_mock else llm)
    if not projects:
        print("[警告] 未解析出项目，将使用空项目集（面试官会让你现场介绍）。")
    elif args.resume_file:
        rq = resume_quality(projects)
        print(f"[简历] 项目 {[p.name[:16] for p in projects]} · 指标覆盖 {rq['metrics_ratio']:.0%}")
        if rq["advice"]:
            print(f"[简历] ! {rq['advice']}")

    jd_info = parse_jd(load_jd(args.jd))
    if jd_info["keywords"]:
        print(f"[JD] 已读取岗位要求，重点技术：{'、'.join(jd_info['keywords'][:8])}\n")

    session = InterviewSession(
        llm=llm,
        projects=projects,
        role_key=args.role,
        max_turns=args.max_turns,
        verbose=not args.demo,
        save=args.save,
        persona=args.persona,
        jd_keywords=jd_info["keywords"],
        use_plan=args.plan,
        coach=getattr(args, "coach", False),
        audit=args.audit or args.otel,
        prompt_version=args.prompt_version,
        web=args.web,
    )

    if args.plan:
        from planner import plan_summary

        print("[计划] 面试前问题计划：")
        print(plan_summary(session.plan))
        if session.plan_issues:
            print("[计划] 覆盖校验发现问题：")
            for it in session.plan_issues:
                print("  - " + it)
        else:
            print("[计划] 覆盖校验通过（每个项目都覆盖到，要素齐全）")
        print()

    # 只有 --demo 才脚本化跑完；--mock 只是换后端，仍然可以交互练习。
    # （原来 --mock 也走 demo，导致没配 Key 的同学只能看固定剧本，没法自己练。）
    if args.demo:
        session.verbose = False
        result = session.run_scripted(DEMO_ANSWERS)
        if getattr(args, "coach", False):
            from coach import render_card
            for c in session.cards:
                print(render_card(c))
                print()
        _maybe_export_chart(session, args)
        print(render_report(result.to_dict()))
        _maybe_export_otel(session, args)
        return 0

    # 交互式
    print("=" * 66)
    print("模拟面试开始")
    print("  · 直接输入你的回答后回车，面试会继续")
    print("  · /help 看指令   /report 看当前报告   /quit 结束")
    print("=" * 66)
    session.start()
    _print_progress(session)
    while True:
        try:
            ans = input(f"\n[第 {session.sm.total_turns + 1}/{session.sm.max_turns} 轮] 你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[提示] 检测到输入中断（Ctrl+Z / 关窗口 / Ctrl+C），面试提前结束。")
            print("       如果想完整跑完，请重新运行并持续输入回答；或直接跑 1-run-demo.bat 看完整流程。")
            break
        low = ans.lower()
        # 想停就停：心态不好、有事、或者就想看看报告，都不该被"必须答完"绑住
        if low in ("/end", "end", "/结束", "结束", "/stop", "stop"):
            print("[结束] 已按你的要求结束，正在生成报告…")
            session.end_early("user_quit")
            _maybe_export_chart(session, args)
            print(render_report(session.result().to_dict()))
            _maybe_export_otel(session, args)
            return 0
        if low in ("exit", "quit", "/quit", "退出"):
            print("[提示] 面试提前结束。")
            break
            _maybe_export_otel(session, args)
            print(render_report(session.result().to_dict()))
            continue
        if low in ("/help", "help", "?"):
            print(_HELP_TEXT)
            continue
        if low in ("/skip", "skip"):
            ans = "（这题我不会，先跳过）"
        if not ans:
            print("[提示] 你没输入内容。请输入回答，或 /quit 结束、/help 看指令。")
            continue
        session.step(ans)
        if session.sm.phase == "FEEDBACK":
            print("\n[提示] 面试已走完所有阶段，正常结束。")
            break
        _print_progress(session)
    print("\n" + "-" * 66)
    print("本场已保存到 runs/ 目录，用 python run.py --history 看趋势。")
    return 0


_HELP_TEXT = """可用指令：
  /help    看这份说明
  /report  打印当前进度下的报告（不会结束面试）
  /end     想停就停：立刻结束并出完整报告（结束原因记为"本人提前结束"）
  /skip    这题跳过（会记为未答出，影响短板诊断）
  /quit    结束面试并出报告
其余任何输入都会被当作你的回答。"""


def _print_progress(session) -> None:
    """打印一行阶段进度。

    为什么需要：原来打完一题，用户不知道现在到哪一步、还剩几轮，
    只能盲打。真实面试里候选人是知道"现在在深挖环节"的，这点信息差会
    直接影响他的答法（自我介绍时该铺多少细节）。
    """
    from config import PHASE_CONFIG

    sm = session.sm
    order = list(PHASE_CONFIG.keys())
    step_no = order.index(sm.phase) + 1 if sm.phase in order else 0
    total_turns = getattr(sm, "max_turns", 0) or 0
    done = getattr(sm, "total_turns", 0)

    parts = [f"阶段 {step_no}/{len(order)}", PHASE_CONFIG.get(sm.phase, {}).get("desc", sm.phase)]
    if sm.phase == "PROJECT_PROBE":
        parts.append(f"深挖第 {sm.probe.ladder_level} 层「{sm.ladder_name}」")
        if sm.probe.current:
            parts.append(sm.probe.current)
    if total_turns:
        parts.append(f"第 {done}/{total_turns} 轮")
    print("\n" + " · ".join(parts))


if __name__ == "__main__":
    sys.exit(main())

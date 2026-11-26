"""生成一份"演示快照"HTML：真实跑一场，把对话、点评卡、报告、雷达图排进一页。

为什么要这个：光给数字看不出效果，截图又要人工点半天。
这个脚本用真实模型跑 4 轮（开评估模式），把：
  · 每轮问题/回答/评分
  · 评估卡片（为什么扣分 / 缺什么 / 示范回答）
  · 最终报告 + 雷达图（内嵌 SVG，离线可看）
渲染成一个静态 HTML，直接浏览器打开或用 headless 截图。

用法：python tools/demo_snapshot.py [--mock]
"""
from __future__ import annotations

import argparse
import html
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from chart import local_svg  # noqa: E402
from llm import build_llm  # noqa: E402
from resume import parse_resume_best  # noqa: E402
from session import InterviewSession  # noqa: E402
from store import render_report  # noqa: E402

ANSWERS = [
    "目标是把同学找答案的时间从十几分钟压到 1 分钟内，语料是 300 份课程 PDF。",
    "自建 120 条问答对做测试集，人工标注答案所在 chunk，按 recall@5 统计；"
    "基线是全文塞上下文只有 71%，加检索 82%，再加重排到 86%。",
    "重排让延迟从 0.9 秒涨到 1.8 秒，我觉得值得：答错一次要重新问，成本 30 秒以上。",
    "数据翻十倍先崩在内存索引，现在 1.2G 会变 12G，我会先做分片和按课程路由。",
]


def card_html(card: dict) -> str:
    if not card or not card.get("enabled"):
        return ""
    gaps = "".join(f"<li>{html.escape(g)}</li>" for g in (card.get("gaps") or []))
    ideal = card.get("ideal") or "（本次模型未返回示范回答，规则理由仍然可用）"
    tips = " / ".join(card.get("tips") or [])
    v = html.escape(str(card.get("verdict") or ""))
    return f"""
  <div class="card">
    <div class="cardhead"><span class="badge v-{v}">{v} {card.get('score')}</span>
      <span>最弱：{html.escape(str(card.get('weakest') or '-'))}</span>
      <span class="src">来源：{html.escape(str(card.get('source') or 'rule'))}</span></div>
    <div><b>为什么</b>{html.escape(card.get('why') or '')}</div>
    {f'<div><b>缺什么</b><ul>{gaps}</ul></div>' if gaps else ''}
    <div class="ideal"><b>示范回答</b><div>{html.escape(ideal)}</div></div>
    {f'<div class="hint">怎么改：{html.escape(tips)}</div>' if tips else ''}
  </div>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "reports", "demo.html"))
    args = ap.parse_args()

    llm = build_llm(mode="mock" if args.mock else "auto")
    resume_text = open(os.path.join(ROOT, "data", "test_resumes", "our_sample.md"),
                       encoding="utf-8").read()
    projects = parse_resume_best(resume_text, llm=None if args.mock else llm)
    sess = InterviewSession(llm=llm, projects=projects, role_key="ai_algo",
                            max_turns=8, verbose=False, save=False, seed=11,
                            coach=True, audit=True)
    sess.start()
    for a in ANSWERS:
        sess.step(a)

    report = render_report(sess.result().to_dict())
    dims = (sess.result().to_dict().get("meta", {}).get("stats") or {}).get("dims") or {}
    svg = local_svg(dims, title="五维能力雷达")

    turns_html = []
    for i, t in enumerate(sess.turns, 1):
        ev = t.evaluation
        meta = f"{ev.verdict} · {round(ev.avg, 2) if hasattr(ev, 'avg') else ''}" if ev else ""
        card = sess.cards[i - 1] if i - 1 < len(sess.cards) else {}
        turns_html.append(f"""
  <div class="turn"><div class="q"><b>Q{i}</b> {html.escape(t.question)}</div>
    <div class="a"><b>你答</b> {html.escape(t.answer)}</div>
    <div class="meta">{html.escape(meta)}</div>{card_html(card)}</div>""")

    page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>模拟面试 Agent · 效果演示</title><style>
 body{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#f5f7fb;color:#2b3440;margin:0;padding:24px}}
 .wrap{{max-width:1080px;margin:0 auto}}
 h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#6b7688;font-size:13px;margin-bottom:18px}}
 .grid{{display:grid;grid-template-columns:1fr 380px;gap:18px;align-items:start}}
 .panel{{background:#fff;border-radius:14px;padding:18px 20px;box-shadow:0 2px 14px rgba(20,30,50,.06)}}
 .turn{{margin-bottom:16px;padding-bottom:14px;border-bottom:1px dashed #e6eaf1}}
 .q{{font-size:13.5px;line-height:1.6}} .a{{font-size:13px;color:#5a6678;margin-top:4px;line-height:1.6}}
 .q b,.a b{{color:#4e79a7;margin-right:6px;font-weight:600}}
 .meta{{font-size:12px;color:#8b95a5;margin-top:4px}}
 .card{{border:1px solid #e6eaf1;border-radius:10px;padding:10px 12px;margin-top:10px;background:#fbfcfe;font-size:13px;line-height:1.65}}
 .cardhead{{display:flex;gap:10px;align-items:center;font-size:12px;color:#6b7688;margin-bottom:6px}}
 .badge{{font-weight:600;padding:1px 8px;border-radius:999px;font-size:11px;border:1px solid}}
 .v-pass{{color:#1f9d55;border-color:#1f9d55}} .v-partial{{color:#b7791f;border-color:#b7791f}}
 .v-fail{{color:#c53030;border-color:#c53030}}
 .card b{{color:#4e79a7;margin-right:6px}} .card ul{{margin:4px 0 4px 18px;padding:0}}
 .ideal{{border-left:3px solid #4e79a7;background:rgba(78,121,167,.06);border-radius:4px;padding:8px 10px;margin-top:6px}}
 .hint{{font-size:12px;color:#8b95a5;margin-top:6px}}
 pre{{white-space:pre-wrap;font-family:ui-monospace,Consolas,monospace;font-size:12.5px;line-height:1.7;margin:0}}
</style></head><body><div class="wrap">
<h1>模拟面试 Agent · 效果演示</h1>
<div class="sub">真实模型 · 4 轮 · 开启评估模式 · 同一场数据同时出「对话 / 点评卡 / 报告 / 雷达图」</div>
<div class="grid">
  <div class="panel">{''.join(turns_html)}</div>
  <div>
    <div class="panel" style="text-align:center;margin-bottom:18px">{svg}</div>
    <div class="panel"><pre>{html.escape(report)}</pre></div>
  </div>
</div></div></body></html>"""
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"已生成 {args.out}（{len(page)} 字符）")
    print(f"轮次 {len(sess.turns)} · 卡片 {len(sess.cards)} · 维度 {dims}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

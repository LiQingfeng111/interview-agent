"""把一个请求从进来到出去的全过程打印出来（给完全不了解这个项目的人看）。

不解释代码，只展示"你在浏览器点一下，后端做了哪几步、回了什么"。
用法：python tools/walkthrough.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import persist  # noqa: E402
import webui  # noqa: E402


def step(title: str) -> None:
    print()
    print("─" * 74)
    print(title)
    print("─" * 74)


def main() -> int:
    webui.persist.DB_PATH = os.path.join(tempfile.mkdtemp(), "walk.db")
    persist.DB_PATH = webui.persist.DB_PATH

    step("① 浏览器 POST /api/start  →  开一场面试")
    payload = {"role": "ai_algo", "mock": True, "user": "demo",
               "resume": open(os.path.join(ROOT, "data", "test_resumes", "our_sample.md"),
                              encoding="utf-8").read()}
    print("请求体（截断）：", json.dumps({k: (v[:40] + "…" if isinstance(v, str) and len(v) > 40 else v)
                                    for k, v in payload.items()}, ensure_ascii=False))
    started = webui.api_start(payload)
    print("\n后端返回：")
    for k in ("sid", "question", "progress"):
        print(f"  {k}: {json.dumps(started.get(k), ensure_ascii=False)[:150]}")

    sid = started["sid"]
    step("② 浏览器 POST /api/answer  →  答一轮")
    ans = webui.api_answer({"sid": sid, "user": "demo",
                            "answer": "目标是把找答案时间压到 1 分钟内，语料 300 份 PDF，召回 71%。"})
    print("后端返回的键：", list(ans))
    print("  下一问：", str(ans.get("question"))[:90])
    print("  进度：", json.dumps(ans.get("progress"), ensure_ascii=False)[:160])
    if ans.get("card"):
        c = ans["card"]
        print(f"  点评卡：{c.get('verdict')} {c.get('score')} · 来源 {c.get('source')}")
        print("    为什么：", str(c.get("why"))[:100])

    step("③ 这一轮后端内部发生了什么（事件账本，按顺序）")
    sess = webui.SESSIONS[sid]["session"]
    for ev in sess.ledger.events[-10:]:
        print(f"  {ev.seq:>3} {ev.type:<22} {json.dumps(ev.payload, ensure_ascii=False)[:90]}")

    step("④ 浏览器 GET /api/report  →  出报告")
    rep = webui.api_report(sid)
    print("报告长度：", len(rep.get("report") or ""), "字")
    print("五维：", json.dumps(rep.get("dims"), ensure_ascii=False))
    print("\n报告开头：")
    for line in (rep.get("report") or "").splitlines()[:14]:
        print("   ", line)

    step("⑤ 数据落到哪里")
    print("  会话库：", persist.DB_PATH)
    print("  事件流：", getattr(sess.ledger, "path", "(未落盘)"))
    print("  库里的会话：", [(r["sid"][-8:], r["user"], r["turns"])
                        for r in persist.list_sessions(path=persist.DB_PATH)])
    print()
    print("结论：所谓「后台」，就是上面这四步背后的 Python 函数；")
    print("      黑窗口里那行 'Starting web UI on http://127.0.0.1:8770' 就是它们的入口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

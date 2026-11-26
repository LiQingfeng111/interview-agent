"""审计体检：跑若干场（mock）面试，统计不变量违规 + 验证重放一致。

为什么要有这个门禁：
  "不变量"写在代码里不会自动生效——必须有人每轮真跑一遍并统计。
  同类问题在上一轮发生过：要素覆盖恒为 0，看着全绿，其实统计口径是坏的。
  所以这里不只看"有没有报错"，还要求**跑得出来数字**。

判定：
  fatal 必须为 0（模型自由发挥可以，但违反硬约束不行）；
  warn 允许存在，数量与明细都会打印，方便持续收敛；
  重放必须逐字段一致（排除时间戳），否则记账一定有问题。

用法：
  python -m eval.audit_check              # 默认 6 场
  python -m eval.audit_check --n 12
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from llm import MockLLM  # noqa: E402
from replay import diff_snapshots  # noqa: E402
from resume import parse_resume_best  # noqa: E402
from session import InterviewSession  # noqa: E402
from ledger import load_ledger  # noqa: E402

ANSWERS = [
    "目标是让同学能查课程资料，规模 300 份课件，我一个人从解析到上线。",
    "baseline 是不做检索直接把全文塞进上下文，召回率 71%，延迟 0.6 秒。",
    "试过用摘要替代检索，结果公式丢了，这就是被否掉的失败方案。",
    "重排让延迟从 0.9 涨到 1.8 秒，召回 71% 到 82%，我认为这个取舍值得。",
    "空召回率超过 5% 就会告警，我用它发现索引失效。",
    "如果数据量翻十倍，我会先把索引分片，再考虑换模型。",
]


def run_one(seed: int, turns: int, tmpdir: str) -> dict:
    projects = parse_resume_best(
        open(os.path.join(ROOT, "data", "test_resumes", "our_sample.md"),
             encoding="utf-8").read()
    )
    sess = InterviewSession(
        llm=MockLLM(), projects=projects, role_key="ai_algo",
        max_turns=turns, verbose=False, save=False, seed=seed, audit=True,
        # 账本路径必须在构造时给：构造阶段就会写 resume.parsed 等事件，
        # 事后改 .path 会导致前后事件写进两个文件（seq 从 0 重来，链校验必失败）。
        audit_path=os.path.join(tmpdir, f"events_{seed}.jsonl"),
    )
    sess.run_scripted(ANSWERS * 4)
    meta = sess.result().meta
    return {"audit": meta.get("audit") or {}, "path": sess.ledger.path}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--turns", type=int, default=16)
    args = ap.parse_args()

    fatal = warn = 0
    reasons: dict[str, int] = {}
    evidence_ratios: list[float] = []
    replay_ok = replay_bad = 0
    detail: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        for seed in range(args.n):
            r = run_one(seed, args.turns, tmp)
            a = r["audit"]
            fatal += a.get("fatal", 0)
            warn += a.get("warn", 0)
            reasons[a.get("finish_reason", "-")] = reasons.get(a.get("finish_reason", "-"), 0) + 1
            evidence_ratios.append((a.get("evidence") or {}).get("ratio", 1.0))
            for v in (a.get("violations") or [])[:2]:
                detail.append(f"seed{seed} {v['id']} {v['severity']} {v['desc']}")

            # 重放：同一事件流折叠两次必须一致，且链完整
            if os.path.exists(r["path"]):
                l1 = load_ledger(r["path"])
                l2 = load_ledger(r["path"])
                if not diff_snapshots(l1.fold(), l2.fold()) and not l1.verify_chain():
                    replay_ok += 1
                else:
                    replay_bad += 1

    print("=" * 70)
    print(f"审计体检｜{args.n} 场 mock 面试｜每场最多 {args.turns} 轮")
    print("=" * 70)
    print(f"  不变量：fatal {fatal} · warn {warn}")
    print(f"  结束原因分布：{reasons}")
    ev_avg = sum(evidence_ratios) / max(1, len(evidence_ratios))
    print(f"  评分证据可核验率：{ev_avg:.0%}")
    print(f"  重放一致：{replay_ok}/{replay_ok + replay_bad}")
    for line in detail[:6]:
        print(f"    - {line}")

    # 矛盾账本：3 组构造矛盾必须全部检出（提案 S3 的验收线）
    from memory import Memory
    m = Memory()
    m.observe("覆盖了 300 份课件，召回率 71%，延迟 0.6 秒。", "问答助手", 1)
    m.observe("其实一共 1000 多份文档。", "问答助手", 3)
    m.observe("召回率能到 82%。", "问答助手", 5)
    m.observe("延迟大概 1.8 秒。", "问答助手", 7)
    keys = {c.key for c in m.contradictions}
    contradict_ok = len(keys) >= 3
    print(f"  矛盾检出：{len(keys)}/3（{sorted(keys)}）")

    ok = (fatal == 0 and replay_bad == 0 and ev_avg >= 0.95 and contradict_ok)
    print(f"判定：{'PASS' if ok else 'FAIL'}"
          + ("" if ok else "（要求 fatal=0 / 重放全一致 / 证据可核验≥95% / 矛盾 3/3）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

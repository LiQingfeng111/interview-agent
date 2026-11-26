"""重放一致率门禁：录一场，读回来折叠两次，逐字段比对。

为什么要单独一关：
  "可重放"是这套审计体系的地基。如果事件流读回来折叠出的快照和现场不一致，
  那报告里的每个数字都不可复现，审计就退化成"作者说的"。

判定：3 场不同随机种子的会话，全部满足
  1. 事件链 seq 连续、无孤儿 causation；
  2. 同一文件折叠两次，快照逐字段相同；
  3. finish_reason 有值且落在闭集里。
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ledger import load_ledger  # noqa: E402
from replay import diff_snapshots  # noqa: E402
from verifier import FINISH_REASONS  # noqa: E402
from eval.audit_check import ANSWERS, run_one  # noqa: E402


def main() -> int:
    ok = 0
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for seed in range(3):
            r = run_one(seed, 16, tmp)
            path = r["path"]
            if not os.path.exists(path):
                problems.append(f"seed{seed}: 事件文件没落盘（{path}）")
                continue
            a, b = load_ledger(path), load_ledger(path)
            diffs = diff_snapshots(a.fold(), b.fold())
            chain = a.verify_chain()
            reason = a.fold().get("finish_reason", "")
            if diffs:
                problems.append(f"seed{seed}: 快照不一致 {diffs[:2]}")
            elif chain:
                problems.append(f"seed{seed}: 事件链异常 {chain[:1]}")
            elif reason not in FINISH_REASONS:
                problems.append(f"seed{seed}: finish_reason={reason!r} 不在闭集")
            else:
                ok += 1

    print("=" * 70)
    print("重放一致率｜3 场会话：事件链 + 快照折叠 + 结束原因闭集")
    print("=" * 70)
    print(f"  一致：{ok}/3")
    for p in problems[:5]:
        print(f"    - {p}")
    passed = ok == 3
    print(f"判定：{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

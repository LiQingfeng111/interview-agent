"""一键质量门禁：单测 + 评估回归 + 部署体检 + 门槛判定。

为什么需要：
  之前"跑测试"是三个 .bat 加一堆手敲命令，靠人记得跑。
  凡是靠人记得的事，早晚会漏。把判定收敛成一个命令、一个退出码，
  CI 才有地方接，别人接手也知道"合格"的标准是什么。

用法：
  python check.py            # 全量：单测 + 评估 + 基线比较
  python check.py --fast     # 只跑单测（改小东西时用）
退出码：0 全绿，1 有不达标项。
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def _run(title: str, args: list[str]) -> int:
    print("=" * 70)
    print(title)
    print("=" * 70)
    proc = subprocess.run([PY] + args, cwd=ROOT)
    print()
    return proc.returncode


def main() -> int:
    fast = "--fast" in sys.argv
    results: list[tuple[str, int]] = []

    results.append(("单元测试", _run("1/7 单元测试", ["-m", "unittest", "discover", "-s", "tests"])))
    if fast:
        print("（--fast 跳过评估、题库与审计检查）")
    else:
        results.append(("评估回归", _run("2/7 评估回归（与基线比较）",
                                        [os.path.join("eval", "run_eval.py"), "--compare"])))
        results.append(("知识库备课", _run("3/7 知识库备课质量",
                                        [os.path.join("eval", "check_kb.py"), "--cached"])))
        results.append(("技术题库", _run("4/7 题库健康检查（题量/覆盖/时效）",
                                        [os.path.join("eval", "check_bank.py")])))
        # 审计三关：不变量 fatal=0、重放逐字段一致、评分尺子对得上标注。
        # 这三关的意义是——报告里写的"可审计"不是形容词，是通过判定的结论。
        results.append(("不变量体检", _run("5/7 不变量体检（6 场 mock：fatal 必须为 0）",
                                        ["-m", "eval.audit_check", "--n", "6"])))
        results.append(("审计重放", _run("6/7 重放一致率（事件流折叠两次比对）",
                                        ["-m", "eval.audit_replay"])))
        results.append(("评分校准", _run("7/8 评分校准（20 条标注，mock 一致率 ≥70%）",
                                        ["-m", "eval.calibrate", "--file", os.path.join("data", "calibration", "labeled_v2.jsonl")])))
        results.append(("部署体检", _run("8/9 部署路径体检（Dockerfile 静态 + 容器参数启动）",
                                        [os.path.join("tools", "verify_deploy.py")])))
        results.append(("Docker路径", _run("9/9 Docker 路径体检（WSL → dockerd → 容器 → HTTP）",
                                          [os.path.join("tools", "verify_docker_path.py")])))

    print("=" * 70)
    failed = [name for name, code in results if code != 0]
    for name, code in results:
        print(f"  {'PASS' if code == 0 else 'FAIL'}  {name}")
    print("=" * 70)
    if failed:
        print(f"门禁：FAIL（{len(failed)} 项不通过）")
        return 1
    print("门禁：PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())

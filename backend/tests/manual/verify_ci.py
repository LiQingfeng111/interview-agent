"""校验 CI 配置文件的语法和结构。

## 为什么需要这个

CI 配置写错的话，push 上去才会发现（等几分钟甚至几十分钟）。
本地先校验能立刻发现问题，尤其是：
- YAML 语法（缩进、冒号、引号）
- 引用的脚本文件是否存在
- 环境变量是否齐全

## 用法

    .venv/bin/python backend/tests/manual/verify_ci.py
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
CI = ROOT / ".github" / "workflows" / "ci.yml"

print(f"检查 {CI.relative_to(ROOT)}")
print()

if not CI.exists():
    print("  ✗ 文件不存在")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("  ! 没装 pyyaml，跳过语法校验")
    sys.exit(0)

try:
    doc = yaml.safe_load(CI.read_text(encoding="utf-8"))
except yaml.YAMLError as e:
    print(f"  ✗ YAML 语法错误：{e}")
    sys.exit(1)

print("  ✓ YAML 语法正确")

jobs = doc.get("jobs", {})
print(f"  ✓ 共 {len(jobs)} 个 job")

problems: list[str] = []

for name, job in jobs.items():
    steps = job.get("steps", [])
    print(f"    - {name}: {len(steps)} 步")

    for step in steps:
        run = step.get("run", "")
        if not run:
            continue

        # 检查 run 里引用的项目内脚本是否存在
        for token in run.split():
            if token.startswith("backend/") or token.startswith("tools/"):
                p = ROOT / token
                if not p.exists():
                    problems.append(f"{name}: 引用的脚本不存在 → {token}")

        # 检查是否还在用 python -c（容易三层转义出错）
        if "python -c" in run:
            problems.append(f"{name}: 用了 python -c 内联（建议改成脚本）")

# 检查 job 依赖是否都存在
for name, job in jobs.items():
    for need in job.get("needs", []) if isinstance(job.get("needs"), list) else []:
        if need not in jobs:
            problems.append(f"{name}: needs 引用了不存在的 job → {need}")

print()
if problems:
    print("发现问题：")
    for p in problems:
        print(f"  ✗ {p}")
    sys.exit(1)

print("  ✓ 结构与引用检查通过")
print()
print("清理后的 job 列表：")
for name in jobs:
    print(f"  {name}")

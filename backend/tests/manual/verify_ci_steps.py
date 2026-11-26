"""验证 CI 里的两个关键步骤：建表 + 启动自检。

CI 里这两步是用 `python -c` 内联写的。内联脚本容易因为引号/转义
在 GitHub Actions 的 YAML 里出错，所以先在这里跑通逻辑，
再决定 CI 里怎么写（内联还是调脚本）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

print("=== 步骤 1: init_schema ===")
from app.repos.pg import init_schema

r = init_schema()
print(f"  执行 {r['statements']} 条语句")

print()
print("=== 步骤 2: 启动自检 ===")
from app.core import selfcheck

result = selfcheck.run(strict=True)
print(f"  通过：{result['checked_modules']} 个模块 + 数据库")
print(f"  ok = {result['ok']}")

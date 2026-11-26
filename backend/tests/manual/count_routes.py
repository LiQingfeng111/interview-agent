"""从源码统计 API 路由（不依赖服务运行）。"""

import pathlib
import re

base = pathlib.Path("/mnt/e/桌面/实习项目/interview_agent/backend/app/api")
total = 0
by_file = {}

for f in sorted(base.glob("*.py")):
    if f.name in ("__init__.py",):
        continue
    src = f.read_text(encoding="utf-8")
    hits = re.findall(r'@router\.(get|post|put|delete|patch)\(\s*"([^"]*)"', src)
    if hits:
        by_file[f.stem] = hits
        total += len(hits)

print(f"总端点数: {total}")
print()
for name, hits in by_file.items():
    print(f"  {name}.py  ({len(hits)})")
    for m, p in hits:
        print(f"    {m.upper():7} {p}")

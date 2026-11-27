import ast
import pathlib
import sys

p = pathlib.Path("/mnt/e/桌面/实习项目/interview_agent/backend/app/core/service.py")
try:
    ast.parse(p.read_text(encoding="utf-8"), str(p))
    print("  语法 OK")
except SyntaxError as e:
    print(f"  FAIL 行{e.lineno}: {e.msg}")
    sys.exit(1)

src = p.read_text(encoding="utf-8")
for fn in ("_round_highlight", "_smart_truncate", "_attach_cost",
           "_report_from_turns"):
    n = src.count(f"def {fn}")
    print(f"  {fn}: 定义 {n} 次" + ("  ← 重复！" if n > 1 else ""))

import ast
import pathlib
import sys

root = pathlib.Path("/mnt/e/桌面/实习项目/interview_agent")
files = [
    "backend/app/core/runtime.py",
    "backend/app/core/service.py",
    "backend/app/graph/interview_graph.py",
]
bad = 0
for f in files:
    p = root / f
    try:
        ast.parse(p.read_text(encoding="utf-8"), str(p))
        print(f"  ok   {f}")
    except SyntaxError as e:
        print(f"  FAIL {f}:{e.lineno} {e.msg}")
        bad += 1
sys.exit(1 if bad else 0)

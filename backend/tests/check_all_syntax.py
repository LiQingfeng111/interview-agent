import ast
import pathlib
import sys

base = pathlib.Path("/mnt/e/桌面/实习项目/interview_agent/backend/app")
files = [
    "core/service.py",
    "core/cost.py",
    "core/selfcheck.py",
    "repos/documents.py",
    "api/speech.py",
    "api/stream.py",
    "rag/boost.py",
    "rag/memory.py",
    "graph/interview_graph.py",
    "graph/checkpointer.py",
]
bad = 0
for f in files:
    p = base / f
    if not p.exists():
        print(f"  缺失 {f}")
        bad += 1
        continue
    try:
        ast.parse(p.read_text(encoding="utf-8"), str(p))
        print(f"  ok   {f}")
    except SyntaxError as e:
        print(f"  FAIL {f}:{e.lineno} {e.msg}")
        bad += 1
print()
print("全部通过" if bad == 0 else f"{bad} 个文件有问题")
sys.exit(1 if bad else 0)

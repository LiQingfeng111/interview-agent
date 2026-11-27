"""打印真实会话的「逐轮记录」，检查截断效果。

用法：
    PYTHONPATH='backend:.' .venv/bin/python backend/tests/manual/show_turns.py <session_id>
"""

import os
import sys

ROOT = "/mnt/e/桌面/实习项目/interview_agent"
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/backend")
os.environ.setdefault("DATABASE_URL", "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("IA_REDIS", "0")
os.environ.setdefault("IA_JWT_SECRET", "x" * 32)

import logging
logging.basicConfig(level=logging.ERROR)

from app.core.service import _report_from_turns  # noqa: E402
from app.rag.memory import _load_turns  # noqa: E402

sid = sys.argv[1] if len(sys.argv) > 1 else ""
if not sid:
    print("用法: python show_turns.py <session_id>")
    sys.exit(1)

turns = _load_turns(sid)
print(f"读到 {len(turns)} 轮")

# 只显示长问答的轮次（短的不涉及截断）
long_turns = [t for t in turns if len(str(t.get("question") or "")) > 100]
print(f"其中长问题（>100字）: {len(long_turns)} 轮")

r = _report_from_turns(turns)
md = r.get("markdown") or ""

start = md.find("## 逐轮记录")
seg = md[start:] if start >= 0 else md

print()
print("=" * 64)
print("逐轮记录（实际输出，只看有截断的）")
print("=" * 64)
for line in seg.split("\n")[:40]:
    print(line)
print("=" * 64)

# 检查
import re
issues = []
for line in seg.split("\n"):
    if line.startswith("**问**：") or line.startswith("**答**："):
        body = line.split("：", 1)[1].strip()
        if len(body) > 50 and not body.endswith(("。", "！", "？", "；", "，", "…")):
            issues.append(body[-20:])

print()
if issues:
    print(f"✗ 有 {len(issues)} 处被切在半句：")
    for i in issues[:3]:
        print(f"    ...{i!r}")
else:
    print("✓ 所有截断点都在标点处")

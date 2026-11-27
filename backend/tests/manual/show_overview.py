"""打印真实会话的「表现概览」段，验证截断效果。

用法：
    PYTHONPATH='backend:.' .venv/bin/python backend/tests/manual/show_overview.py <session_id>
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
    print("用法: python show_overview.py <session_id>")
    sys.exit(1)

turns = _load_turns(sid)
print(f"读到 {len(turns)} 轮")

r = _report_from_turns(turns)
md = r.get("markdown") or ""

start = md.find("## 表现概览")
end = md.find("## 逐轮记录")
seg = md[start:end] if start >= 0 and end > start else md[:800]

print()
print("=" * 60)
print("表现概览（实际输出）")
print("=" * 60)
for line in seg.split("\n"):
    print(line)
print("=" * 60)

# 检查是否还有"孤立单字"截断
import re
bad = re.findall(r"— ([^—\n]{1,3})$", seg, flags=re.M)
if bad:
    print(f"\n! 疑似还有孤立短截断: {bad}")
else:
    print("\n✓ 没有孤立短截断")

# 检查是否包含回答
if "答：" in seg:
    print("✓ 包含候选人回答")
else:
    print("! 没有回答内容")

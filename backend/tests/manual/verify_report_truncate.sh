#!/usr/bin/env bash
# 用真实会话验证报告截断效果
set -u
BASE=http://127.0.0.1:8080

echo "=== 0) 确保后端在跑 ==="
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$BASE/api/health")
echo "  /api/health -> $code"
[ "$code" != "200" ] && { echo "  后端没起来"; exit 1; }

echo ""
echo "=== 1) 找一个有多轮记录的会话 ==="
SID=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT session_id FROM turns GROUP BY session_id
   HAVING COUNT(*) >= 3 ORDER BY MAX(created_at) DESC LIMIT 1" 2>/dev/null | tr -d ' ')
if [ -z "$SID" ]; then
  echo "  没有可用会话，先跑一场"
  exit 1
fi
echo "  会话: $SID"

OWNER=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT owner FROM sessions WHERE id='$SID'" 2>/dev/null | tr -d ' ')
echo "  属于: $OWNER"

echo ""
echo "=== 2) 该会话的 turns ==="
docker exec ia-pg psql -U ia -d interview_agent -c \
  "SELECT seq, verdict, ROUND(score::numeric,2) AS score,
          LEFT(question, 30) AS q
   FROM turns WHERE session_id='$SID' ORDER BY seq LIMIT 5" 2>/dev/null | head -10

echo ""
echo "=== 3) 直接调 _report_from_turns 看输出 ==="
PYTHONPATH='backend:.' .venv/bin/python - "$SID" <<'PYEOF' 2>&1 | grep -vE "^INFO|^WARNING"
import sys
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent")
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent/backend")
import os
os.environ.setdefault("DATABASE_URL", "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("IA_REDIS", "0")

from app.core.service import _report_from_turns
from app.rag.memory import _load_turns

sid = sys.argv[1]
turns = _load_turns(sid)
print(f"  读到 {len(turns)} 轮")

r = _report_from_turns(turns)
md = r.get("markdown") or ""

# 只打印"表现概览"那一段
start = md.find("## 表现概览")
end = md.find("## 逐轮记录")
if start >= 0:
    seg = md[start:end if end > start else start + 600]
    print()
    print("  " + "=" * 56)
    print("  表现概览（实际输出）")
    print("  " + "=" * 56)
    for line in seg.split("\n"):
        print("  " + line)
else:
    print("  ! 没找到表现概览段")
    print(md[:400])
PYEOF

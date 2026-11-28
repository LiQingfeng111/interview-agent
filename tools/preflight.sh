#!/usr/bin/env bash
# 完整环境自检：所有依赖 + 服务 + 数据
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
cd "$PROJ" || exit 1

FAIL=0
ok()   { echo "  ✓ $1"; }
bad()  { echo "  ✗ $1"; FAIL=$((FAIL+1)); }
warn() { echo "  ~ $1"; }

echo "================================================================"
echo "  1. 基础设施"
echo "================================================================"

# PG
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
  N=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
    "SELECT COUNT(*) FROM bank_questions" 2>/dev/null | tr -d ' ')
  ok "PostgreSQL（题库 ${N:-0} 题）"
else
  echo "  PG 未运行，启动…"
  bash ops.sh db 2>&1 | grep -E '✓|✗' | head -2
  N=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
    "SELECT COUNT(*) FROM bank_questions" 2>/dev/null | tr -d ' ')
  [ "${N:-0}" -gt 0 ] && ok "PostgreSQL（题库 $N 题）" || bad "PostgreSQL 起不来"
fi

# Ollama
GW=$(ip route show default 2>/dev/null | awk '{print $3}')
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
  "http://$GW:11800/api/tags" 2>/dev/null || echo 000)
if [ "$CODE" = "200" ]; then
  ok "Ollama（$GW:11800）"
else
  bad "Ollama 不可达（$CODE）"
fi

# 模型网关
KEY=$(grep -E '^LLM_API_KEY=' .env 2>/dev/null | cut -d= -f2- | tr -d '"' | head -c 12)
BASE=$(grep -E '^LLM_BASE_URL=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')
if [ -n "$BASE" ]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
    "${BASE%/}/models" -H "Authorization: Bearer $(grep -E '^LLM_API_KEY=' .env | cut -d= -f2- | tr -d '\"')" 2>/dev/null || echo 000)
  [ "$CODE" = "200" ] && ok "模型网关（$BASE）" || bad "模型网关不可达（$CODE）"
else
  bad "未配置 LLM_BASE_URL"
fi

echo ""
echo "================================================================"
echo "  2. Python 依赖"
echo "================================================================"

.venv/bin/python - <<'PY'
import sys
mods = {
    "fastapi": "Web 框架",
    "psycopg": "PostgreSQL 驱动",
    "langgraph": "状态编排",
    "langchain_core": "LangChain",
    "rank_bm25": "BM25",
    "jieba": "中文分词",
    "torch": "rerank 基础",
    "sentence_transformers": "cross-encoder",
    "pymupdf": "PDF 解析",
    "zipfile": "DOCX 解析（标准库，docx 本质是 zip）",
}
missing = []
for m, desc in mods.items():
    try:
        __import__(m)
        print(f"  ✓ {m:<24} {desc}")
    except ImportError:
        print(f"  ✗ {m:<24} {desc}")
        missing.append(m)
sys.exit(1 if missing else 0)
PY
[ $? -ne 0 ] && FAIL=$((FAIL+1))

echo ""
echo "================================================================"
echo "  3. 数据完整性"
echo "================================================================"

.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent")
sys.path.insert(0, "/mnt/e/桌面/实习项目/interview_agent/backend")
os.environ.setdefault("DATABASE_URL", "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("IA_REDIS", "0")
os.environ.setdefault("IA_JWT_SECRET", "x" * 32)
import logging
logging.basicConfig(level=logging.CRITICAL)

from app.repos.pg import get_conn

checks = [
    ("题库有向量", "SELECT COUNT(*) FROM bank_questions WHERE embedding IS NOT NULL", 700),
    ("对话 chunk 有向量", "SELECT COUNT(*) FROM doc_chunks WHERE embedding IS NOT NULL", 1),
    ("历史问答有向量", "SELECT COUNT(*) FROM turns WHERE embedding IS NOT NULL", 1),
    ("有话题标签", "SELECT COUNT(DISTINCT topic) FROM bank_questions WHERE topic <> ''", 30),
    ("有默认账号", "SELECT COUNT(*) FROM users WHERE username='demo'", 1),
]
bad = 0
with get_conn() as c:
    cur = c.cursor()
    for name, sql, expect in checks:
        cur.execute(sql)
        n = cur.fetchone()[0]
        if n >= expect:
            print(f"  ✓ {name:<22} {n}")
        else:
            print(f"  ✗ {name:<22} {n}（期望 ≥{expect}）")
            bad += 1
    cur.close()
sys.exit(1 if bad else 0)
PY
[ $? -ne 0 ] && FAIL=$((FAIL+1))

echo ""
echo "================================================================"
if [ $FAIL -eq 0 ]; then
  echo "  全部就绪，可以跑端到端"
else
  echo "  $FAIL 项未通过"
fi
echo "================================================================"
exit $FAIL

#!/usr/bin/env bash
# 确保 PG + Ollama 就绪，然后跑实验
#
# ## 为什么需要这个包装脚本
#
# 这台机器上 Docker 容器会被意外回收（会话回收、WSL 重启等），
# 每次跑实验前都要手动 `bash ops.sh db`，已经发生过 3 次。
# 把"确保依赖就绪"固化到脚本里。
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
cd "$PROJ" || exit 1

echo "=== 确保依赖就绪 ==="

# PG
if docker exec ia-pg pg_isready -U ia -d interview_agent >/dev/null 2>&1; then
  echo "  ✓ PostgreSQL 已就绪"
else
  echo "  PG 未运行，启动…"
  bash ops.sh db 2>&1 | grep -E '✓|✗' | head -2
fi

# 题库
N=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT COUNT(*) FROM bank_questions" 2>/dev/null | tr -d ' ')
echo "  题库: ${N:-0} 题"

# Ollama
GW=$(ip route show default 2>/dev/null | awk '{print $3}')
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
  "http://$GW:11800/api/tags" 2>/dev/null || echo 000)
if [ "$code" = "200" ]; then
  echo "  ✓ Ollama 就绪（$GW:11800）"
else
  echo "  ✗ Ollama 不可达（$code）—— 请在 Windows 侧启动"
  exit 1
fi

echo ""
echo "=== 跑实验 ==="
export PYTHONPATH='backend:.'
export DATABASE_URL='postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent'
export OLLAMA_BASE_URL="http://$GW:11800"
export IA_REDIS=0

exec .venv/bin/python "${1:-backend/tests/manual/exp_rerank_final.py}" 2>&1 \
  | grep -vE '^INFO|Building prefix|Loading model from|Prefix dict|预计算|完成（|WARNING:psycopg'

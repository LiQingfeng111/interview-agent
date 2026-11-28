#!/usr/bin/env bash
# 跑诊断脚本前先确保 PG/Ollama/API 就绪
#
# ## 为什么需要它
#
# 这台机器上容器和进程会被定期回收（每几十小时一次）。
# 每次跑脚本都手动 `ops.sh db` 太低效，而且会让人误判成"代码坏了"。
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
cd "$PROJ" || exit 1

ensure_pg() {
  if docker exec ia-pg pg_isready -U ia -d interview_agent >/dev/null 2>&1; then
    return 0
  fi
  echo "  [自愈] 启动 PostgreSQL…"
  bash ops.sh db 2>&1 | grep -E '✓|✗' | head -2
}

ensure_ollama() {
  GW=$(ip route show default 2>/dev/null | awk '{print $3}')
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://$GW:11800/api/tags" 2>/dev/null || echo 000)
  if [ "$CODE" != "200" ]; then
    echo "  [警告] Ollama 不可达（$CODE）—— 向量相关功能会失败"
  fi
  echo "$GW"
}

echo "=== 依赖自愈 ==="
ensure_pg
GW=$(ensure_ollama)
echo "  网关: $GW"

echo ""
echo "=== 执行: $* ==="
export PYTHONPATH='backend:.'
export DATABASE_URL='postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent'
export OLLAMA_BASE_URL="http://$GW:11800"
export IA_REDIS=0

exec .venv/bin/python "$@" 2>&1 | grep -vE '^INFO|^WARNING|Is the server running'

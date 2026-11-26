#!/usr/bin/env bash
# 环境自愈：确保 PG / Ollama / 端口转发都在
#
# 为什么需要：
# - Docker 容器会被意外回收（会话回收、手动 down）
# - Ollama 是 Windows 进程，重启就没了
# - 端口转发同理
#
# 三者任一缺失，后端要么拒绝启动（自检拦 PG），要么功能降级
# （embedding 失败 / 模型连不上）。每次手动查太累，固化成脚本。
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
cd "$PROJ" || exit 1

ok()   { echo "  ✓ $*"; }
warn() { echo "  ⚠ $*"; }
bad()  { echo "  ✗ $*"; }

echo "=== 1) PostgreSQL ==="
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
  ok "ia-pg 运行中"
else
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
    docker start ia-pg >/dev/null 2>&1 && ok "已启动（复用现有容器）"
  else
    docker run -d --name ia-pg \
      -e POSTGRES_USER=ia -e POSTGRES_PASSWORD=ia_dev_pwd \
      -e POSTGRES_DB=interview_agent \
      -p 5432:5432 -v ia_pgdata:/var/lib/postgresql/data \
      pgvector/pgvector:pg16 >/dev/null 2>&1 && ok "已创建并启动"
  fi
  for i in $(seq 1 30); do
    if docker exec ia-pg pg_isready -U ia -d interview_agent >/dev/null 2>&1; then
      ok "就绪（${i}s）"
      break
    fi
    sleep 1
  done
fi

echo ""
echo "=== 2) Ollama（Windows 侧，需 0.0.0.0:11800）==="
HOST_GW=$(ip route show default | awk '{print $3}')
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST_GW:11800/api/tags" 2>/dev/null || echo 000)
if [ "$code" = "200" ]; then
  ok "$HOST_GW:11800 可达"
else
  bad "$HOST_GW:11800 不可达（$code）"
  warn "请在 Windows PowerShell 执行："
  warn "  powershell -ExecutionPolicy Bypass -File tools\\host_services.ps1"
fi

echo ""
echo "=== 3) 模型网关端口转发（18336）==="
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST_GW:18336/v1/models" 2>/dev/null || echo 000)
if [ "$code" = "200" ] || [ "$code" = "401" ]; then
  ok "$HOST_GW:18336 可达（$code）"
else
  bad "$HOST_GW:18336 不可达（$code）"
  warn "请在 Windows 执行：node tools\\port_forward.cjs 18336 18326"
fi

echo ""
echo "=== 4) LLM_API_KEY ==="
KEY=$(grep -E '^LLM_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
if [ -n "$KEY" ]; then
  ok "已配置（${#KEY} 字符）"
else
  bad "未配置 —— 模型调用会 401"
fi

echo ""
echo "=== 5) 题库 ==="
N=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT COUNT(*) || '/' || COUNT(embedding) FROM bank_questions" 2>/dev/null || echo "?")
if [ "$N" = "?" ]; then
  bad "查不到（数据库可能还没初始化，跑 bash ops.sh schema && bash ops.sh bank）"
else
  ok "总题数/已向量化 = $N"
fi

echo ""
echo "=== 结论 ==="
if [ "$code" = "200" ] || [ "$code" = "401" ]; then
  echo "  可以启动后端：bash ops.sh api"
else
  echo "  先按上面提示修好宿主机服务，再启动后端"
fi

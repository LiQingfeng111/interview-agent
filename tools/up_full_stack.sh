#!/usr/bin/env bash
# Docker 全栈一键启动（自动探测宿主机服务地址）
#
# 解决的核心问题：
# `host.docker.internal` 在 WSL2 + Docker Engine 组合下不通，
# 必须用 WSL 默认网关 IP（能路由到 Windows 宿主机上的 Ollama / 模型网关）。
# 这个脚本自动探测并把结果写进 .env，避免手工试地址。
set -u

PROJ=/mnt/e/桌面/实习项目/interview_agent
BUILD_DIR=$HOME/ia-build

echo "=========== 1) 探测宿主机地址 ==========="
HOST_GW=$(ip route show default | awk '{print $3}')
echo "  WSL 默认网关: $HOST_GW"

# 验证这个地址能不能访问 Ollama
OLLAMA_OK=0
if curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST_GW:11800/api/tags" 2>/dev/null | grep -q 200; then
  echo "  ✓ $HOST_GW:11800 可达（Ollama）"
  OLLAMA_OK=1
else
  echo "  ✗ $HOST_GW:11800 不可达"
  echo "    请先在 Windows PowerShell 启动 Ollama："
  echo "      \$env:OLLAMA_HOST='0.0.0.0:11800'; & \"\$env:LOCALAPPDATA\\Programs\\Ollama\\ollama.exe\" serve"
fi

# 验证模型网关转发
GW_OK=0
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://$HOST_GW:18336/v1/models" 2>/dev/null || echo 000)
if [ "$code" = "200" ] || [ "$code" = "401" ]; then
  echo "  ✓ $HOST_GW:18336 可达（模型网关转发）"
  GW_OK=1
else
  echo "  ✗ $HOST_GW:18336 不可达（$code）"
  echo "    请在 Windows PowerShell 启动端口转发："
  echo "      node tools/port_forward.cjs 18336 18326"
fi

echo ""
echo "=========== 2) 写 .env（docker compose 会读）==========="
# 保留已有的 LLM_API_KEY：这个 key 是手工从 relay 配置里取的，
# 脚本重新生成 .env 时如果丢掉它，模型调用会 401
# （实测踩过：跑完本脚本后所有模型请求都 "missing or invalid relay key"）。
EXISTING_KEY=""
if [ -f "$PROJ/.env" ]; then
  EXISTING_KEY=$(grep -E '^LLM_API_KEY=' "$PROJ/.env" 2>/dev/null | cut -d= -f2- || true)
fi
if [ -z "$EXISTING_KEY" ]; then
  echo "  ⚠ .env 里没有 LLM_API_KEY，模型调用会失败（401）"
  echo "    请手工加一行：LLM_API_KEY=<relay key>"
fi

cat > "$PROJ/.env" <<EOF
# 由 ops.sh up-full 自动生成
POSTGRES_PASSWORD=ia_dev_pwd
IA_JWT_SECRET=dev-secret-for-local-testing-only-please-change

# 宿主机服务地址（自动探测：WSL 默认网关）
OLLAMA_BASE_URL=http://$HOST_GW:11800
LLM_BASE_URL=http://$HOST_GW:18336/v1
LLM_MODEL=lobsterai/deepseek-flash
LLM_API_KEY=$EXISTING_KEY
EOF
echo "  已写 $PROJ/.env（OLLAMA=$HOST_GW:11800，key ${#EXISTING_KEY} 字符）"

echo ""
echo "=========== 3) 同步到构建目录 ==========="
mkdir -p "$BUILD_DIR"
cp "$PROJ/.env" "$BUILD_DIR/.env"
cp "$PROJ/docker-compose.yml" "$BUILD_DIR/"
echo "  已同步"

echo ""
echo "=========== 4) 启动全栈 ==========="
cd "$BUILD_DIR" || exit 1
docker compose up -d 2>&1 | tail -8

echo ""
echo "=========== 5) 等待健康 ==========="
for i in $(seq 1 60); do
  BE=$(docker inspect --format='{{.State.Health.Status}}' ia-backend 2>/dev/null || echo none)
  if [ "$BE" = "healthy" ]; then
    echo "  ✓ 后端 healthy（${i}0s 内）"
    break
  fi
  sleep 3
done

echo ""
echo "=========== 6) 最终状态 ==========="
docker ps --format '  {{.Names}} | {{.Status}}' | grep "^  ia-"
echo ""
curl -s --max-time 15 http://127.0.0.1:8080/api/health | head -c 600
echo ""
echo ""
echo "  前端: http://localhost:8081"
echo "  后端: http://localhost:8080/docs"

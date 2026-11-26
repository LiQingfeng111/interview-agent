#!/usr/bin/env bash
# Docker 全栈启动验证
set -u
DST=$HOME/ia-build
cd "$DST" || exit 1

echo "=========== 1) 启动全栈 ==========="
docker compose up -d 2>&1 | tail -15

echo ""
echo "=========== 2) 等待健康 ==========="
for i in $(seq 1 60); do
  PG=$(docker inspect --format='{{.State.Health.Status}}' ia-build-postgres-1 2>/dev/null || echo "none")
  BE=$(docker inspect --format='{{.State.Health.Status}}' ia-build-backend-1 2>/dev/null || echo "none")
  echo "  [${i}s] pg=$PG backend=$BE"
  if [ "$PG" = "healthy" ] && [ "$BE" = "healthy" ]; then
    echo "  全部就绪"
    break
  fi
  sleep 2
done

echo ""
echo "=========== 3) 容器状态 ==========="
docker compose ps 2>&1 | head -10

echo ""
echo "=========== 4) 后端健康检查（容器内 → Docker 网络）==========="
docker exec ia-build-backend-1 curl -s --max-time 10 http://localhost:8080/api/health 2>&1 | head -c 600
echo ""

echo ""
echo "=========== 5) 前端可访问性 ==========="
curl -s -o /dev/null -w "  前端 :8081 → %{http_code}\n" --max-time 10 http://127.0.0.1:8081/ 2>&1
curl -s -o /dev/null -w "  后端 :8080 → %{http_code}\n" --max-time 10 http://127.0.0.1:8080/api/health 2>&1

echo ""
echo "=========== 6) 前端代理后端（关键）==========="
curl -s --max-time 15 http://127.0.0.1:8081/api/health 2>&1 | head -c 400
echo ""

echo ""
echo "=========== 7) 各容器日志尾部 ==========="
for c in ia-build-postgres-1 ia-build-redis-1 ia-build-backend-1 ia-build-frontend-1; do
  echo "  --- $c ---"
  docker logs --tail 3 "$c" 2>&1 | sed 's/^/    /' | head -4
done

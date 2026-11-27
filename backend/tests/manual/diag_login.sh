#!/usr/bin/env bash
# 诊断登录失败：直接打接口，看返回什么
set -u
BASE=http://127.0.0.1:8080

echo "=== 1) 后端在运行吗 ==="
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$BASE/api/health")
echo "  /api/health -> $code"

echo ""
echo "=== 2) 注册一个全新账号 ==="
U="login_test_$RANDOM"
cat > /tmp/reg.json <<EOF
{"user":"$U","password":"test123456"}
EOF
echo "  用户名: $U"
curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  --data-binary @/tmp/reg.json | head -c 300
echo ""

echo ""
echo "=== 3) 用这个新账号登录 ==="
cat > /tmp/login.json <<EOF
{"user":"$U","password":"test123456"}
EOF
RESP=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  --data-binary @/tmp/login.json)
echo "  响应: $(echo "$RESP" | head -c 400)"
TOKEN=$(echo "$RESP" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
echo "  token 长度: ${#TOKEN}"

echo ""
echo "=== 4) 如果拿到 token，验证能访问受保护接口 ==="
if [ -n "$TOKEN" ]; then
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/auth/me" \
    -H "Authorization: Bearer $TOKEN")
  echo "  /api/auth/me -> $code"
else
  echo "  ⚠ 没拿到 token，登录本身失败"
fi

echo ""
echo "=== 5) 数据库里有哪些用户 ==="
docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT username, role, created_at::date FROM users ORDER BY created_at DESC LIMIT 8" 2>/dev/null

echo ""
echo "=== 6) 前端能否访问后端（关键：vite 代理）==="
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://127.0.0.1:5173/api/health")
echo "  经前端代理 /api/health -> $code"

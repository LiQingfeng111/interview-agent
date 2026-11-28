#!/usr/bin/env bash
# 快速探测：注册/登录/健康 三个基本接口是否正常
set -u
BASE=http://127.0.0.1:8080

echo "=== health ==="
curl -s -o /dev/null -w "  %{http_code}\n" --max-time 8 "$BASE/api/health"

echo ""
echo "=== register ==="
U="probe_$(date +%H%M%S)_$RANDOM"
printf '{"user":"%s","password":"probe123456"}' "$U" > /tmp/p_reg.json
curl -s -X POST "$BASE/api/auth/register" \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/p_reg.json --max-time 25 | head -c 250
echo ""

echo ""
echo "=== login ==="
curl -s -X POST "$BASE/api/auth/login" \
  -H 'Content-Type: application/json' \
  --data-binary @/tmp/p_reg.json --max-time 25 > /tmp/p_login.json
head -c 250 /tmp/p_login.json
echo ""
TOKEN=$(grep -o '"access_token":"[^"]*"' /tmp/p_login.json | cut -d'"' -f4)
echo "  token 长度: ${#TOKEN}"

echo ""
echo "=== auth/me ==="
if [ -n "$TOKEN" ]; then
  curl -s -o /dev/null -w "  %{http_code}\n" --max-time 12 \
    "$BASE/api/auth/me" -H "Authorization: Bearer $TOKEN"
fi

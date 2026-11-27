#!/usr/bin/env bash
# 验证：删掉 demo 账号 → 重启后端 → 账号自动重建
#
# 这是"账号一直在"的终极验证：不靠备份，靠代码。
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
cd "$PROJ" || exit 1

echo "=== 1) 删掉 demo 账号 ==="
docker exec ia-pg psql -U ia -d interview_agent -c \
  "DELETE FROM users WHERE username='demo'" 2>/dev/null
LEFT=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT COUNT(*) FROM users WHERE username='demo'" 2>/dev/null | tr -d ' ')
echo "  剩余 demo 账号数: $LEFT"

echo ""
echo "=== 2) 重启后端（触发启动自检）==="
bash ops.sh api 2>&1 | grep -E "就绪|自检失败" | head -2

echo ""
echo "=== 3) 检查日志里的默认账号动作 ==="
grep -E "默认账号" /tmp/fastapi.log 2>/dev/null | tail -3

echo ""
echo "=== 4) 账号是否重建 ==="
N=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT COUNT(*) FROM users WHERE username='demo'" 2>/dev/null | tr -d ' ')
if [ "$N" = "1" ]; then
  echo "  ✓ demo 账号已自动重建"
else
  echo "  ✗ demo 账号不存在（数量 $N）"
fi

echo ""
echo "=== 5) 实际登录验证 ==="
cat > /tmp/bv_login.json <<'EOF'
{"user":"demo","password":"demo123456"}
EOF
RESP=$(curl -s -X POST http://127.0.0.1:8080/api/auth/login \
  -H 'Content-Type: application/json' --data-binary @/tmp/bv_login.json)
TOKEN=$(echo "$RESP" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
if [ -n "$TOKEN" ]; then
  echo "  ✓ demo / demo123456 登录成功（token ${#TOKEN} 字符）"
  ROLE=$(echo "$RESP" | grep -o '"role":"[^"]*"' | cut -d'"' -f4)
  echo "  角色: $ROLE"
else
  echo "  ✗ 登录失败: $(echo "$RESP" | head -c 200)"
fi

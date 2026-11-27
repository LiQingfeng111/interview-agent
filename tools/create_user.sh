#!/usr/bin/env bash
# 创建/重置一个可用的演示账号
#
# ## 为什么需要这个脚本
#
# 调试过程中我多次 `docker rm -f ia-pg` / `compose down -v`，
# 数据卷被删，用户表清空 —— 用户之前注册的账号就没了。
# 这个脚本用来快速恢复一个可用账号。
#
# ## 用法
#
#   bash tools/create_user.sh                    # 默认 demo / demo123456
#   bash tools/create_user.sh alice alice123456  # 自定义
set -u
BASE=${BASE:-http://127.0.0.1:8080}
USER_NAME=${1:-demo}
USER_PASS=${2:-demo123456}

echo "=== 目标账号: $USER_NAME ==="

# 先试登录（已存在且密码对就直接可用）
cat > /tmp/cu_login.json <<EOF
{"user":"$USER_NAME","password":"$USER_PASS"}
EOF
LOGIN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  --data-binary @/tmp/cu_login.json)
TOKEN=$(echo "$LOGIN" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -n "$TOKEN" ]; then
  echo "  ✓ 账号已存在且密码正确，可直接登录"
  echo "    用户名: $USER_NAME"
  echo "    密码:   $USER_PASS"
  exit 0
fi

# 登录失败 → 试注册
echo "  登录失败，尝试注册…"
cat > /tmp/cu_reg.json <<EOF
{"user":"$USER_NAME","password":"$USER_PASS"}
EOF
REG=$(curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  --data-binary @/tmp/cu_reg.json)
echo "  注册响应: $(echo "$REG" | head -c 200)"

# 注册后立刻登录验证
LOGIN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  --data-binary @/tmp/cu_login.json)
TOKEN=$(echo "$LOGIN" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -n "$TOKEN" ]; then
  echo ""
  echo "  ✓ 可用"
  echo "    用户名: $USER_NAME"
  echo "    密码:   $USER_PASS"
else
  echo ""
  echo "  ✗ 仍无法登录，响应："
  echo "    $LOGIN"
  exit 1
fi

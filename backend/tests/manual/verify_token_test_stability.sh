#!/usr/bin/env bash
# 验证 test_tampered_token_rejected 的稳定性
#
# ## 为什么要连跑多次
#
# JWT 每次 create_access 都会生成新的 iat/exp，签名字节不同。
# 原来的测试"改最后一个字符"，而 base64url 末位有填充位，
# 某些取值解码后字节相同 → 验签通过 → 测试偶发失败。
#
# 本地跑一两次过不代表稳定；CI 上跑一次就撞上了。
# 所以这里连跑 30 次，必须全过。
set -u
cd /mnt/e/桌面/实习项目/interview_agent || exit 1
export PYTHONPATH='backend:.'
export IA_JWT_SECRET='test-secret-for-unit-tests-only-32bytes'

PASS=0
FAIL=0
for i in $(seq 1 30); do
  if .venv/bin/python -m pytest \
      backend/tests/test_auth.py::test_tampered_token_rejected -q \
      >/dev/null 2>&1; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
    echo "  第 $i 次失败"
  fi
done

echo ""
echo "结果：$PASS 通过 / $FAIL 失败（共 30 次）"
if [ "$FAIL" -eq 0 ]; then
  echo "✓ 稳定"
  exit 0
else
  echo "✗ 不稳定，仍有概率性问题"
  exit 1
fi

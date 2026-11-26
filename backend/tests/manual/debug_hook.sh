#!/usr/bin/env bash
# 调试 pre-commit 检查：为什么假密钥没被拦
set -u
cd /mnt/e/桌面/实习项目/interview_agent || exit 1

echo "=== 1) 造测试文件并暂存 ==="
echo 'LLM_API_KEY=ofm-FAKEFAKEFAKEFAKEFAKE123456' > hooktest.tmp
git add -f hooktest.tmp
echo "  已暂存"

echo ""
echo "=== 2) 暂存区的新增行（脚本读的就是这个）==="
git diff --cached -U0 | head -10

echo ""
echo "=== 3) 危险文件名检查 ==="
DANG=$(git diff --cached --name-only --diff-filter=ACM | grep -E '(^|/)\.env$|\.key$|\.pem$|secret|credential' || true)
echo "  匹配到: [${DANG}]"

echo ""
echo "=== 4) 内容模式检查 ==="
ADDED=$(git diff --cached -U0 | grep -E '^\+' | grep -v '^\+\+\+' || true)
echo "  ADDED 行数: $(echo "$ADDED" | grep -c . || echo 0)"
echo "  ADDED 内容: [${ADDED}]"

echo ""
echo "  测试 ofm- 模式:"
echo "$ADDED" | grep -oE 'ofm-[A-Za-z0-9_-]{20,}' | head -2 || echo "    没匹配到"

echo ""
echo "=== 5) 真实 key 前缀比对 ==="
if [ -f .env ]; then
  REALKEY=$(grep -E '^LLM_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
  echo "  .env 里的 key 长度: ${#REALKEY}"
  if [ "${#REALKEY}" -ge 16 ]; then
    PREFIX=${REALKEY:0:16}
    echo "  前缀: $PREFIX"
    if echo "$ADDED" | grep -qF "$PREFIX"; then
      echo "  → 匹配到真实 key"
    else
      echo "  → 不匹配（测试用的是假 key，正常）"
    fi
  fi
else
  echo "  .env 不存在"
fi

echo ""
echo "=== 6) 清理 ==="
git restore --staged hooktest.tmp 2>/dev/null
rm -f hooktest.tmp
echo "  已清理"

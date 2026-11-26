#!/usr/bin/env bash
# 验证 README 里的 badge 是否真的能访问（返回 200 且是图片）
#
# ## 为什么要验
#
# badge 是 shields.io 的动态图片。写错参数的话它不会报错，
# 而是显示一个灰色的 "invalid" 图片——在 README 上看起来像没加载完。
#
# 所以必须逐个请求，确认 HTTP 200 + Content-Type 是图片。
set -u
README=/mnt/e/桌面/实习项目/interview_agent/README.md

echo "=== 提取 README 里的 badge URL ==="
grep -oE '!\[[^]]*\]\((https://[^)]+)\)' "$README" \
  | grep -oE 'https://[^)]+' > /tmp/badges.txt
grep -oE '<img src="(https://[^"]+)"' "$README" \
  | grep -oE 'https://[^"]+' >> /tmp/badges.txt

sort -u /tmp/badges.txt > /tmp/badges_uniq.txt
COUNT=$(wc -l < /tmp/badges_uniq.txt)
echo "  找到 $COUNT 个外部图片"

echo ""
echo "=== 逐个验证 ==="
FAIL=0
while IFS= read -r url; do
  [ -z "$url" ] && continue
  # 取 HTTP 状态和 content-type
  RESULT=$(curl -s -o /dev/null -w "%{http_code}|%{content_type}" \
    --max-time 20 -L "$url" 2>/dev/null || echo "000|")
  CODE=${RESULT%%|*}
  CTYPE=${RESULT#*|}

  # 短名字便于阅读
  NAME=$(echo "$url" | sed 's|https://||' | cut -c1-58)

  if [ "$CODE" = "200" ] && echo "$CTYPE" | grep -qi "image"; then
    echo "  ✓ $CODE  $NAME"
  else
    echo "  ✗ $CODE  $NAME"
    echo "        content-type: $CTYPE"
    FAIL=$((FAIL + 1))
  fi
done < /tmp/badges_uniq.txt

echo ""
if [ "$FAIL" -eq 0 ]; then
  echo "全部 $COUNT 个外部图片可访问"
  exit 0
else
  echo "有 $FAIL 个有问题"
  exit 1
fi

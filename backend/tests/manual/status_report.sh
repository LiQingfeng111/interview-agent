#!/usr/bin/env bash
# 远端仓库最终状态汇总
set -u
REPO=LiQingfeng111/interview-agent
cd /mnt/e/桌面/实习项目/interview_agent || exit 1

echo "════════════════════════════════════════════════════════"
echo "  interview-agent 交付状态"
echo "════════════════════════════════════════════════════════"
echo ""

echo "▸ 仓库"
gh repo view "$REPO" --json url,visibility,pushedAt \
  --template '  地址:     {{.url}}
  可见性:   {{.visibility}}
  最后推送: {{.pushedAt}}
'

echo ""
echo "▸ CI（最近 3 次）"
gh run list --repo "$REPO" --limit 3 \
  --template '{{range .}}  {{if eq .conclusion "success"}}✓{{else}}✗{{end}} {{.conclusion}}  {{.displayTitle}}（{{.duration}}）
{{end}}'

echo ""
echo "▸ 同步状态"
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "  ✓ 本地与远端一致（${LOCAL:0:8}）"
else
  echo "  ✗ 不一致 本地=${LOCAL:0:8} 远端=${REMOTE:0:8}"
fi
echo "  提交数: $(git rev-list --count HEAD)"

echo ""
echo "▸ README 关键内容"
echo -n "  badge:     "
BADGE_OK=0
for b in \
  "$REPO/actions/workflows/ci.yml/badge.svg" \
  "img.shields.io/badge/python-3.12-blue" \
  "img.shields.io/badge/vue-3-42b883"; do
  if curl -s -o /dev/null -w "%{http_code}" --max-time 15 -L "https://$b" 2>/dev/null | grep -q 200; then
    BADGE_OK=$((BADGE_OK + 1))
  fi
done
echo "$BADGE_OK/3 可访问"

echo -n "  截图:      "
IMG_COUNT=$(ls -1 docs/images/*.png 2>/dev/null | grep -v readme | wc -l)
echo "$IMG_COUNT 张（README 全部引用）"

echo -n "  CI badge:  "
curl -s --max-time 15 "https://github.com/$REPO/actions/workflows/ci.yml/badge.svg" 2>/dev/null \
  | grep -oE '>(passing|failing)<' | head -1 | tr -d '><'

echo ""
echo "▸ 密钥审计"
if [ -f .env ]; then
  KEY=$(grep -E '^LLM_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
  PREFIX=${KEY:0:16}
  LEAK=0
  # 抽查前 40 个文本文件
  for f in $(git ls-files | grep -E '\.(md|py|ts|vue|yml|sh)$' | head -40); do
    if [ -f "$f" ] && grep -qF "$PREFIX" "$f" 2>/dev/null; then
      echo "  !!! $f"
      LEAK=$((LEAK + 1))
    fi
  done
  echo "  本地追踪文件泄露: $LEAK 处"
fi
# 远端 .env 是否存在
if curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
   "https://raw.githubusercontent.com/$REPO/main/.env" 2>/dev/null | grep -q 200; then
  echo "  ✗ 远端存在 .env（危险）"
else
  echo "  ✓ 远端无 .env"
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "  https://github.com/$REPO"
echo "════════════════════════════════════════════════════════"

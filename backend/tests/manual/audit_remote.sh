#!/usr/bin/env bash
# 远端仓库最终审计
set -u
REPO=LiQingfeng111/interview-agent

echo "=== 1) 仓库信息 ==="
gh repo view "$REPO" --json url,visibility,pushedAt,defaultBranchRef \
  --jq '"  地址: \(.url)\n  可见性: \(.visibility)\n  分支: \(.defaultBranchRef.name)\n  最后推送: \(.pushedAt)"'

echo ""
echo "=== 2) CI 各 job 结果 ==="
RUN=$(gh run list --repo "$REPO" --limit 1 --json databaseId --jq '.[0].databaseId')
gh api "repos/$REPO/actions/runs/$RUN/jobs" \
  --jq '.jobs[] | "  \(.conclusion)  \(.name)"'

echo ""
echo "=== 3) 本地 vs 远端 ==="
cd /mnt/e/桌面/实习项目/interview_agent || exit 1
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
echo "  本地: $LOCAL"
echo "  远端: $REMOTE"
[ "$LOCAL" = "$REMOTE" ] && echo "  ✓ 同步" || echo "  ✗ 不一致"

echo ""
echo "=== 4) 提交数 / 文件数 ==="
echo "  提交: $(git rev-list --count HEAD)"
echo "  文件: $(gh api "repos/$REPO/git/trees/main?recursive=1" --jq '.tree | map(select(.type=="blob")) | length')"

echo ""
echo "=== 5) 密钥审计（远端实际内容）==="
env >/dev/null 2>&1
# 从本地 .env 拿真实 key 前缀
if [ -f .env ]; then
  KEY=$(grep -E '^LLM_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
  if [ -n "$KEY" ] && [ "${#KEY}" -ge 16 ]; then
    PREFIX=${KEY:0:16}
    echo "  检查远端文件里有没有 key 前缀（前 16 位）…"
    LEAK=0
    for f in $(gh api "repos/$REPO/git/trees/main?recursive=1" --jq '.tree[].path' | grep -E '\.(md|py|ts|vue|yml|yaml|json|sh|txt)$' | grep -v package-lock | head -100); do
      if gh api "repos/$REPO/contents/$f" --jq '.content' 2>/dev/null | base64 -d 2>/dev/null | grep -qF "$PREFIX"; then
        echo "  !!! $f"
        LEAK=$((LEAK+1))
      fi
    done
    echo "  泄露 $LEAK 处"
  fi
else
  echo "  .env 不存在（无法比对）"
fi

echo ""
echo "=== 6) 关键文件是否在远端 ==="
for f in README.md LICENSE start.sh .env.example .github/workflows/ci.yml \
         backend/requirements.txt docs/images/02-interview-setup.png; do
  if gh api "repos/$REPO/contents/$f" --jq '.name' >/dev/null 2>&1; then
    echo "  ✓ $f"
  else
    echo "  ✗ $f 缺失"
  fi
done

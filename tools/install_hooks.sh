#!/usr/bin/env bash
# 安装 git hooks（密钥检查 + 测试）
#
# ## 为什么需要手动安装
#
# git hooks 存放在 .git/hooks/ 里，**不会随仓库分发** ——
# 别人 clone 下来是没有的。
#
# 所以这个脚本做两件事：
#   1. 把 hooks 装到 .git/hooks/
#   2. 在 README 里说明"clone 后要跑一次"
#
# ## 用法
#
#   bash tools/install_hooks.sh
set -u
PROJ=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJ" || exit 1

HOOK_DIR=".git/hooks"
if [ ! -d "$HOOK_DIR" ]; then
  echo "✗ 不是 git 仓库（缺 .git/hooks）"
  exit 1
fi

# ---------------------------------------------------------------- pre-commit
cat > "$HOOK_DIR/pre-commit" <<'HOOK'
#!/usr/bin/env bash
# 提交前检查：密钥泄露
#
# 这个 hook 由 tools/install_hooks.sh 安装。
# 只做一件事：拦住疑似密钥，防止它进入提交历史。
#
# 为什么只用这一个检查（不跑测试）：
# 提交频率高，每次跑 400 条测试会让人想加 --no-verify 绕过，
# 那样这个 hook 就形同虚设。密钥检查只要 0.2 秒，没人会想绕过它。
exec bash "$(git rev-parse --show-toplevel)/tools/pre_commit_check.sh"
HOOK

chmod +x "$HOOK_DIR/pre-commit"
echo "✓ 已安装 pre-commit（密钥检查）"

# ---------------------------------------------------------------- 验证
echo ""
echo "=== 自测 ==="

# 造一个假场景：往暂存区塞一个带 key 的文件
TESTFILE=".hook_selftest.tmp"
echo 'LLM_API_KEY=ofm-FAKEFAKEFAKEFAKEFAKE123456' > "$TESTFILE"
git add -f "$TESTFILE" 2>/dev/null

if bash tools/pre_commit_check.sh >/dev/null 2>&1; then
  echo "  ✗ 自测失败：假密钥居然通过了"
  RESULT=1
else
  echo "  ✓ 自测通过：假密钥被拦下"
  RESULT=0
fi

git restore --staged "$TESTFILE" 2>/dev/null
rm -f "$TESTFILE"

echo ""
if [ "$RESULT" -eq 0 ]; then
  echo "密钥检查已生效。之后的每次 git commit 都会自动跑。"
  echo ""
  echo "  绕过（仅在确认不敏感时）：git commit --no-verify"
else
  echo "检查脚本有问题，请排查 tools/pre_commit_check.sh"
fi
exit "$RESULT"

#!/usr/bin/env bash
# ============================================================================
#  提交前密钥检查（pre-commit hook）
#
#  ## 为什么需要这道防线
#
#  `.gitignore` 只在"文件还没被追踪"时有效。一旦某次误操作 `git add -f .env`，
#  或者把 key 粘进了某个 .py/.md 里，`.gitignore` 就管不住了。
#
#  而且密钥一旦进了提交历史，`git rm` 也删不掉——必须重写历史，
#  如果已经 push 过还要强制推送 + 换密钥。
#
#  所以：**每次 commit 前自动扫一遍**，比事后补救便宜得多。
#
#  ## 检查什么
#
#  1. 暂存区里有没有 .env / *.key / *.pem 这类文件
#  2. 暂存的文件内容里有没有像是密钥的字符串
#     （用真实 key 的前缀 + 通用模式双重匹配）
#  3. 疑似 token 的长随机串
#
#  ## 安装
#
#    bash tools/install_hooks.sh
# ============================================================================
set -u

RED=$'\033[31m'; YEL=$'\033[33m'; GRN=$'\033[32m'; END=$'\033[0m'

fail() { echo "${RED}✗ $*${END}" >&2; }

violations=0
report() {
  violations=$((violations + 1))
  fail "$*"
}

# ---------------------------------------------------------------- 1) 危险文件名
#
# 暂存区里出现这些，直接拒。不看内容，因为文件本身就不该进来。
DANGEROUS=$(git diff --cached --name-only --diff-filter=ACM 2>/dev/null | grep -E '(^|/)\.env$|(^|/)\.env\.local$|\.key$|\.pem$|secret|credential' || true)
if [ -n "$DANGEROUS" ]; then
  report "暂存区里有敏感文件名："
  echo "$DANGEROUS" | sed 's/^/    /' >&2
  echo "    如果确认要提交，用：git commit --no-verify" >&2
fi

# ---------------------------------------------------------------- 2) 内容扫描
#
# 只看暂存的**新增行**（`git diff --cached -U0` 的 + 行），
# 这样不会因为历史里已有的内容而误报。
#
# ## 这里有个坑（第一版就是错的）
#
# 我写的是：
#     ADDED=$(git diff --cached -U0 | grep -E '^\+' | grep -v '^\+\+\+')
#
# 看着没问题，但 `grep -E '^\+'` 之后所有行都以 + 开头了，
# 紧接着 `grep -v '^\+\+\+'` 会把**内容本身以 ++ 开头的行**也滤掉。
# 实测结果：ADDED 恒为空 → 检查恒通过 → 假密钥照样进去。
#
# 正确做法：先用 awk 精确跳过 `+++ b/file` 这种文件头，
# 再取 + 开头的行。
ADDED=$(git diff --cached -U0 2>/dev/null \
  | awk '/^\+\+\+ /{next} /^\+/{print substr($0,2)}' || true)

if [ -n "$ADDED" ]; then
  # 2a) 通用密钥模式
  #
  # 这些模式来自各家 API key 的常见格式。
  # 宁可误报（多看一眼），不要漏报（泄露）。
  #
  # ## 两个踩过的坑
  #
  # ① `-----BEGIN ... PRIVATE KEY-----` 不能直接放进 PATTERNS ——
  #    grep 会把它当成命令行选项，报 `unknown option -- ---BEGIN`。
  #    私钥头单独用 `grep -e` 处理（见 2a-bis）。
  #
  # ② 会误报两类正常内容：
  #    - `package-lock.json` 里的 `"integrity": "sha512-..."` 完整性哈希
  #    - 自测脚本里的假 key（`ofm-FAKE...`）
  #    下面加了白名单过滤。
  PATTERNS=(
    'sk-[A-Za-z0-9]{20,}'                    # OpenAI 风格
    'sk-ant-[A-Za-z0-9_-]{20,}'              # Anthropic
    'ghp_[A-Za-z0-9]{30,}'                   # GitHub PAT
    'gho_[A-Za-z0-9]{30,}'
    'AIza[A-Za-z0-9_-]{30,}'                 # Google API
    'ofm-[A-Za-z0-9_-]{20,}'                 # 本项目 relay key 的格式
    'xox[baprs]-[A-Za-z0-9-]{10,}'           # Slack
    'AKIA[A-Z0-9]{16}'                       # AWS Access Key
  )

  for pat in "${PATTERNS[@]}"; do
    hit=$(echo "$ADDED" | grep -oE -e "$pat" | head -3 || true)
    if [ -n "$hit" ]; then
      # 白名单：明显的假 key 不算泄露
      hit=$(echo "$hit" | grep -vE 'FAKE|EXAMPLE|placeholder|xxxx|test[-_]?key' || true)
      if [ -n "$hit" ]; then
        report "新增内容里疑似密钥（模式 $pat）："
        echo "$hit" | sed 's/^/    /' >&2
      fi
    fi
  done

  # 2a-bis) 私钥文件头（单独处理，避免 grep 选项解析问题）
  if echo "$ADDED" | grep -qE -e 'BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY'; then
    report "新增内容里有私钥文件头（PRIVATE KEY）"
  fi

  # 2b) 真实 key 前缀（如果 .env 存在，拿它做精确比对）
  #
  # 这是最可靠的一道：不管格式怎么变，只要是我们自己那个 key 就拦得住。
  if [ -f .env ]; then
    REALKEY=$(grep -E '^LLM_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)
    if [ -n "$REALKEY" ] && [ "${#REALKEY}" -ge 16 ]; then
      # 用前 16 位匹配（足够唯一，又不会把整个 key 打进日志）
      PREFIX=${REALKEY:0:16}
      if echo "$ADDED" | grep -qF -- "$PREFIX"; then
        report "新增内容里出现了 .env 里的真实 LLM_API_KEY（前 16 位匹配）"
      fi
    fi
  fi

  # 2c) 长随机串（启发式，只警告不拦）
  #
  # 40+ 位、大小写数字混合的串很可能是 token，但也可能是：
  #   - package-lock.json 的 sha512 完整性哈希
  #   - base64 编码的正常数据
  # 所以只警告，并排除已知安全场景。
  SUSPECT=$(echo "$ADDED" \
    | grep -oE '[A-Za-z0-9_-]{40,}' \
    | grep -E '[A-Z]' | grep -E '[a-z]' | grep -E '[0-9]' \
    | grep -vE '^sha(1|256|384|512)-' \
    | head -3 || true)
  if [ -n "$SUSPECT" ]; then
    echo "${YEL}! 新增内容里有长随机串，确认不是密钥：${END}" >&2
    echo "$SUSPECT" | sed 's/^/    /' >&2
    echo "    （确认无害可直接 commit，这一项只警告不拦）" >&2
  fi
fi

# ---------------------------------------------------------------- 结果
if [ "$violations" -gt 0 ]; then
  echo "" >&2
  echo "${RED}提交被拦下：发现 $violations 处疑似密钥${END}" >&2
  echo "" >&2
  echo "  怎么处理：" >&2
  echo "    1. 从暂存区移除：git restore --staged <文件>" >&2
  echo "    2. 如果需要它，加到 .gitignore" >&2
  echo "    3. 确认不敏感再绕过：git commit --no-verify" >&2
  echo "" >&2
  exit 1
fi

echo "${GRN}✓ 密钥检查通过${END}"
exit 0

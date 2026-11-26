#!/usr/bin/env bash
# 验证 requirements.txt 是否真的够用（WSL 无 venv 版本）
#
# ## 为什么必须验
#
# 上一版 requirements.txt 漏了 langchain 一整套，GitHub CI 上直接崩 61 条：
#     ModuleNotFoundError: No module named 'langchain_core'
#
# 本地看不出来，因为 .venv 里有历史残留的包。
# 唯一可靠的验法：在**没有任何历史残留**的环境里只按这份清单装，再跑测试。
#
# ## 为什么用 pip --target 而不是 venv
#
# 这台 WSL 缺 python3-venv 和 ensurepip（没有 sudo 装不了）。
# `pip install --target=DIR` 能把包装到一个空目录，
# 效果等价于干净环境：PYTHONPATH 指向它，就只看得到这些包。
#
# ## 用法
#
#   bash tools/verify_requirements.sh
set -u
PROJ=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJ" || exit 1

# 用现有 .venv 的 pip（它有网络和索引配置）
PIP="$PROJ/.venv/bin/pip"
PY="$PROJ/.venv/bin/python"
if [ ! -x "$PIP" ]; then
  echo "✗ 找不到 $PIP"
  exit 1
fi

TARGET=/tmp/ia-req-check
echo "=== 1) 准备空目录作为隔离环境 ==="
rm -rf "$TARGET"
mkdir -p "$TARGET"
echo "  $TARGET"

echo ""
echo "=== 2) 只按 requirements.txt 安装到该目录 ==="
START=$(date +%s)
if "$PIP" install -q --target "$TARGET" -r backend/requirements.txt 2>&1 | tail -6; then
  END=$(date +%s)
  echo "  ✓ 安装完成（$((END-START)) 秒）"
else
  echo "  ✗ 安装失败"
  exit 1
fi

echo ""
echo "=== 3) 检查关键包能否 import（只用 $TARGET 里的包）==="
MISSING=0
for mod in fastapi uvicorn pydantic jwt psycopg redis fitz docx numpy \
           langchain_core langchain_openai langgraph; do
  if PYTHONPATH="$TARGET" "$PY" -S -c "import $mod" 2>/dev/null; then
    echo "  ✓ $mod"
  else
    echo "  ✗ $mod 缺失"
    MISSING=$((MISSING + 1))
  fi
done

echo ""
echo "=== 4) 跑规则层测试（CI 的 core job 内容）==="
echo "  注意：PYTHONPATH 里 $TARGET 在前，项目根在后"
RESULT=$(PYTHONPATH="$TARGET:$PROJ" "$PY" -m pytest tests/ -q 2>&1 | tail -3)
echo "$RESULT" | sed 's/^/  /'
RC=0
echo "$RESULT" | grep -qE '[0-9]+ (passed|failed)' || RC=1
echo "$RESULT" | grep -qE '[0-9]+ failed' && RC=1

echo ""
echo "=== 结论 ==="
if [ "$MISSING" -gt 0 ]; then
  echo "  ✗ 有 $MISSING 个包缺失，requirements.txt 不完整"
  exit 1
elif [ "$RC" -eq 0 ]; then
  echo "  ✓ requirements.txt 完整，隔离环境下规则层测试全过"
  rm -rf "$TARGET"
  exit 0
else
  echo "  ✗ 包装齐了但测试没全过（可能是代码问题，不是清单问题）"
  exit 1
fi

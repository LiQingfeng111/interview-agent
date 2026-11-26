#!/usr/bin/env bash
# 探测 WSL 里建虚拟环境的手段（没有 sudo 时的备选）
set -u
echo "=== 1) python3-venv 可用性 ==="
if python3 -m venv /tmp/_probe_venv 2>/tmp/_probe_err; then
  echo "  ✓ python3 -m venv 可用"
  rm -rf /tmp/_probe_venv
else
  echo "  ✗ 不可用"
  head -3 /tmp/_probe_err | sed 's/^/    /'
fi

echo ""
echo "=== 2) ensurepip ==="
python3 -c "import ensurepip" 2>/dev/null && echo "  ✓ 有" || echo "  ✗ 没有"

echo ""
echo "=== 3) virtualenv 命令 ==="
which virtualenv 2>/dev/null && echo "  ✓ 有" || echo "  ✗ 没有"

echo ""
echo "=== 4) 现有 .venv 里有没有 virtualenv ==="
if [ -x .venv/bin/virtualenv ]; then
  echo "  ✓ 有（可用它建新环境）"
else
  echo "  ✗ 没有"
fi

echo ""
echo "=== 5) pip --target 方案（不需要 venv）==="
echo "  可以用：pip install --target=/tmp/xxx -r backend/requirements.txt"
echo "  然后 PYTHONPATH=/tmp/xxx 跑测试"

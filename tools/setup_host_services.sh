#!/usr/bin/env bash
# 启动 Ollama（Windows 宿主机）+ 端口转发，让 WSL 后端能访问
#
# 为什么需要这个脚本：
# Ollama 和模型网关都在 Windows 侧，且只监听 127.0.0.1。
# WSL 里的后端访问不到，必须：
#   1. 让 Ollama 监听 0.0.0.0:11800（Windows 侧）
#   2. 给模型网关做端口转发（18336 → 18326）
#
# 这两个步骤以前靠手动，机器重启就没了 → 测试大面积失败。
# 现在固化成一个脚本，`bash ops.sh setup-host` 调它。
set -u

echo "=== 1) 检查 Ollama 是否在跑 ==="
OLLAMA_PORT=11800
if curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:$OLLAMA_PORT/api/tags" 2>/dev/null | grep -q 200; then
  echo "  ✓ Ollama 已在 $OLLAMA_PORT"
else
  echo "  Ollama 未运行，需要 Windows 侧启动（本脚本在 WSL 里跑不了 Windows 进程）"
  echo "  请在 Windows PowerShell 执行："
  echo "    \$env:OLLAMA_HOST='0.0.0.0:11800'; & \"\$env:LOCALAPPDATA\\Programs\\Ollama\\ollama.exe\" serve"
fi

echo ""
echo "=== 2) 检查模型网关转发（18336）==="
if curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:18336/v1/models" 2>/dev/null | grep -qE "200|401"; then
  echo "  ✓ 转发端口 18336 已就绪"
else
  echo "  未就绪，请在 Windows PowerShell 执行："
  echo "    node tools/port_forward.cjs 18336 18326"
fi

echo ""
echo "=== 3) 从 WSL 侧验证可达性 ==="
HOST_IP=$(ip route show default | awk '{print $3}')
echo "  宿主 IP: $HOST_IP"
for port in 11800 18336; do
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://$HOST_IP:$port/" 2>/dev/null || echo "000")
  echo "    $HOST_IP:$port → $code"
done

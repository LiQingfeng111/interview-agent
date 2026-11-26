#!/usr/bin/env bash
# 探测：WSL 里用哪个地址能访问 Windows 侧的 Vite（5173）
set -u
GW=$(ip route show default | awk '{print $3}')
echo "WSL 默认网关: $GW"
echo ""

for h in 127.0.0.1 "$GW" localhost ::1; do
  URL="http://$h:5173/"
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$URL" 2>/dev/null || echo 000)
  case "$CODE" in
    200|30*) echo "  ✓ $h:5173 -> $CODE" ;;
    *)       echo "  ✗ $h:5173 -> $CODE" ;;
  esac
done

echo ""
echo "=== Vite 实际监听在哪个地址（Windows 侧）==="
echo "  （在 Windows PowerShell 里看：netstat -ano | findstr :5173）"

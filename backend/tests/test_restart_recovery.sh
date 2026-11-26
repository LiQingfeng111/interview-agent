#!/usr/bin/env bash
# 实测：进程重启后能否继续答同一场面试
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
BASE=http://127.0.0.1:8080
J='Content-Type: application/json'

U="restart_$RANDOM$RANDOM"
RESUME='项目一：课程问答助手\n结果：召回从71%提到82%'

echo "=== 1) 注册登录 ==="
curl -s -X POST "$BASE/api/auth/register" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"restart123\"}" >/dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"restart123\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "  token ${#TOKEN} 字符"

echo ""
echo "=== 2) 开面试 ==="
START=$(curl -s -X POST "$BASE/api/sessions" -H "$J" -H "$AUTH" \
  -d "{\"resume\":\"$RESUME\",\"role\":\"ai_algo\",\"max_turns\":6,\"mock\":true}")
SID=$(echo "$START" | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  session_id = $SID"

echo ""
echo "=== 3) 先答 2 轮 ==="
for i in 1 2; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H "$J" -H "$AUTH" \
    -d '{"answer":"召回从71%提到82%，切块500字，实测过三档。"}')
  T=$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)
  V=$(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4)
  echo "  第$T轮 verdict=$V"
done

echo ""
echo "=== 4) 杀掉服务（模拟崩溃）==="
pkill -f "uvicorn app.main:app" 2>/dev/null && echo "  已杀" || echo "  未在运行"
sleep 2

echo ""
echo "=== 5) 重启服务 ==="
cd "$PROJ" || exit 1
bash ops.sh api 2>&1 | grep -E "就绪|PID|超时" | head -2

echo ""
echo "=== 6) 重启后继续答（关键验证）==="
R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H "$J" -H "$AUTH" \
  -d '{"answer":"三档里 500 字最好，代价是索引体积多 30%。"}')
echo "  响应: $(echo "$R" | head -c 400)"
T=$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)
if [ -n "$T" ] && [ "$T" -gt 0 ]; then
  echo ""
  echo "  ✓ 重启后能继续答：第 $T 轮（不是从 0 开始）"
else
  echo ""
  echo "  ✗ 重启后恢复失败"
fi

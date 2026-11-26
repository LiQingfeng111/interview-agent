#!/usr/bin/env bash
# 成本累积最终验证（修好所有已知问题后）
set -u
BASE=http://127.0.0.1:8080
PROJ=/mnt/e/桌面/实习项目/interview_agent
U="cfinal_$RANDOM$RANDOM"

curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"cfinal123456\"}" >/dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"cfinal123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "用户 $U"

cat > /tmp/cf_resume.md <<'EOF'
姓名：钱七
项目一：课程问答助手（2026.03-2026.09）
做法：RAG检索，切块500字，top20重排
结果：召回从71%提到82%，P99延迟1.8秒
我负责：切块策略实验、评测集标注
EOF
UP=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/cf_resume.md" -F "kind=resume")
DOC=$(echo "$UP" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  doc=$DOC"
if [ -z "$DOC" ]; then echo "  上传失败: $(echo "$UP" | head -c 200)"; exit 1; fi

for i in $(seq 1 30); do
  S=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$S" = "ready" ] && { echo "  向量化就绪（${i}s）"; break; }
  sleep 1
done

cat > /tmp/cf_start.json <<EOF
{"resume":"","document_id":"$DOC","role":"ai_algo","persona":"balanced","max_turns":12,"mock":false}
EOF
START=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' -H "$AUTH" \
  --data-binary @/tmp/cf_start.json)
SID=$(echo "$START" | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -z "$SID" ]; then echo "  开会话失败: $(echo "$START" | head -c 300)"; exit 1; fi
echo "  会话 $SID"

cat > /tmp/cf_ans.json <<'EOF'
{"answer":"切块500字是300/500/800三档实测选的，召回从71%到82%，代价是索引体积多30%。"}
EOF

q() {
  docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
    "SELECT 'tokens=' || total_tokens || ' calls=' || llm_calls || ' cost=' || ROUND(total_cost_cny::numeric,6)
     FROM sessions WHERE id='$SID'" 2>/dev/null | head -1
}

echo ""
echo "=== 重启前：答 3 轮 ==="
for i in 1 2 3; do
  T0=$(date +%s%N)
  curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/cf_ans.json >/dev/null
  T1=$(date +%s%N)
  echo "  第$i轮 $(( (T1-T0)/1000000 ))ms"
done
echo "  $(q)"

echo ""
echo "=== 重启后端 ==="
pkill -f "uvicorn app.main:app" 2>/dev/null && echo "  已杀"
sleep 3
cd "$PROJ" && bash ops.sh api 2>&1 | grep '就绪' | head -1

echo ""
echo "=== 重启后：再答 3 轮 ==="
for i in 4 5 6; do
  T0=$(date +%s%N)
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/cf_ans.json)
  T1=$(date +%s%N)
  echo "  第$i轮 $(( (T1-T0)/1000000 ))ms  $(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4)"
done
echo "  $(q)"

echo ""
echo "=== 报告 ==="
curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH" > /tmp/cf_rep.json
python3 -c "
import json
d = json.load(open('/tmp/cf_rep.json')).get('data') or {}
c = d.get('cost') or {}
print('  cost:', json.dumps(c, ensure_ascii=False))
print('  turns:', d.get('turns'), 'avg:', d.get('avg'))
" 2>/dev/null

echo ""
echo "=== 日志里的记账记录 ==="
grep -c 'usage 记账' /tmp/fastapi.log 2>/dev/null || echo 0

echo ""
echo "=== 判断：6 轮应约 12 次调用 ==="
CALLS=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT llm_calls FROM sessions WHERE id='$SID'" 2>/dev/null | tr -d ' ')
echo "  实际: ${CALLS:-0} 次"
[ "${CALLS:-0}" -ge 10 ] && echo "  ✓ 跨重启累积正确" || echo "  ✗ 仍有丢失"

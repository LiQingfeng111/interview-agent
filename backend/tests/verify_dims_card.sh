#!/usr/bin/env bash
# 验证：① 五维分数入库 ② coach 卡片返回
set -u
BASE=http://127.0.0.1:8080
U="dim_$RANDOM"

# 先跑 schema（补 turns 的维度列）
cd /mnt/e/桌面/实习项目/interview_agent || exit 1
bash ops.sh schema 2>&1 | grep -E "条语句" | head -1

curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"dim123456\"}" >/dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"dim123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "用户 $U"

cat > /tmp/dim_resume.md <<'EOF'
姓名：吴十
项目一：课程问答助手
做法：RAG，切块500字，top20重排
结果：召回从71%提到82%
EOF
DOC=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/dim_resume.md" -F "kind=resume" \
  | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
for i in $(seq 1 25); do
  S=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$S" = "ready" ] && break
  sleep 1
done

echo ""
echo "=== 开面试（coach=true 验证点评卡）==="
cat > /tmp/dim_start.json <<EOF
{"resume":"","document_id":"$DOC","role":"ai_algo","persona":"balanced","max_turns":6,"mock":true,"coach":true}
EOF
SID=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' -H "$AUTH" \
  --data-binary @/tmp/dim_start.json | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  session=$SID"

cat > /tmp/dim_ans.json <<'EOF'
{"answer":"切块500字是300/500/800三档实测选的，召回从71%到82%，代价是索引体积多30%。"}
EOF

for i in 1 2 3; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/dim_ans.json)
  echo "  第$i轮："
  echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
ev = d.get('eval') or {}
print('    eval.dims:', json.dumps(ev.get('dims'), ensure_ascii=False))
card = d.get('card')
if card:
    keys = list(card.keys())[:4]
    print(f'    card: 有（字段 {keys}）')
else:
    print('    card: null')
" 2>/dev/null
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  [ "$D" = "true" ] && break
done

echo ""
echo "=== DB 里的五维分数 ==="
docker exec ia-pg psql -U ia -d interview_agent -c \
  "SELECT seq, verdict, ROUND(score::numeric,2) AS avg, ROUND(accuracy::numeric,1) AS acc,
          ROUND(depth::numeric,1) AS dep, ROUND(tradeoff::numeric,1) AS trd,
          ROUND(boundary::numeric,1) AS bnd, ROUND(clarity::numeric,1) AS clr
   FROM turns WHERE session_id='$SID' ORDER BY seq" 2>/dev/null | head -8

echo ""
echo "=== 报告里的 dims（雷达图数据源）==="
curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
print('  dims:', json.dumps(d.get('dims'), ensure_ascii=False))
print('  turns:', d.get('turns'), 'avg:', d.get('avg'))
" 2>/dev/null

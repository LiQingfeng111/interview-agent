#!/usr/bin/env bash
# 本轮新增功能的端到端验证
set -u
BASE=http://127.0.0.1:8080
U="feat_$RANDOM$RANDOM"

echo "############ 0) 后端就绪 ############"
cd /mnt/e/桌面/实习项目/interview_agent || exit 1
bash ops.sh api 2>&1 | grep -E "就绪|自检失败" | head -2

curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"feat123456\"}" >/dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"feat123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "  用户 $U"

cat > /tmp/feat_resume.md <<'EOF'
姓名：郑十一
项目一：课程问答助手（2026.03-2026.09）
做法：RAG检索，切块500字，top20重排，bge-m3 做 embedding
结果：召回从71%提到82%，P99延迟1.8秒
代价：索引体积增加30%
我负责：切块策略实验、评测集标注、重排模块
EOF
DOC=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/feat_resume.md" -F "kind=resume" \
  | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
for i in $(seq 1 25); do
  S=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$S" = "ready" ] && { echo "  简历就绪（${i}s）"; break; }
  sleep 1
done

echo ""
echo "############ 1) 开面试（真模型 + coach）############"
cat > /tmp/feat_start.json <<EOF
{"resume":"","document_id":"$DOC","role":"ai_algo","persona":"strict","max_turns":6,"mock":false,"coach":true}
EOF
START=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' -H "$AUTH" \
  --data-binary @/tmp/feat_start.json)
SID=$(echo "$START" | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -z "$SID" ]; then
  echo "  失败: $(echo "$START" | head -c 300)"
  exit 1
fi
echo "  session=$SID"

echo ""
echo "############ 2) SSE 回放流式（前端首问用）############"
curl -s -N --max-time 15 "$BASE/api/sessions/$SID/stream?token=$TOKEN" > /tmp/sse_out.txt 2>&1
FRAMES=$(grep -c '^event:' /tmp/sse_out.txt 2>/dev/null || echo 0)
FIRST=$(grep -m1 '^data:' /tmp/sse_out.txt 2>/dev/null | head -c 80)
LAST=$(grep '^event: done' -A1 /tmp/sse_out.txt 2>/dev/null | tail -1 | head -c 100)
echo "  帧数: $FRAMES"
echo "  首帧: $FIRST"
echo "  结束: $LAST"

echo ""
echo "############ 3) SSE 真流式（/stream/ask）############"
T0=$(date +%s%N)
curl -s -N --max-time 60 -X POST "$BASE/api/sessions/$SID/stream/ask?token=$TOKEN" > /tmp/sse_live.txt 2>&1
T1=$(date +%s%N)
LIVE_FRAMES=$(grep -c '^event: token' /tmp/sse_live.txt 2>/dev/null || echo 0)
LIVE_START=$(grep -m1 '"live"' /tmp/sse_live.txt 2>/dev/null | head -c 60)
echo "  耗时 $(( (T1-T0)/1000000 ))ms"
echo "  token 帧数: $LIVE_FRAMES"
echo "  首帧: $LIVE_START"
if grep -q '"live": true' /tmp/sse_live.txt 2>/dev/null; then
  echo "  ✓ 真流式已生效"
else
  echo "  ⚠ 走了降级回放（模型可能不支持 stream）"
fi

echo ""
echo "############ 4) 逐轮作答（验证五维 + card）############"
cat > /tmp/feat_ans.json <<'EOF'
{"answer":"切块500字是300/500/800三档实测选的，召回从71%到82%，代价是索引体积多30%。"}
EOF
for i in 1 2 3 4 5 6; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/feat_ans.json)
  echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
ev = d.get('eval') or {}
dims = ev.get('dims') or {}
card = d.get('card')
print(f\"  第{d.get('turn')}轮 {ev.get('verdict')}({ev.get('avg')}) dims={len(dims)}个 card={'有' if card else '无'}\")
" 2>/dev/null
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  [ "$D" = "true" ] && break
done

echo ""
echo "############ 5) 报告（雷达图数据）############"
curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH" > /tmp/feat_rep.json
python3 -c "
import json
d = json.load(open('/tmp/feat_rep.json')).get('data') or {}
dims = d.get('dims') or {}
print(f'  turns={d.get(\"turns\")} avg={d.get(\"avg\")}')
print(f'  五维: {json.dumps(dims, ensure_ascii=False)}')
c = d.get('cost') or {}
print(f'  成本: {c.get(\"tokens\")} token / {c.get(\"calls\")} 次调用')
md = d.get('markdown') or ''
print(f'  报告长度: {len(md)} 字符')
" 2>/dev/null

echo ""
echo "############ 6) DB 里的五维 ############"
docker exec ia-pg psql -U ia -d interview_agent -c \
  "SELECT seq, verdict, ROUND(score::numeric,2) AS avg, accuracy AS acc, depth AS dep,
          tradeoff AS trd, boundary AS bnd, clarity AS clr
   FROM turns WHERE session_id='$SID' ORDER BY seq" 2>/dev/null | head -10

#!/usr/bin/env bash
# Docker 全栈最终验收（所有问题修完后）
set -u
BASE=http://127.0.0.1:8080
FRONT=http://127.0.0.1:8081

echo "############ 0) 等后端真正就绪 ############"
# 踩过的坑：容器 `health: starting` 期间（约 40 秒）接口是不可用的，
# 直接测会得到 502，让人误以为"代理坏了"。
# 这里显式等到 healthy 再测，避免误判。
for i in $(seq 1 40); do
  BE=$(docker inspect --format='{{.State.Health.Status}}' ia-backend 2>/dev/null || echo none)
  FE=$(docker inspect --format='{{.State.Health.Status}}' ia-frontend 2>/dev/null || echo none)
  if [ "$BE" = "healthy" ] && [ "$FE" = "healthy" ]; then
    echo "  ✓ 前后端都 healthy（${i}0s 内）"
    break
  fi
  sleep 3
done
# 再给 nginx 一点时间完成首请求的 DNS 解析
sleep 3

echo ""
echo "############ 1) 容器状态 ############"
docker ps --format '  {{.Names}} | {{.Status}}' | grep "^  ia-" | head -5

echo ""
echo "############ 2) 组件健康 ############"
curl -s --max-time 15 "$BASE/api/health" | python3 -c "
import json,sys
d = json.load(sys.stdin)
for k, v in (d.get('components') or {}).items():
    mark = '✓' if v.get('ok') else '✗'
    extra = {kk: vv for kk, vv in v.items() if kk != 'ok'}
    print(f'  {mark} {k:<14} ' + json.dumps(extra, ensure_ascii=False)[:90])
" 2>/dev/null

echo ""
echo "############ 3) 题库向量 ############"
docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT COUNT(*) AS total, COUNT(embedding) AS vec FROM bank_questions" 2>/dev/null

echo ""
echo "############ 4) 前端 → 后端代理 ############"
curl -s -o /dev/null -w "  首页 :8081 → %{http_code}\n" --max-time 10 "$FRONT/"
curl -s -o /dev/null -w "  代理 /api/health → %{http_code}\n" --max-time 15 "$FRONT/api/health"

echo ""
echo "############ 5) 端到端：题库语义检索 ############"
U="dfinal_$RANDOM"
curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"dfinal123456\"}" >/dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"dfinal123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "  用户 $U"

curl -s -X POST "$BASE/api/bank/search" -H 'Content-Type: application/json' -H "$AUTH" \
  -d '{"q":"多头注意力每个头的维度是多少","k":3}' \
  | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
hits = d.get('hits') or []
print(f'  召回 {len(hits)} 条（阈值 {d.get(\"threshold\")}）')
for h in hits[:3]:
    print(f\"    {h['score']:.3f}  {h['project']}  {h['text'][:38]}\")
" 2>/dev/null

echo ""
echo "############ 6) 端到端：开一场面试（mock，验流程）############"
cat > /tmp/df_resume.md <<'EOF'
姓名：周九
项目一：课程问答助手
做法：RAG，切块500字
结果：召回从71%提到82%
EOF
DOC=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/df_resume.md" -F "kind=resume" \
  | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -n "$DOC" ]; then
  for i in $(seq 1 25); do
    S=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
    [ "$S" = "ready" ] && { echo "  简历向量化就绪（${i}s）"; break; }
    sleep 1
  done
fi

cat > /tmp/df_start.json <<EOF
{"resume":"","document_id":"$DOC","role":"ai_algo","persona":"balanced","max_turns":6,"mock":true}
EOF
SID=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' -H "$AUTH" \
  --data-binary @/tmp/df_start.json | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
if [ -z "$SID" ]; then
  echo "  开会话失败"
else
  echo "  会话 $SID"
  cat > /tmp/df_ans.json <<'EOF'
{"answer":"切块500字，召回从71%到82%，代价是索引多30%。"}
EOF
  for i in 1 2 3; do
    R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
      -H "$AUTH" --data-binary @/tmp/df_ans.json)
    T=$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)
    V=$(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4)
    D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
    echo "    第${T:-?}轮 $V done=$D"
    [ "$D" = "true" ] && break
  done

  echo ""
  echo "  报告："
  curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH" \
    | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
print(f'    turns={d.get(\"turns\")} avg={d.get(\"avg\")}')
c = d.get('cost') or {}
print(f'    cost: {c.get(\"tokens\")} token / {c.get(\"calls\")} 次')
" 2>/dev/null
fi

echo ""
echo "############ 结论 ############"
echo "  一条命令起全栈：bash tools/up_full_stack.sh"
echo "  前端 http://localhost:8081"
echo "  后端 http://localhost:8080/docs"

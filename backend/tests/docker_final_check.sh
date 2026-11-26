#!/usr/bin/env bash
# Docker 全栈最终验收
set -u
echo "=========== 1) 容器状态 ==========="
docker ps --format '  {{.Names}} | {{.Status}} | {{.Ports}}' | grep -E "ia-" | head -6

echo ""
echo "=========== 2) 后端健康（含各组件）==========="
curl -s --max-time 15 http://127.0.0.1:8080/api/health | head -c 700
echo ""

echo ""
echo "=========== 3) 前端 → 后端代理 ==========="
curl -s -o /dev/null -w "  前端首页 :8081 → %{http_code}\n" --max-time 10 http://127.0.0.1:8081/
curl -s -o /dev/null -w "  经代理 /api/health → %{http_code}\n" --max-time 15 http://127.0.0.1:8081/api/health

echo ""
echo "=========== 4) 题库自动导入了吗 ==========="
docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT '总题数=' || COUNT(*) || ' 有向量=' || COUNT(*) FILTER (WHERE embedding IS NOT NULL) FROM bank_questions" 2>/dev/null

echo ""
echo "=========== 5) 端到端：注册→登录→会话 ==========="
U="docker_$RANDOM$RANDOM"
curl -s -X POST http://127.0.0.1:8080/api/auth/register -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"docker123456\"}" | head -c 120
echo ""
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/api/auth/login -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"docker123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
echo "  token ${#TOKEN} 字符"

cat > /tmp/dk_resume.md <<'EOF'
姓名：孙八
项目一：课程问答助手
做法：RAG，切块500字
结果：召回从71%提到82%
EOF
DOC=$(curl -s -X POST http://127.0.0.1:8080/api/documents/upload \
  -H "Authorization: Bearer $TOKEN" -F "file=@/tmp/dk_resume.md" -F "kind=resume" \
  | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  doc=$DOC"

if [ -n "$DOC" ]; then
  for i in $(seq 1 30); do
    S=$(curl -s "http://127.0.0.1:8080/api/documents/$DOC/status" -H "Authorization: Bearer $TOKEN" \
      | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
    [ "$S" = "ready" ] && { echo "  向量化就绪（${i}s）"; break; }
    sleep 1
  done
fi

echo ""
echo "=========== 6) 组件健康明细 ==========="
curl -s --max-time 15 http://127.0.0.1:8080/api/health \
  | python3 -c "
import json,sys
d = json.load(sys.stdin)
for k, v in (d.get('components') or {}).items():
    ok = '✓' if v.get('ok') else '✗'
    print(f'  {ok} {k}: ' + json.dumps({kk: vv for kk, vv in v.items() if kk != \"ok\"}, ensure_ascii=False)[:100])
" 2>/dev/null

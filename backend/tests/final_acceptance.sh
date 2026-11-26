#!/usr/bin/env bash
# 最终验收：真实模型完整一场 + 报告 + 成本 + 重启续跑
set -u
PROJ=/mnt/e/桌面/实习项目/interview_agent
BASE=http://127.0.0.1:8080
J='Content-Type: application/json'
U="final_$RANDOM$RANDOM"

echo "############ 1) 注册登录 ############"
curl -s -X POST "$BASE/api/auth/register" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"final123456\"}" >/dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"final123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "  用户 $U  token ${#TOKEN}"

echo ""
echo "############ 2) 上传简历 ############"
cat > /tmp/fin_resume.md <<'EOF'
姓名：王五   方向：AI 应用

项目一：课程问答助手（2026.03-2026.09）
背景：同学查课程答案要翻 300 份 PDF，平均 8 分钟
做法：RAG，切块 500 字，top20 重排，bge-m3 做 embedding
结果：召回 71% → 82%，P99 延迟 1.8 秒
代价：索引体积 +30%
我的职责：切块策略实验、200 条评测集标注、重排模块实现

项目二：工单自动分类（2026.01-2026.02）
做法：规则兜底 + 小模型微调
结果：准确率 0.86
EOF
UP=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/fin_resume.md" -F "kind=resume")
DOC=$(echo "$UP" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  document_id=$DOC"
for i in $(seq 1 30); do
  S=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$S" = "ready" ] && { echo "  向量化就绪（${i}s）"; break; }
  [ "$S" = "failed" ] && { echo "  向量化失败"; break; }
  sleep 1
done

echo ""
echo "############ 3) 开面试（真实模型）############"
START=$(curl -s -X POST "$BASE/api/sessions" -H "$J" -H "$AUTH" -d "{
  \"resume\":\"\",\"document_id\":\"$DOC\",\"role\":\"ai_algo\",
  \"persona\":\"strict\",\"max_turns\":6,\"mock\":false,\"coach\":false}")
SID=$(echo "$START" | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  session=$SID"
echo "  首问: $(echo "$START" | grep -o '"question":"[^"]*"' | head -1 | cut -d'"' -f4 | head -c 80)"
[ -z "$SID" ] && { echo "  失败: $START" | head -c 300; exit 1; }

echo ""
echo "############ 4) 答 2 轮后杀进程 ############"
for i in 1 2; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H "$J" -H "$AUTH" \
    -d '{"answer":"切块500字是300/500/800三档实测选的，召回从71%到82%，代价是索引体积多30%。"}')
  echo "  第$i轮: $(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4)($(echo "$R" | grep -o '"avg":[0-9.]*' | head -1 | cut -d: -f2))"
done

echo ""
echo "########## 杀后端进程（模拟崩溃）##########"
pkill -f "uvicorn app.main:app" 2>/dev/null && echo "  已杀"
sleep 3
cd "$PROJ" && bash ops.sh api 2>&1 | grep -E '就绪' | head -1

echo ""
echo "############ 5) 重启后继续答（关键）############"
R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H "$J" -H "$AUTH" \
  -d '{"answer":"重排只在top20做，全量重排延迟会到3秒以上，用户等不了。"}')
T=$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)
echo "  当前轮次: $T   verdict: $(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4)"
[ "${T:-0}" -gt 0 ] && echo "  ✓ 重启续跑成功" || echo "  ✗ 重启续跑失败"

echo ""
echo "############ 6) 继续答完 ############"
for i in 4 5 6; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H "$J" -H "$AUTH" \
    -d '{"answer":"200条标注集是我做的，双人校验kappa=0.81。如果数据翻十倍索引先崩，我会做量化压缩。"}')
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  echo "  第$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)轮: $(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4) done=$D"
  [ "$D" = "true" ] && break
done

echo ""
echo "############ 7) 报告 ############"
curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
md = d.get('markdown') or d.get('report') or ''
print(f'  长度: {len(md)} 字符')
print(f'  轮次: {d.get(\"turns\")}  均分: {d.get(\"avg\")}')
print(f'  结论: {d.get(\"verdict_counts\")}')
print(f'  来自DB重建: {d.get(\"rebuilt_from_db\")}')
c = d.get('cost') or {}
if c:
    print(f'  成本: {c.get(\"tokens\")} token / {c.get(\"cost_cny\")} 元 / {c.get(\"calls\")} 次调用')
print()
print('  --- 报告前 600 字 ---')
print(md[:600])
" 2>/dev/null

echo ""
echo "############ 8) 长期画像（下次开场会用到）############"
docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT weak_elements::text || ' | ' || weak_topics::text || ' | 场次 ' || sessions_count
   FROM candidate_profile WHERE username='$U'" 2>/dev/null | head -2

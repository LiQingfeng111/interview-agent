#!/usr/bin/env bash
# 图驱动版端到端测试：开会话 → 答几轮 → 看报告
set -u
BASE=${BASE:-http://127.0.0.1:8080}
J='Content-Type: application/json'

U="gtest_$RANDOM$RANDOM"
echo "=== 1) 注册 $U ==="
curl -s -X POST "$BASE/api/auth/register" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"graph123456\"}" | head -c 150
echo

echo "=== 2) 登录 ==="
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"graph123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
echo "  token ${#TOKEN} 字符"
AUTH="Authorization: Bearer $TOKEN"

echo "=== 3) 开面试（mock 模式，不花 token）==="
START=$(curl -s -X POST "$BASE/api/sessions" -H "$J" -H "$AUTH" -d '{
  "resume":"项目一：课程问答助手（2026.03-2026.09）\n背景：同学找答案要翻300份PDF，平均8分钟\n做法：RAG检索，切块500字，top20重排\n结果：召回从71%提到82%，P99延迟1.8秒\n我负责：切块策略实验、评测集标注\n\n项目二：智能客服工单分类\n做法：规则兜底+小模型微调\n结果：准确率0.86",
  "role":"ai_algo","persona":"balanced","max_turns":6,"mock":true,"coach":false
}')
echo "$START" | head -c 600
SID=$(echo "$START" | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo
echo "  session_id = $SID"

if [ -z "$SID" ]; then
  echo "  开会话失败，中止"
  exit 1
fi

echo ""
echo "=== 4) 连续答题（看每轮评分与下一问）==="
for i in 1 2 3; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H "$J" -H "$AUTH" \
    -d '{"answer":"召回从71%提到82%，切块500字，300/500/800三档实测过，代价是索引体积多30%。"}')
  V=$(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4)
  A=$(echo "$R" | grep -o '"avg":[0-9.]*' | head -1 | cut -d: -f2)
  Q=$(echo "$R" | grep -o '"question":"[^"]*"' | head -1 | cut -d'"' -f4 | head -c 60)
  T=$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  echo "  第$T轮: verdict=$V avg=$A done=$D"
  echo "         下一问: $Q"
  if [ "$D" = "true" ]; then break; fi
done

echo ""
echo "=== 5) 结束并取报告 ==="
curl -s -X POST "$BASE/api/sessions/$SID/finish" -H "$J" -H "$AUTH" \
  -d '{"reason":"测试完成"}' | head -c 300
echo
REP=$(curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH")
echo "$REP" | head -c 400
echo
echo ""
echo "=== 6) 历史列表 ==="
curl -s "$BASE/api/sessions?page=1&size=5" -H "$AUTH" | head -c 400
echo

#!/usr/bin/env bash
# 端到端验收：模拟一个真实用户从注册到拿报告的完整路径
#
# 这是"项目能跑"的最终证据。任何一步失败都说明有真实问题。
set -u
BASE=http://127.0.0.1:8080
U="e2e_$RANDOM$RANDOM"

pass=0
fail=0

check() {
  local name="$1" got="$2" want="$3"
  if [ "$got" = "$want" ]; then
    echo "  ✓ $name"
    pass=$((pass + 1))
  else
    echo "  ✗ $name（期望 $want，实际 $got）"
    fail=$((fail + 1))
  fi
}

echo "=============================================="
echo " 端到端验收"
echo "=============================================="

echo ""
echo "▸ 0) 服务就绪"
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/health")
check "健康检查" "$code" "200"

echo ""
echo "▸ 1) 注册与登录"
curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"e2e123456\"}" -o /dev/null

LOGIN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"e2e123456\"}")
TOKEN=$(echo "$LOGIN" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
[ -n "$TOKEN" ] && check "登录拿到 token" "yes" "yes" || check "登录拿到 token" "no" "yes"
AUTH="Authorization: Bearer $TOKEN"

code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/auth/me" -H "$AUTH")
check "带 token 访问 /me" "$code" "200"

code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/sessions" -H 'Content-Type: application/json' -X POST -d '{}')
check "不带 token 被拒" "$code" "401"

echo ""
echo "▸ 2) 题库"
STATS=$(curl -s --max-time 10 "$BASE/api/bank/stats" -H "$AUTH")
TOTAL=$(echo "$STATS" | grep -o '"total":[0-9]*' | cut -d: -f2)
[ "${TOTAL:-0}" -gt 700 ] && check "题库已加载（$TOTAL 题）" "yes" "yes" || check "题库已加载" "no($TOTAL)" "yes"

HITS=$(curl -s --max-time 20 -X POST "$BASE/api/bank/search" -H 'Content-Type: application/json' -H "$AUTH" \
  -d '{"q":"多头注意力每个头的维度","k":3}' | grep -o '"score"' | wc -l)
[ "${HITS:-0}" -gt 0 ] && check "语义检索有结果（$HITS 条）" "yes" "yes" || check "语义检索有结果" "no" "yes"

echo ""
echo "▸ 3) 简历上传与向量化"
cat > /tmp/e2e_resume.md <<'EOF'
姓名：验收测试
项目一：课程问答助手（2026.03-2026.09）
背景：同学找课程答案要翻 300 份 PDF，平均 8 分钟
做法：RAG 检索，切块 500 字，top20 重排，bge-m3 做 embedding
结果：召回从 71% 提到 82%，P99 延迟 1.8 秒
代价：索引体积增加 30%
我负责：切块策略实验、评测集标注、重排模块实现
EOF

DOC=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/e2e_resume.md" -F "kind=resume" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$DOC" ] && check "上传成功" "yes" "yes" || check "上传成功" "no" "yes"

STATUS=""
for _ in $(seq 1 30); do
  STATUS=$(curl -s --max-time 5 "$BASE/api/documents/$DOC/status" -H "$AUTH" \
    | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$STATUS" = "ready" ] && break
  [ "$STATUS" = "failed" ] && break
  sleep 1
done
check "向量化完成" "$STATUS" "ready"

echo ""
echo "▸ 4) 面试（mock 模式，验证流程而非模型质量）"
cat > /tmp/e2e_start.json <<EOF
{"resume":"","document_id":"$DOC","role":"ai_algo","persona":"balanced","max_turns":6,"mock":true,"coach":true}
EOF
START=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' -H "$AUTH" \
  --data-binary @/tmp/e2e_start.json)
SID=$(echo "$START" | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$SID" ] && check "会话已创建" "yes" "yes" || check "会话已创建" "no" "yes"

Q1=$(echo "$START" | grep -o '"question":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$Q1" ] && check "首问已生成" "yes" "yes" || check "首问已生成" "no" "yes"

echo "▸ 5) 逐轮作答"
cat > /tmp/e2e_ans.json <<'EOF'
{"answer":"切块 500 字是 300/500/800 三档实测选的，召回从 71% 到 82%，代价是索引体积多 30%。"}
EOF

turns_done=0
for i in $(seq 1 8); do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/e2e_ans.json)
  T=$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  [ -n "$T" ] && turns_done=$T
  [ "$D" = "true" ] && break
done
[ "$turns_done" -gt 0 ] && check "推进了 $turns_done 轮" "yes" "yes" || check "推进轮次" "no" "yes"

# 五维分数
DIMS=$(echo "$R" | grep -o '"dims":{[^}]*}' | head -1)
[ -n "$DIMS" ] && check "五维评分存在" "yes" "yes" || check "五维评分存在" "no" "yes"

# 点评卡
CARD=$(echo "$R" | grep -o '"card":{[^}]*}' | head -1)
[ -n "$CARD" ] && check "coach 点评卡存在" "yes" "yes" || check "coach 点评卡存在" "no" "yes"

echo ""
echo "▸ 6) 报告"
REP=$(curl -s --max-time 20 "$BASE/api/sessions/$SID/report" -H "$AUTH")
MDLEN=$(echo "$REP" | wc -c)
[ "$MDLEN" -gt 500 ] && check "报告有内容（${MDLEN} 字节）" "yes" "yes" || check "报告有内容" "no($MDLEN)" "yes"

RDIMS=$(echo "$REP" | grep -o '"dims":{[^}]*}' | head -1)
[ -n "$RDIMS" ] && check "报告含五维数据（雷达图可用）" "yes" "yes" || check "报告含五维数据" "no" "yes"

echo ""
echo "▸ 7) 历史与权限隔离"
HIST=$(curl -s --max-time 10 "$BASE/api/sessions" -H "$AUTH")
HN=$(echo "$HIST" | grep -o '"session_id"' | wc -l)
[ "${HN:-0}" -gt 0 ] && check "历史列表有记录" "yes" "yes" || check "历史列表有记录" "no" "yes"

# 另一个用户不能看这个会话
U2="e2e_other_$RANDOM"
curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U2\",\"password\":\"e2e123456\"}" -o /dev/null
T2=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U2\",\"password\":\"e2e123456\"}" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$BASE/api/sessions/$SID" -H "Authorization: Bearer $T2")
[ "$code" = "403" ] || [ "$code" = "404" ] && check "他人会话被隔离（$code）" "yes" "yes" || check "他人会话被隔离" "no($code)" "yes"

echo ""
echo "=============================================="
echo " 通过 $pass / 失败 $fail"
echo "=============================================="
[ "$fail" -eq 0 ] && exit 0 || exit 1

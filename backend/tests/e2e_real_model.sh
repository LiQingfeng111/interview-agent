#!/usr/bin/env bash
# 真实模型完整一场面试（不 mock）。花 token，但能验证端到端真实质量。
set -u
BASE=${BASE:-http://127.0.0.1:8080}
J='Content-Type: application/json'
U="real_$RANDOM$RANDOM"

echo "=========== 1) 注册登录 ==========="
curl -s -X POST "$BASE/api/auth/register" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"real123456\"}" >/dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H "$J" \
  -d "{\"user\":\"$U\",\"password\":\"real123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "  token ${#TOKEN} 字符  用户 $U"

echo ""
echo "=========== 2) 上传简历并等向量化 ==========="
cat > /tmp/real_resume.md <<'EOF'
姓名：李雷   求职方向：AI 应用开发

项目一：课程问答助手（2026.03 - 2026.09）
背景：同学找课程答案要翻 300 份 PDF，平均 8 分钟
做法：RAG 检索，切块 500 字，top20 重排，用 bge-m3 做 embedding
结果：召回从 71% 提到 82%，P99 延迟 1.8 秒
代价：索引体积增加 30%
我负责：切块策略实验、评测集标注、重排模块

项目二：智能客服工单分类（2026.01 - 2026.02）
背景：客服每天涌入 2000 工单，人工分类平均 3 分钟
做法：先用规则兜底，再接小模型微调
结果：分类准确率 0.86
EOF

UP=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/real_resume.md" -F "kind=resume")
DOC=$(echo "$UP" | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  document_id = $DOC"
for i in $(seq 1 30); do
  ST=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH")
  S=$(echo "$ST" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  if [ "$S" = "ready" ] || [ "$S" = "failed" ]; then
    echo "  状态: $ST" | head -c 160; echo
    break
  fi
  sleep 1
done

echo ""
echo "=========== 3) 开面试（真实模型，6 轮）==========="
T0=$(date +%s)
START=$(curl -s -X POST "$BASE/api/sessions" -H "$J" -H "$AUTH" -d "{
  \"resume\":\"\",\"document_id\":\"$DOC\",
  \"role\":\"ai_algo\",\"persona\":\"balanced\",\"max_turns\":6,
  \"mock\":false,\"web\":false,\"coach\":false
}")
T1=$(date +%s)
SID=$(echo "$START" | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
echo "  耗时 $((T1-T0)) 秒  session=$SID"
echo "  首问: $(echo "$START" | grep -o '"question":"[^"]*"' | head -1 | cut -d'"' -f4)"

if [ -z "$SID" ]; then
  echo "  开会话失败：$START" | head -c 400
  exit 1
fi

echo ""
echo "=========== 4) 逐轮作答（真实评分）==========="
i=1
for ANS in \
  "召回从71%提到82%，切块500字是在300/500/800三档里实测选的。代价是索引体积多了30%。" \
  "重排用的是bge-reranker，只在top20上做。因为全量重排延迟会到3秒以上，用户等不了。" \
  "评测集是200条人工标注的问答对，我负责写标注规范并做了双人校验，一致性kappa是0.81。" \
  "如果数据量翻十倍，索引会先崩。我的方案是先做量化压缩，再把冷数据下沉到磁盘索引。" \
  "这个项目我最大的失误是初期没做好baseline测量，导致后来无法证明改进幅度。" \
  "如果重做，我会先花两天做评测基建，再动工程实现。"
do
  T0=$(date +%s)
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H "$J" -H "$AUTH" \
    -d "{\"answer\":\"$ANS\"}")
  T1=$(date +%s)

  TURN=$(echo "$R" | grep -o '"turn":[0-9]*' | head -1 | cut -d: -f2)
  V=$(echo "$R" | grep -o '"verdict":"[^"]*"' | head -1 | cut -d'"' -f4)
  AVG=$(echo "$R" | grep -o '"avg":[0-9.]*' | head -1 | cut -d: -f2)
  WEAK=$(echo "$R" | grep -o '"weakest":"[^"]*"' | head -1 | cut -d'"' -f4)
  EVID=$(echo "$R" | grep -o '"evidence":"[^"]*"' | head -1 | cut -d'"' -f4 | head -c 40)
  DONE=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  PHASE=$(echo "$R" | grep -o '"phase":"[^"]*"' | head -1 | cut -d'"' -f4)
  Q=$(echo "$R" | grep -o '"question":"[^"]*"' | head -1 | cut -d'"' -f4)

  echo "  第$TURN轮 [$((T1-T0))s] $V($AVG) 阶段=$PHASE 弱项=$WEAK"
  echo "    证据: $EVID"
  if [ -n "$Q" ]; then echo "    下一问: $(echo "$Q" | head -c 70)"; fi
  if [ "$DONE" = "true" ]; then echo "    → 面试结束"; break; fi
  i=$((i+1))
done

echo ""
echo "=========== 5) 报告 ============"
REP=$(curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH")
echo "$REP" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
dims = d.get('dims') or {}
md = d.get('markdown') or d.get('report') or ''
print('  分维度:', json.dumps(dims, ensure_ascii=False))
print('  报告长度:', len(md), '字符')
print('  --- 报告前 500 字 ---')
print(md[:500])
" 2>/dev/null || echo "$REP" | head -c 500

echo ""
echo "=========== 6) 成本核对（PG 里的 llm 调用记录）==========="
docker exec ia-pg psql -U ia -d interview_agent -t -c "
SELECT COUNT(*) || ' 轮, 总token ' || COALESCE(SUM(tokens_in+tokens_out),0)
       || ', 总成本 ' || ROUND(COALESCE(SUM(cost_cny),0)::numeric, 4) || ' 元'
FROM turns WHERE session_id = '$SID';" 2>/dev/null | head -2

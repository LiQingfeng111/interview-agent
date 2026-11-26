#!/usr/bin/env bash
# 真模型端到端：验证历史上下文 + 双轨评分在真实面试里生效
#
# ## 要观察什么
#
# 1. 每轮的 `score_source` —— 确认 accuracy/boundary/clarity 走了双轨
# 2. 后面几轮说重复的话，accuracy/depth 是否被扣分
# 3. 报告里能否看到 repeated 标记
set -u
BASE=http://127.0.0.1:8080
U="dual_$RANDOM$RANDOM"

echo "############ 准备 ############"
curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"dual123456\"}" -o /dev/null
TOKEN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"dual123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
AUTH="Authorization: Bearer $TOKEN"
echo "  用户 $U"

cat > /tmp/dual_resume.md <<'EOF'
姓名：测试用户
项目一：课程问答助手（2026.03-2026.09）
做法：RAG 检索，切块 500 字，top20 重排，bge-m3 做 embedding
结果：召回从 71% 提到 82%，P99 延迟 1.8 秒
代价：索引体积增加 30%
我负责：切块策略实验、评测集标注、重排模块实现
EOF
DOC=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/dual_resume.md" -F "kind=resume" \
  | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
for i in $(seq 1 25); do
  S=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$S" = "ready" ] && { echo "  简历就绪（${i}s）"; break; }
  sleep 1
done

echo ""
echo "############ 面试（真模型，8 轮）############"
cat > /tmp/dual_start.json <<EOF
{"resume":"","document_id":"$DOC","role":"ai_algo","persona":"balanced","max_turns":8,"mock":false}
EOF
SID=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' -H "$AUTH" \
  --data-binary @/tmp/dual_start.json | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -z "$SID" ] && { echo "  开会话失败"; exit 1; }
echo "  session=$SID"

# 第 1-3 轮：给有数字的实质回答
# 第 4-5 轮：**故意重复第 1 轮的话** → 应该被扣分
echo ""
echo "▸ 第 1-3 轮：实质回答（有数字）"
cat > /tmp/ans_solid.json <<'EOF'
{"answer":"切块 500 字是 300/500/800 三档实测选的。召回从 71% 提到 82%，P99 延迟 1.8 秒，代价是索引体积增加 30%。"}
EOF
for i in 1 2 3; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/ans_solid.json)
  echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
ev = d.get('eval') or {}
src = ev.get('score_source') or {}
sig = ev.get('rule_signals') or {}
print(f\"  第{d.get('turn')}轮 {ev.get('verdict')} avg={ev.get('avg')} \"
      f\"acc={ev.get('accuracy')} dep={ev.get('depth')} \"
      f\"repeated={ev.get('repeated')} metrics={sig.get('metrics')}\")
if src:
    print(f'      来源: acc={src.get(\"accuracy\",\"?\")} clarity={src.get(\"clarity\",\"?\")}')
" 2>/dev/null
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  [ "$D" = "true" ] && break
done

echo ""
echo "▸ 第 4-5 轮：故意重复上面说过的话（应被扣分）"
for i in 4 5; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/ans_solid.json)
  echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
ev = d.get('eval') or {}
print(f\"  第{d.get('turn')}轮 avg={ev.get('avg')} acc={ev.get('accuracy')} \"
      f\"dep={ev.get('depth')} repeated={ev.get('repeated')}\")
" 2>/dev/null
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  [ "$D" = "true" ] && break
done

echo ""
echo "▸ 第 6+ 轮：全新的实质内容（讲失败方案）"
cat > /tmp/ans_new.json <<'EOF'
{"answer":"失败方案我踩过坑：一开始用固定窗口切块，跨段落的语义被切断，召回只有 71%。后来改成按标题层级切，再补一层 parent_text 保留上下文，才到 82%。这个方案只在文档结构清晰时有效，遇到扫描件就失效了。"}
EOF
for i in 6 7 8; do
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary @/tmp/ans_new.json)
  echo "$R" | python3 -c "
import json,sys
d = json.load(sys.stdin).get('data') or {}
ev = d.get('eval') or {}
sig = ev.get('rule_signals') or {}
print(f\"  第{d.get('turn')}轮 avg={ev.get('avg')} acc={ev.get('accuracy')} \"
      f\"bnd={ev.get('boundary')} repeated={ev.get('repeated')} \"
      f\"boundary词={sig.get('boundary')}\")
" 2>/dev/null
  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  [ "$D" = "true" ] && break
done

echo ""
echo "############ DB 里的逐轮记录 ############"
docker exec ia-pg psql -U ia -d interview_agent -c \
  "SELECT seq, verdict, ROUND(score::numeric,2) AS avg,
          accuracy AS acc, depth AS dep, boundary AS bnd
   FROM turns WHERE session_id='$SID' ORDER BY seq" 2>/dev/null | head -14

echo ""
echo "############ 结论 ############"
echo "  观察点："
echo "    1. 有 score_source → 双轨生效"
echo "    2. 第 4-5 轮 repeated=True 且 acc/dep 下降 → 历史感知生效"
echo "    3. 第 6+ 轮 bnd 上升 → boundary 规则检测生效"

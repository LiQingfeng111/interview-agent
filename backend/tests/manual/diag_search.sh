#!/usr/bin/env bash
# 诊断：题库检索为什么返回空
set -u
BASE=http://127.0.0.1:8080
U="srch_$RANDOM$RANDOM"

curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"srch123456\"}" -o /dev/null
T=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"srch123456\"}" \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

echo "=== 1) 原始响应 ==="
cat > /tmp/srch.json <<'EOF'
{"q":"多头注意力每个头的维度","k":3}
EOF
curl -s -X POST "$BASE/api/bank/search" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $T" --data-binary @/tmp/srch.json | head -c 500
echo ""

echo ""
echo "=== 2) 换个查询（更通用的词）==="
cat > /tmp/srch2.json <<'EOF'
{"q":"RAG 检索增强生成","k":3}
EOF
curl -s -X POST "$BASE/api/bank/search" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $T" --data-binary @/tmp/srch2.json | head -c 400
echo ""

echo ""
echo "=== 3) 直接用诊断接口看阈值与命中 ==="
cat > /tmp/srch3.json <<'EOF'
{"q":"多头注意力每个头的维度","k":5,"use":"select"}
EOF
curl -s -X POST "$BASE/api/diagnostics/rag/search" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $T" --data-binary @/tmp/srch3.json | head -c 600
echo ""

echo ""
echo "=== 4) 数据库里向量维度是否正常 ==="
docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT COUNT(*), COUNT(embedding), MIN(vector_dims(embedding)), MAX(vector_dims(embedding))
   FROM bank_questions" 2>/dev/null

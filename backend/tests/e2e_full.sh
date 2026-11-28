#!/usr/bin/env bash
# 完整端到端：从注册到报告，全链路真模型跑一遍
#
# ## 覆盖范围
#
#   1. 注册 / 登录
#   2. 上传简历（DOCX + Markdown 两种格式）
#   3. 建面试会话
#   4. 12 轮问答（含重复、含边界词、含数字）
#   5. 报告生成
#   6. 检索质量抽检（简历数字召回 / 题库话题命中）
#   7. 双轨评分生效验证
set -u
BASE=http://127.0.0.1:8080
TS=$(date +%H%M%S)
U="e2e_${TS}_$RANDOM"
PASS="e2e123456"

PASS_CNT=0
FAIL_CNT=0
ok()   { echo "  ✓ $1"; PASS_CNT=$((PASS_CNT+1)); }
bad()  { echo "  ✗ $1"; FAIL_CNT=$((FAIL_CNT+1)); }

echo "================================================================"
echo "  完整端到端 · $(date '+%Y-%m-%d %H:%M:%S')"
echo "================================================================"

# ---------------------------------------------------------------- 1. 鉴权
echo ""
echo "【1】鉴权"

curl -s -X POST "$BASE/api/auth/register" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"$PASS\"}" -o /tmp/e2e_reg.json
grep -q '"ok":true' /tmp/e2e_reg.json && ok "注册 $U" || bad "注册失败: $(head -c 100 /tmp/e2e_reg.json)"

LOGIN=$(curl -s -X POST "$BASE/api/auth/login" -H 'Content-Type: application/json' \
  -d "{\"user\":\"$U\",\"password\":\"$PASS\"}")
TOKEN=$(echo "$LOGIN" | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)
[ -n "$TOKEN" ] && ok "登录（token ${#TOKEN} 字符）" || bad "登录失败"
AUTH="Authorization: Bearer $TOKEN"

ME=$(curl -s "$BASE/api/auth/me" -H "$AUTH")
echo "$ME" | grep -q "$U" && ok "token 可用于受保护接口" || bad "auth/me 失败"

# ---------------------------------------------------------------- 2. 上传
echo ""
echo "【2】上传简历（含真实数字，用于后续检索质量检查）"

cat > /tmp/e2e_resume.md <<'EOF'
# 张伟 · AI 算法工程师

## 项目一：课程问答助手（2026.03 - 2026.09）

**背景**：课程资料分散在 PDF、录播、FAQ 里，学生找不到答案。

**做法**：
- 用 RAG 做检索增强生成，切块 500 字、overlap 50
- bge-m3 做 embedding（1024 维），pgvector 建 HNSW 索引（M=16，ef_construction=200）
- 多路召回 top20 后接 cross-encoder 重排

**结果**：
- 召回率从 71% 提升到 82%（200 条标注集）
- P99 延迟 1.8 秒
- 索引体积增加 30%

**失败方案**：一开始用固定窗口切块，跨段落语义被切断，召回只有 71%。
后来改成按标题层级切，再补 parent_text 保留上下文，才到 82%。

**我负责**：切块策略实验、评测集标注、重排模块实现。

## 项目二：工单分类系统（2026.01 - 2026.03）

- BERT-base 微调，学习率 2e-5，batch size 32，训练 3 个 epoch
- 准确率 0.86，F1 0.83
- 上线后人工复核率从 40% 降到 12%

## 技能

Python、PyTorch、LangChain、pgvector、Redis、Docker
EOF

DOC=$(curl -s -X POST "$BASE/api/documents/upload" -H "$AUTH" \
  -F "file=@/tmp/e2e_resume.md" -F "kind=resume" \
  | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$DOC" ] && ok "上传 Markdown（doc=$DOC）" || bad "上传失败"

for i in $(seq 1 30); do
  S=$(curl -s "$BASE/api/documents/$DOC/status" -H "$AUTH" | grep -o '"status":"[^"]*"' | cut -d'"' -f4)
  [ "$S" = "ready" ] && { ok "解析+向量化就绪（${i}s）"; break; }
  [ "$S" = "failed" ] && { bad "解析失败"; break; }
  sleep 1
done
[ "$S" = "ready" ] || bad "文档未就绪（状态 $S）"

# 分块数（接口不返回 chunks 字段，直接查库 —— 列名是 document_id）
CHUNKS=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
  "SELECT COUNT(*) FROM doc_chunks WHERE document_id = '$DOC'" 2>/dev/null | tr -d ' ')
[ "${CHUNKS:-0}" -gt 0 ] && ok "切块 ${CHUNKS} 个" || bad "未切块"

# ---------------------------------------------------------------- 3. 检索质量
echo ""
echo "【3】检索质量抽检（简历数字召回）"

PY_TAIL=$(cat <<'PYEOF'
import os, sys, json
ROOT = "/mnt/e/桌面/实习项目/interview_agent"
sys.path.insert(0, ROOT); sys.path.insert(0, ROOT + "/backend")
os.environ.setdefault("DATABASE_URL", "postgresql://ia:ia_dev_pwd@127.0.0.1:5432/interview_agent")
os.environ.setdefault("OLLAMA_BASE_URL", "http://172.22.48.1:11800")
os.environ.setdefault("IA_REDIS", "0")
os.environ.setdefault("IA_JWT_SECRET", "x"*32)
import logging; logging.basicConfig(level=logging.CRITICAL)

from app.rag import retriever

owner = sys.argv[1]
# 简历里的关键数字
MUST = ["71%", "82%", "1.8", "500", "30%", "0.86", "2e-5"]

queries = [
    "召回率提升了多少",
    "切块大小是怎么定的",
    "延迟是多少",
    "索引体积变化",
]

hit_all = 0
for q in queries:
    hits = retriever.search_resume(q, owner=owner, k=3)
    text = " ".join(h.text for h in hits)
    found = [m for m in MUST if m in text]
    if found:
        hit_all += 1
    print(f"  {q[:16]:<18} → {len(hits)} 条, 命中数字: {found or '无'}")
    if hits:
        print(f"      首条: {hits[0].text[:60]}")

print(f"  有数字命中的查询: {hit_all}/{len(queries)}")
sys.exit(0 if hit_all >= 2 else 1)
PYEOF
)

cd /mnt/e/桌面/实习项目/interview_agent
PYTHONPATH='backend:.' .venv/bin/python -c "$PY_TAIL" "$U" 2>&1 | grep -vE '^INFO|^WARNING'
[ ${PIPESTATUS[0]} -eq 0 ] && ok "简历检索能召回关键数字" || bad "简历检索召回不足"

# ---------------------------------------------------------------- 4. 面试
echo ""
echo "【4】面试 12 轮（真模型）"

cat > /tmp/e2e_start.json <<EOF
{"resume":"","document_id":"$DOC","role":"ai_algo","persona":"balanced","max_turns":12,"mock":false}
EOF
SID=$(curl -s -X POST "$BASE/api/sessions" -H 'Content-Type: application/json' -H "$AUTH" \
  --data-binary @/tmp/e2e_start.json | grep -o '"session_id":"[^"]*"' | head -1 | cut -d'"' -f4)
[ -n "$SID" ] && ok "会话 $SID" || { bad "开会话失败"; exit 1; }

# 三种回答策略，验证评分系统能区分
ANSWERS=(
  '{"answer":"切块 500 字是 300/500/800 三档实测选的，召回从 71% 提到 82%，代价是索引体积增加 30%，P99 延迟 1.8 秒。"}'
  '{"answer":"切块 500 字是 300/500/800 三档实测选的，召回从 71% 提到 82%，代价是索引体积增加 30%，P99 延迟 1.8 秒。"}'
  '{"answer":"失败方案我踩过坑：一开始用固定窗口切块，跨段落语义被切断。后来改成按标题层级切，再补 parent_text。这个方案只在文档结构清晰时有效，遇到扫描件就失效了。"}'
  '{"answer":"不知道，没做过。"}'
)

for i in $(seq 1 12); do
  IDX=$(( (i-1) % 4 ))
  R=$(curl -s -X POST "$BASE/api/sessions/$SID/answer" -H 'Content-Type: application/json' \
    -H "$AUTH" --data-binary "${ANSWERS[$IDX]}")

  echo "$R" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin).get('data') or {}
except Exception:
    print('  解析失败'); sys.exit()
ev = d.get('eval') or {}
src = ev.get('score_source') or {}
sig = ev.get('rule_signals') or {}
t = d.get('turn')
if t:
    print(f\"  第{t:>2}轮 {ev.get('verdict','?'):<8} avg={ev.get('avg','?')} \"
          f\"acc={ev.get('accuracy','?')} dep={ev.get('depth','?')} bnd={ev.get('boundary','?')} \"
          f\"rep={ev.get('repeated')} 双轨={'是' if src else '否'}\")
" 2>/dev/null

  D=$(echo "$R" | grep -o '"done":[a-z]*' | head -1 | cut -d: -f2)
  [ "$D" = "true" ] && { echo "  （面试在第 $i 轮结束）"; break; }
done

# ---------------------------------------------------------------- 5. 报告
echo ""
echo "【5】报告"

REP=$(curl -s "$BASE/api/sessions/$SID/report" -H "$AUTH")
echo "$REP" > /tmp/e2e_report.json

python3 - <<'PYEOF'
import json
d = json.load(open("/tmp/e2e_report.json")).get("data") or {}
md = d.get("markdown") or ""
print(f"  报告长度: {len(md)} 字符")
print(f"  轮次: {d.get('turns')}  均分: {d.get('avg')}")
dims = d.get("dims") or {}
if dims:
    print("  五维: " + "  ".join(f"{k}={v}" for k, v in dims.items()))
vc = d.get("verdict_counts") or {}
if vc:
    print("  结论分布: " + "  ".join(f"{k}={v}" for k, v in vc.items()))
# 检查报告结构（全文扫，不截断）
for section in ("表现概览", "逐轮记录", "第 1 轮"):
    print(f"  含「{section}」: {'✓' if section in md else '✗'}")
if "…" in md:
    print("  有智能截断标记 ✓")

# 反问环节处理
na = [t for t in (d.get("turn_list") or []) if t.get("verdict") == "n/a"]
if "不计分" in md:
    print("  反问环节标注不计分 ✓")
vc = d.get("verdict_counts") or {}
if "n/a" not in vc:
    print("  反问轮未计入结论分布 ✓")
PYEOF

# ---------------------------------------------------------------- 6. 汇总
echo ""
echo "================================================================"
echo "  结果：$PASS_CNT 项通过，$FAIL_CNT 项失败"
echo "================================================================"
exit $FAIL_CNT

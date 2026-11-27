#!/usr/bin/env bash
# 检查所有依赖是否就绪
set -u
echo "=== PostgreSQL ==="
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
  echo "  容器运行中"
  N=$(docker exec ia-pg psql -U ia -d interview_agent -t -A -c \
    "SELECT COUNT(*) FROM bank_questions" 2>/dev/null | tr -d ' ')
  echo "  题库: ${N:-查询失败}"
else
  echo "  ✗ 容器未运行"
fi

echo ""
echo "=== Ollama ==="
GW=$(ip route show default 2>/dev/null | awk '{print $3}')
echo "  网关: $GW"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://$GW:11800/api/tags" 2>/dev/null || echo 000)
echo "  /api/tags -> $CODE"

if [ "$CODE" = "200" ]; then
  curl -s --max-time 5 "http://$GW:11800/api/tags" | python3 -c "
import json,sys
d = json.load(sys.stdin)
for m in d.get('models', []):
    print('    ' + m['name'])
" 2>/dev/null
fi

echo ""
echo "=== 本地包 ==="
cd /mnt/e/桌面/实习项目/interview_agent || exit 1
.venv/bin/python - <<'PY'
for mod in ("rank_bm25", "jieba", "torch", "transformers",
            "sentence_transformers"):
    try:
        m = __import__(mod)
        print(f"  ✓ {mod}: {getattr(m, '__version__', 'ok')}")
    except ImportError:
        print(f"  ✗ {mod}")
PY

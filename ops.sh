#!/usr/bin/env bash
# ============================================================================
# 一键启动 / 运维脚本（替代之前散落的 20+ 个一次性脚本）
#
# 用法：
#   ./ops.sh up        起 PostgreSQL + 后端（幂等，重复执行安全）
#   ./ops.sh db        只起数据库
#   ./ops.sh schema    建表 + 向量索引（幂等）
#   ./ops.sh bank      导入 722 题（断点续跑，已有向量的不重算）
#   ./ops.sh api       只起后端
#   ./ops.sh stop      停后端
#   ./ops.sh status    各部分状态
#   ./ops.sh e2e       端到端冒烟测试
#   ./ops.sh test      跑全部测试
#
# 设计原则：所有操作幂等。重复执行不会破坏已有数据、不会重复算向量。
# ============================================================================
set -u

PROJ=/mnt/e/桌面/实习项目/interview_agent
PY="$PROJ/.venv/bin/python"
PG=ia-pg
DB=interview_agent
PGUSER=ia

# 读 .env（如果存在）。放在最前面，后面的 ${VAR:-default} 才能拿到值。
#
# 为什么要这个文件：模型网关和 Ollama 都在 Windows 宿主机，
# 而 WSL 里 127.0.0.1 指向 WSL 自己 —— 必须用宿主 IP。
# 把它写进 .env 而不是硬编码，是因为宿主 IP 会变（WSL 重启后可能不同）。
if [ -f "$PROJ/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$PROJ/.env"
  set +a
fi

export PYTHONPATH="$PROJ/backend:$PROJ"
export DATABASE_URL="postgresql://${PGUSER}:${POSTGRES_PASSWORD:-ia_dev_pwd}@127.0.0.1:5432/${DB}"
export IA_REDIS=0
export IA_LEGACY_ROOT="$PROJ"

ok()   { echo "  ✓ $*"; }
err()  { echo "  ✗ $*"; }
head2(){ echo ""; echo "=== $* ==="; }

# ---------------------------------------------------------------- 数据库
db_start() {
  head2 "PostgreSQL"
  if docker ps --format '{{.Names}}' | grep -q "^${PG}$"; then
    ok "已在运行"
  else
    docker start "$PG" >/dev/null 2>&1 && ok "已启动" || { err "启动失败"; return 1; }
  fi
  for i in $(seq 1 30); do
    if docker exec "$PG" pg_isready -U "$PGUSER" -d "$DB" >/dev/null 2>&1; then
      ok "就绪（${i}s）"
      return 0
    fi
    sleep 1
  done
  err "30 秒内未就绪"
  return 1
}

db_schema() {
  head2 "建表 + 向量索引"
  "$PY" - <<'PYEOF'
import sys, os
sys.path.insert(0, os.environ["PYTHONPATH"].split(":")[0])
sys.path.insert(0, os.environ["IA_LEGACY_ROOT"])
from app.repos.pg import init_schema
r = init_schema()
print(f"  ✓ {r['statements']} 条语句执行完成")
PYEOF
}

db_bank() {
  head2 "导入题库（断点续跑）"
  "$PY" - <<'PYEOF'
import sys, os, time
sys.path.insert(0, os.environ["PYTHONPATH"].split(":")[0])
sys.path.insert(0, os.environ["IA_LEGACY_ROOT"])
from pathlib import Path
from app.rag.ingest import ingest_bank
path = Path(os.environ["IA_LEGACY_ROOT"]) / "data" / "question_bank.jsonl"
t0 = time.time()
r = ingest_bank(path)
print(f"  ✓ 总 {r['total']} 题，本次向量化 {r['embedded']} 题，{r['seconds']} 秒")
PYEOF
}

# ---------------------------------------------------------------- 后端
api_start() {
  head2 "FastAPI 后端"
  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  sleep 1
  cd "$PROJ/backend" || return 1
  setsid nohup "$PY" -m uvicorn app.main:app \
    --host 0.0.0.0 --port 8080 --log-level info \
    > /tmp/fastapi.log 2>&1 < /dev/null &
  echo $! > /tmp/fastapi.pid
  for i in $(seq 1 45); do
    code=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/health 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then
      ok "就绪（${i}s）PID=$(cat /tmp/fastapi.pid)"
      return 0
    fi
    sleep 1
  done
  err "启动超时，日志："
  tail -20 /tmp/fastapi.log
  return 1
}

api_stop() {
  head2 "停止后端"
  pkill -f "uvicorn app.main:app" 2>/dev/null && ok "已停止" || ok "未在运行"
}

# ---------------------------------------------------------------- 状态
status() {
  head2 "组件状态"
  echo -n "  PostgreSQL : "
  docker exec "$PG" pg_isready -U "$PGUSER" -d "$DB" >/dev/null 2>&1 && echo "ok" || echo "down"
  echo -n "  Ollama     : "
  curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$OLLAMA_BASE_URL/api/tags" 2>/dev/null | grep -q 200 && echo "ok" || echo "down"
  echo -n "  FastAPI    : "
  curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:8080/api/health 2>/dev/null | grep -q 200 && echo "ok" || echo "down"
  echo ""
  echo "  数据："
  docker exec "$PG" psql -U "$PGUSER" -d "$DB" -t -c "
    SELECT '    bank_questions  ' || COUNT(*) || ' 行（有向量 ' ||
           COUNT(*) FILTER (WHERE embedding IS NOT NULL) || '）' FROM bank_questions
    UNION ALL SELECT '    documents       ' || COUNT(*) FROM documents
    UNION ALL SELECT '    doc_chunks      ' || COUNT(*) FROM doc_chunks
    UNION ALL SELECT '    users           ' || COUNT(*) FROM users;" 2>/dev/null | grep -v '^$'
}

# ---------------------------------------------------------------- 测试
e2e() {
  head2 "端到端冒烟"
  bash "$PROJ/tools/e2e_test.sh"
}

run_tests() {
  head2 "旧测试（规则层，356+ 条）"
  cd "$PROJ" || return 1
  "$PY" -m pytest -q 2>&1 | tail -3
  head2 "新测试（backend）"
  PYTHONPATH="$PROJ/backend:$PROJ" "$PY" -m pytest backend/tests -q 2>&1 | tail -3
}

# ---------------------------------------------------------------- 分发
case "${1:-help}" in
  up)     db_start && db_schema && api_start ;;
  db)     db_start ;;
  schema) db_schema ;;
  bank)   db_bank ;;
  api)    api_start ;;
  stop)   api_stop ;;
  status) status ;;
  e2e)    e2e ;;
  test)   run_tests ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac

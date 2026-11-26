#!/usr/bin/env bash
# ============================================================================
#  interview-agent 一键启动
#
#  用法：
#    bash start.sh            # 启动全部（DB + 后端 + 前端）
#    bash start.sh api        # 只起后端
#    bash start.sh web        # 只起前端
#    bash start.sh stop       # 全停
#    bash start.sh status     # 看状态
#    bash start.sh check      # 环境体检（不动任何东西）
#
#  ## 这个脚本存在的理由
#
#  之前根目录有 12 个 .bat，全部指向已废弃的 webui 架构，
#  新人（包括三个月后的自己）根本不知道该点哪个，点对了也起不来。
#
#  现在只留这一个入口，把所有启动逻辑收在这里。
#  Windows 用户走 start.bat（它转调本脚本）。
# ============================================================================
set -u

PROJ=$(cd "$(dirname "$0")" && pwd)
cd "$PROJ" || exit 1

# ---------------------------------------------------------------- 颜色
if [ -t 1 ]; then
  C_OK=$'\033[32m'; C_ERR=$'\033[31m'; C_WARN=$'\033[33m'
  C_DIM=$'\033[90m'; C_HL=$'\033[36m'; C_END=$'\033[0m'
else
  C_OK=''; C_ERR=''; C_WARN=''; C_DIM=''; C_HL=''; C_END=''
fi

ok()   { echo "  ${C_OK}✓${C_END} $*"; }
err()  { echo "  ${C_ERR}✗${C_END} $*"; }
warn() { echo "  ${C_WARN}!${C_END} $*"; }
dim()  { echo "  ${C_DIM}$*${C_END}"; }
head2() { echo ""; echo "${C_HL}▸ $*${C_END}"; }

BACKEND_PID=/tmp/ia-backend.pid
FRONTEND_PID=/tmp/ia-frontend.pid

# ---------------------------------------------------------------- 体检
cmd_check() {
  head2 "环境体检"

  local bad=0

  # Python + venv
  if [ -x "$PROJ/.venv/bin/python" ]; then
    ok "Python venv: $("$PROJ/.venv/bin/python" --version 2>&1)"
  else
    err "缺少 .venv（运行：python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt）"
    bad=1
  fi

  # 依赖
  if "$PROJ/.venv/bin/python" -c "import fastapi, uvicorn, psycopg" 2>/dev/null; then
    ok "后端依赖已装"
  else
    err "后端依赖缺失（.venv/bin/pip install -r backend/requirements.txt）"
    bad=1
  fi

  # Docker / PostgreSQL
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
    ok "PostgreSQL 运行中"
  elif docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
    warn "PostgreSQL 容器存在但未运行（start.sh 会自动启动）"
  else
    err "PostgreSQL 容器不存在（需要 Docker；start.sh 会自动创建）"
    bad=1
  fi

  # 宿主机服务（Ollama / 模型网关）
  local gw
  gw=$(ip route show default 2>/dev/null | awk '{print $3}')
  if [ -n "$gw" ]; then
    local ocode
    ocode=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://$gw:11800/api/tags" 2>/dev/null || echo 000)
    if [ "$ocode" = "200" ]; then
      ok "Ollama（$gw:11800）"
    else
      warn "Ollama 不可达 —— 语义检索会失效"
      dim "  Windows 侧执行：powershell -ExecutionPolicy Bypass -File tools\\host_services.ps1"
    fi

    local lcode
    lcode=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://$gw:18336/v1/models" 2>/dev/null || echo 000)
    if [ "$lcode" = "200" ] || [ "$lcode" = "401" ]; then
      ok "模型网关（$gw:18336）"
    else
      warn "模型网关不可达 —— 只能跑 mock 模式"
      dim "  Windows 侧执行：node tools/port_forward.cjs 18336 18326"
    fi
  fi

  # API key
  if grep -qE '^LLM_API_KEY=.+' .env 2>/dev/null; then
    ok "LLM_API_KEY 已配置"
  else
    warn "LLM_API_KEY 未配置 —— 真实模型调用会 401"
  fi

  # 前端依赖
  if [ -d "$PROJ/frontend/node_modules" ]; then
    ok "前端依赖已装"
  else
    warn "前端依赖未装（cd frontend && npm install）"
  fi

  echo ""
  if [ "$bad" -eq 0 ]; then
    echo "  ${C_OK}基础环境就绪${C_END}，可以 bash start.sh"
  else
    echo "  ${C_ERR}有阻塞项，先按上面提示修${C_END}"
  fi
  return "$bad"
}

# ---------------------------------------------------------------- 数据库
ensure_db() {
  head2 "PostgreSQL"
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
    ok "已在运行"
    return 0
  fi

  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q '^ia-pg$'; then
    docker start ia-pg >/dev/null 2>&1 && ok "已启动（复用现有容器）"
  else
    dim "首次创建容器（约 10 秒）…"
    docker run -d --name ia-pg \
      -e POSTGRES_USER=ia -e POSTGRES_PASSWORD=ia_dev_pwd \
      -e POSTGRES_DB=interview_agent \
      -p 5432:5432 -v ia_pgdata:/var/lib/postgresql/data \
      pgvector/pgvector:pg16 >/dev/null 2>&1 && ok "已创建并启动"
  fi

  for _ in $(seq 1 30); do
    if docker exec ia-pg pg_isready -U ia -d interview_agent >/dev/null 2>&1; then
      ok "就绪"
      return 0
    fi
    sleep 1
  done
  err "启动超时"
  return 1
}

# ---------------------------------------------------------------- 后端
cmd_api() {
  ensure_db || return 1

  head2 "后端 API"
  if curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:8080/api/health 2>/dev/null | grep -q 200; then
    ok "已在运行 → http://localhost:8080/docs"
    return 0
  fi

  pkill -f "uvicorn app.main:app" 2>/dev/null || true
  sleep 1

  # 用 ops.sh 的逻辑（它有完整的环境变量装配）
  setsid nohup bash ops.sh api >/tmp/ia-start-api.log 2>&1 < /dev/null &
  echo $! > "$BACKEND_PID"

  for i in $(seq 1 60); do
    if curl -s -o /dev/null -w "%{http_code}" --max-time 2 http://127.0.0.1:8080/api/health 2>/dev/null | grep -q 200; then
      ok "就绪（${i}s）→ http://localhost:8080/docs"
      return 0
    fi
    sleep 1
  done
  err "启动超时，日志："
  tail -25 /tmp/ia-start-api.log 2>/dev/null | sed 's/^/    /'
  return 1
}

# ---------------------------------------------------------------- 工具
# 探测某个端口上是否有 HTTP 服务
#
# ## 为什么要试多个地址（踩过的坑）
#
# 前端跑在 Windows 侧时，Vite 只绑 `localhost`（可能是 ::1 或 127.0.0.1）。
# WSL 里的 `127.0.0.1` 指向 WSL 自己，**看不到 Windows 的端口**。
#
# 实测：Windows 侧 curl http://localhost:5173 返回 200，
# 但 WSL 里 curl 127.0.0.1:5173 一直失败。
# 结果 start.sh 明明服务在跑，却报"前端还没起来"。
#
# 所以按顺序试：本机回环 → WSL 网关（能路由到 Windows）→ ::1。
http_alive() {
  local port=$1
  local gw
  gw=$(ip route show default 2>/dev/null | awk '{print $3}')

  for host in 127.0.0.1 "$gw" "[::1]"; do
    [ -z "$host" ] && continue
    if curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
         "http://$host:$port/" 2>/dev/null | grep -qE '^(200|30[0-9]|40[0-9])$'; then
      return 0
    fi
  done
  return 1
}

# 返回服务实际可访问的地址（给用户看的）
#
# ## 为什么优先显示 localhost 而不是探测到的 IP
#
# 探测时 WSL 走网关（172.x）才能访问 Windows 的前端，
# 但那不代表**用户**该用网关地址——用户就在 Windows 上，
# 用 http://localhost:5173 最自然，复制粘贴到浏览器就能开。
#
# 所以：探测用多个地址兜底，展示时优先用 localhost。
# 只有当 5173 确实绑在别的地址（比如纯 Docker 场景）才显示实际 IP。
reachable_url() {
  local port=$1
  local gw
  gw=$(ip route show default 2>/dev/null | awk '{print $3}')

  # 本机回环能通 → 用户也用 localhost
  if curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
       "http://127.0.0.1:$port/" 2>/dev/null | grep -qE '^(200|30[0-9]|40[0-9])$'; then
    echo "http://localhost:$port"
    return 0
  fi

  # 回环不通（典型：WSL 访问 Windows 上的服务）→ 提示用户用 localhost，
  # 因为用户通常在 Windows 侧打开浏览器
  if [ -n "$gw" ] && curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
       "http://$gw:$port/" 2>/dev/null | grep -qE '^(200|30[0-9]|40[0-9])$'; then
    echo "http://localhost:$port"
    return 0
  fi

  echo "http://localhost:$port"
}

# ---------------------------------------------------------------- 前端
cmd_web() {
  head2 "前端"
  if http_alive 5173; then
    ok "已在运行 → $(reachable_url 5173)"
    return 0
  fi

  if [ ! -d "$PROJ/frontend/node_modules" ]; then
    err "前端依赖未装：cd frontend && npm install"
    return 1
  fi

  # 检测能不能直接跑 vite
  #
  # ## 为什么要判断而不是直接 `npm run dev`
  #
  # 项目经常在 WSL 里跑，但 frontend/node_modules 是**在 Windows 侧**
  # 用 npm install 装的（Windows 版二进制、.cmd shim）。
  # 在 WSL 里执行会报：
  #     sh: 1: vite: not found
  # 或者更迷惑的 `Exec format error`。
  #
  # 所以先探测：能跑就跑，不能跑就明确告诉用户在 Windows 侧启动，
  # 而不是抛个看不懂的错。
  if command -v npm >/dev/null 2>&1 && \
     [ -x "$PROJ/frontend/node_modules/.bin/vite" ] && \
     "$PROJ/frontend/node_modules/.bin/vite" --version >/dev/null 2>&1; then
    dim "启动 Vite dev server…"
    ( cd "$PROJ/frontend" && \
      setsid nohup npm run dev >/tmp/ia-vite.log 2>&1 < /dev/null & )
    echo $! > "$FRONTEND_PID"

    for i in $(seq 1 25); do
      if curl -s -o /dev/null -w "%{http_code}" --max-time 2 \
           http://127.0.0.1:5173/ 2>/dev/null | grep -q 200; then
        ok "就绪（${i}s）→ http://localhost:5173"
        return 0
      fi
      sleep 1
    done
    warn "启动超时，日志："
    tail -12 /tmp/ia-vite.log 2>/dev/null | sed 's/^/    /'
    return 1
  fi

  # 兜底：无法在当前环境跑，给出准确的诊断和两条可执行的路
  #
  # ## 这里必须给出「真实原因」
  #
  # 第一版写的是"node_modules 可能是 Windows 侧装的"，含糊。
  # 实测 WSL 里的报错是：
  #     ./node_modules/.bin/vite: 15: exec: node: not found
  # 也就是 **WSL 没装 Node**（Node 只装在 Windows 侧）。
  # 这种"环境缺东西"和"依赖装错位置"是两个完全不同的问题，
  # 含糊其辞会让用户白折腾。
  if ! command -v node >/dev/null 2>&1; then
    warn "当前环境没有 Node（前端只能跑在有 Node 的那一侧）"
    dim "  当前 PATH 里找不到 node 命令"
    node_hint="$(command -v node 2>/dev/null || echo '')"
    [ -z "$node_hint" ] && dim "  这通常是 WSL 没装 Node，而 Node 装在 Windows 侧"
  else
    warn "node 存在但 vite 跑不起来"
    dim "  可能是 node_modules 与当前平台不匹配（例如 Windows 侧装的包拿到 Linux 用）"
  fi

  dim ""
  dim "  三条可执行的路（任选一条）："
  dim ""
  dim "  A) 在 Windows 侧启动前端（最简单，推荐）"
  dim "       cd frontend"
  dim "       npm run dev"
  dim "     然后打开 http://localhost:5173"
  dim ""
  dim "  B) 双击 start.bat（会自动做 A，并打开浏览器）"
  dim ""
  dim "  C) 用 Docker（不依赖本机 Node）"
  dim "       docker compose up -d"
  dim "     然后打开 http://localhost:8081"
  return 0
}

# ---------------------------------------------------------------- 停止
cmd_stop() {
  head2 "停止"
  pkill -f "uvicorn app.main:app" 2>/dev/null && ok "后端已停" || ok "后端未运行"
  pkill -f "vite" 2>/dev/null && ok "前端已停" || true
  dim "PostgreSQL 容器保留（数据在里面，用 docker stop ia-pg 才停）"
}

# ---------------------------------------------------------------- 状态
cmd_status() {
  head2 "状态"
  echo -n "  PostgreSQL  : "
  docker exec ia-pg pg_isready -U ia -d interview_agent >/dev/null 2>&1 && echo "ok" || echo "down"
  echo -n "  后端  8080  : "
  curl -s -o /dev/null -w "%{http_code}\n" --max-time 3 http://127.0.0.1:8080/api/health 2>/dev/null || echo "down"
  echo -n "  前端  5173  : "
  if http_alive 5173; then echo "ok"; else echo "down"; fi

  if docker exec ia-pg pg_isready -U ia -d interview_agent >/dev/null 2>&1; then
    echo ""
    echo "  数据："
    docker exec ia-pg psql -U ia -d interview_agent -t -A -c "
      SELECT '    题库      ' || COUNT(*) || ' 题（已向量化 ' ||
             COUNT(embedding) || '）' FROM bank_questions
      UNION ALL SELECT '    会话      ' || COUNT(*) FROM sessions
      UNION ALL SELECT '    报告      ' || COUNT(*) FROM reports
    " 2>/dev/null | sed 's/^/  /'
  fi
}

# ---------------------------------------------------------------- 全部启动
cmd_all() {
  echo ""
  echo "${C_HL}interview-agent 启动${C_END}"
  cmd_check
  cmd_api || exit 1
  cmd_web

  # 结尾横幅：明确告诉用户"打开哪个地址"
  #
  # 这一块之前太弱：只列了地址，没说"点哪个"。
  # 新人看到两个 URL 会犹豫。所以把主要入口单独高亮。
  local web_up=0
  if http_alive 5173; then
    web_up=1
  fi

  echo ""
  echo "${C_OK}════════════════════════════════════════════${C_END}"
  if [ "$web_up" -eq 1 ]; then
    echo "  ${C_OK}→ 打开这个地址开始用：${C_END}"
    echo ""
    echo "     ${C_HL}$(reachable_url 5173)${C_END}"
  else
    echo "  ${C_WARN}前端还没起来。${C_END}先在这个地址启动它："
    echo ""
    echo "     cd frontend && npm run dev"
    echo ""
    echo "     起来后打开 ${C_HL}http://localhost:5173${C_END}"
  fi
  echo ""
  echo "  ${C_DIM}后端 API 文档  http://localhost:8080/docs${C_END}"
  echo "  ${C_DIM}健康检查       http://localhost:8080/api/health${C_END}"
  echo "  ${C_DIM}Docker 模式    前端 :8081 / 后端 :8080${C_END}"
  echo ""
  echo "  停止    bash start.sh stop"
  echo "  状态    bash start.sh status"
  echo "  体检    bash start.sh check"
  echo "${C_OK}════════════════════════════════════════════${C_END}"
}

# ---------------------------------------------------------------- 入口
case "${1:-all}" in
  all)    cmd_all ;;
  api)    cmd_api ;;
  web)    cmd_web ;;
  stop)   cmd_stop ;;
  status) cmd_status ;;
  check)  cmd_check ;;
  *)
    echo "用法: bash start.sh [all|api|web|stop|status|check]"
    exit 1
    ;;
esac

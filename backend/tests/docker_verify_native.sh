#!/usr/bin/env bash
# Docker 构建验证（在 WSL 原生文件系统里做，避开 /mnt/e 的 9p 慢速）
#
# ## 为什么不在 /mnt/e 里构建
#
# 实测：`docker compose build` 在 /mnt/e 下，仅"transferring build context 41KB"
# 就花了 **237 秒**。原因：/mnt/e 是 Windows 挂载，走 9p 协议，
# Docker 需要扫描整个上下文目录（含 .venv、node_modules 等），
# 每次 stat 都要跨 VM 边界。
#
# 解法：把必要文件复制到 ~/ia-build（WSL 原生 ext4），在那里构建。
# 这也更接近真实部署场景（代码在 Linux 上）。
set -u

SRC=/mnt/e/桌面/实习项目/interview_agent
DST=$HOME/ia-build

echo "=========== 1) 准备构建目录（WSL 原生）==========="
rm -rf "$DST"
mkdir -p "$DST"

# 只复制构建需要的文件（不复制 .venv / node_modules / data / logs）
echo "  复制 backend/"
cp -r "$SRC/backend" "$DST/backend" 2>/dev/null || true
rm -rf "$DST/backend/__pycache__" "$DST/backend/tests/__pycache__"

echo "  复制前端源码"
mkdir -p "$DST/frontend"
for f in package.json package-lock.json vite.config.ts tsconfig.json \
         tsconfig.app.json tsconfig.node.json index.html nginx.conf Dockerfile; do
  [ -f "$SRC/frontend/$f" ] && cp "$SRC/frontend/$f" "$DST/frontend/"
done
cp -r "$SRC/frontend/src" "$DST/frontend/src"

echo "  复制旧核心（规则层，镜像里要用）"
# 注意：必须 `cd $SRC` 再 glob，否则 `*.py` 匹配的是当前目录（$HOME）
# 踩过的坑：第一版写成 `for f in *.py; do cp "$SRC/$f" ...`
# 而当时 cwd 是 $HOME，glob 展开为空 → 一个 .py 都没复制 →
# 容器启动自检报"找不到旧核心模块：/app"
CORE_COUNT=0
for f in "$SRC"/*.py; do
  if [ -f "$f" ]; then
    cp "$f" "$DST/"
    CORE_COUNT=$((CORE_COUNT + 1))
  fi
done
echo "    已复制 $CORE_COUNT 个 .py"
if [ "$CORE_COUNT" -lt 10 ]; then
  echo "    ✗ 数量异常（应约 41 个），检查 $SRC/*.py 是否存在"
fi

echo "  复制题库"
mkdir -p "$DST/data"
cp "$SRC/data/question_bank.jsonl" "$DST/data/" 2>/dev/null || echo "  警告：题库缺失"

echo "  复制 compose 文件"
cp "$SRC/docker-compose.yml" "$DST/"
cp "$SRC/.env.example" "$DST/" 2>/dev/null || true

# 关键：.env 必须一起复制
#
# 踩过的坑：这个脚本开头 `rm -rf $DST` 会删掉构建目录，
# 而 `.env`（含宿主机 Ollama/网关的真实地址）如果不同步过去，
# compose 就会用 `host.docker.internal` 默认值——在 WSL2 下**不通**。
# 表现：容器起来了，但 embedding 降级、模型调用全失败。
if [ -f "$SRC/.env" ]; then
  cp "$SRC/.env" "$DST/.env"
  echo "    已同步 .env"
else
  echo "    ⚠ $SRC/.env 不存在（先跑 tools/up_full_stack.sh 生成）"
fi

# .dockerignore：排除测试与缓存
cat > "$DST/.dockerignore" <<'EOF'
**/__pycache__
**/*.pyc
.venv
node_modules
frontend/node_modules
frontend/dist
data/uploads
logs
runs
reports
.git
EOF

echo ""
echo "  目录大小："
du -sh "$DST" 2>/dev/null
echo "  文件数：$(find "$DST" -type f | wc -l)"

echo ""
echo "=========== 2) 构建后端镜像 ==========="
cd "$DST" || exit 1
START=$(date +%s)
timeout 1200 docker compose build backend 2>&1 | tail -20
RC=${PIPESTATUS[0]}
END=$(date +%s)
echo ""
echo "  构建耗时: $((END-START)) 秒  退出码: $RC"

echo ""
echo "=========== 3) 构建前端镜像 ==========="
START=$(date +%s)
timeout 900 docker compose build frontend 2>&1 | tail -15
RC2=${PIPESTATUS[0]}
END=$(date +%s)
echo ""
echo "  构建耗时: $((END-START)) 秒  退出码: $RC2"

echo ""
echo "=========== 4) 镜像清单 ==========="
docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | grep -iE "ia-build|interview" | head -5

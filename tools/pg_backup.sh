#!/usr/bin/env bash
# 备份 / 恢复 PostgreSQL 数据
#
# ## 为什么需要这个脚本（真实教训）
#
# 调试过程中我执行过：
#     docker rm -f ia-pg
#     docker compose down -v        # -v 会删数据卷
#
# 结果用户表被清空，用户之前注册的账号全没了，
# 表现出来的现象是"账号密码都对，但登不进去"。
#
# 这个脚本提供两条出路：
#   1. 删容器前先备份（backup）
#   2. 数据没了能恢复（restore）
#
# ## 用法
#
#   bash tools/pg_backup.sh backup              # 备份到 backups/
#   bash tools/pg_backup.sh restore FILE        # 从文件恢复
#   bash tools/pg_backup.sh list                # 看有哪些备份
set -u
PROJ=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJ" || exit 1

BACKUP_DIR="$PROJ/backups"
PG_CONTAINER=ia-pg
DB=interview_agent
PGUSER=ia

case "${1:-}" in
  backup)
    mkdir -p "$BACKUP_DIR"
    TS=$(date +%Y%m%d_%H%M%S)
    OUT="$BACKUP_DIR/${DB}_${TS}.sql"

    if ! docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
      echo "✗ PostgreSQL 容器没运行，无从备份"
      exit 1
    fi

    echo "备份到 $OUT …"
    if docker exec "$PG_CONTAINER" pg_dump -U "$PGUSER" -d "$DB" --clean --if-exists \
         > "$OUT" 2>/dev/null; then
      SIZE=$(du -h "$OUT" | cut -f1)
      USERS=$(grep -c "INSERT INTO public.users" "$OUT" 2>/dev/null || echo 0)
      echo "  ✓ 完成（$SIZE）"
      echo "  文件: $OUT"
      echo "  提示：里面含用户数据，不要提交到 git（backups/ 已在 .gitignore）"
    else
      echo "  ✗ 备份失败"
      rm -f "$OUT"
      exit 1
    fi
    ;;

  restore)
    FILE=${2:-}
    if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
      echo "用法: bash tools/pg_backup.sh restore <备份文件>"
      echo ""
      echo "可用备份："
      ls -1t "$BACKUP_DIR"/*.sql 2>/dev/null | head -5 | sed 's/^/  /' || echo "  （无）"
      exit 1
    fi

    if ! docker ps --format '{{.Names}}' | grep -q "^${PG_CONTAINER}$"; then
      echo "✗ PostgreSQL 容器没运行"
      exit 1
    fi

    echo "从 $FILE 恢复（会先清空现有数据）…"
    if docker exec -i "$PG_CONTAINER" psql -U "$PGUSER" -d "$DB" < "$FILE" > /dev/null 2>&1; then
      echo "  ✓ 恢复完成"
    else
      echo "  ✗ 恢复失败"
      exit 1
    fi
    ;;

  list)
    echo "备份列表（$BACKUP_DIR）："
    if [ -d "$BACKUP_DIR" ]; then
      ls -lh "$BACKUP_DIR"/*.sql 2>/dev/null | awk '{print "  " $9 "  " $5}' || echo "  （无）"
    else
      echo "  （还没有备份）"
    fi
    ;;

  *)
    echo "用法:"
    echo "  bash tools/pg_backup.sh backup          # 备份"
    echo "  bash tools/pg_backup.sh restore FILE    # 恢复"
    echo "  bash tools/pg_backup.sh list            # 列出备份"
    ;;
esac

#!/usr/bin/env bash
# P22 恢复：把一个备份目录还原到**隔离目标**，默认绝不碰正在用的库和文件目录。
#
# 隔离目标的含义（这也是唯一推荐的做法）：
#   - 数据库：另建一个库（默认 customs_agent_restore_<时间戳>），不动业务库；
#   - 文件：解到一个新目录（默认 <文件根>/../restore-<时间戳>），不动原件；
#   - 然后用隔离库 + 隔离目录启动一次校验，确认数据能读出来。
# 想原地覆盖业务库属于事故恢复，不走本脚本；见 deploy/README.md 第 5.4 节的逐步命令。
#
# 用法：
#   scripts/operations/restore.sh deploy/backups/<时间戳>-<标签>
#   scripts/operations/restore.sh <目录> --target-db my_restore --target-files /tmp/restore
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

BACKUP_DIR="${1:?用法：restore.sh <备份目录> [--target-db 名称] [--target-files 目录]}"
shift || true
STAMP="$(date -u '+%Y%m%d%H%M%S')"
TARGET_DB="${CUSTOMS_PG_NAME:-customs_agent}_restore_${STAMP}"
TARGET_FILES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target-db) TARGET_DB="${2:?}"; shift 2 ;;
    --target-files) TARGET_FILES="${2:?}"; shift 2 ;;
    *) die "未知参数：$1" ;;
  esac
done

[ -d "$BACKUP_DIR" ] || die "备份目录不存在：$BACKUP_DIR"
[ -f "$BACKUP_DIR/db.dump" ] || die "缺少 db.dump：$BACKUP_DIR"
[ -f "$BACKUP_DIR/files.tar.gz" ] || die "缺少 files.tar.gz：$BACKUP_DIR"

# ---- 0. 先校验备份完整性 ----------------------------------------------------
log "校验备份清单 manifest.sha256"
( cd "$BACKUP_DIR" && { sha256sum -c manifest.sha256 2>/dev/null || shasum -a 256 -c manifest.sha256; } ) \
  || die "备份文件与清单不一致，停止恢复（先确认备份介质是否损坏）"

PG_USER_SRC="$(env_get CUSTOMS_PG_USER customs_agent)"
if [ "$BACKUP_MODE" = "compose" ]; then
  require_compose
  TARGET_FILES="${TARGET_FILES:-$APP_FILES_DIR/../restore-$STAMP}"
else
  FILE_ROOT="${CUSTOMS_FILE_ROOT:-$REPO_ROOT/.local/files}"
  TARGET_FILES="${TARGET_FILES:-$FILE_ROOT/../restore-$STAMP}"
fi

log "恢复目标：库=$TARGET_DB 文件目录=$TARGET_FILES 模式=$BACKUP_MODE"

# ---- 1. 建空库（已存在则失败，不覆盖）--------------------------------------
if [ "$BACKUP_MODE" = "compose" ]; then
  log "在 db 容器里建库 $TARGET_DB"
  if dc exec -T db psql -U "$PG_USER_SRC" -d postgres -tAc \
      "SELECT 1 FROM pg_database WHERE datname='$TARGET_DB'" | grep -q 1; then
    die "目标库已存在：${TARGET_DB}（换一个名字，或手工确认后删除；本脚本不覆盖）"
  fi
  dc exec -T db createdb -U "$PG_USER_SRC" -T template0 -E UTF8 "$TARGET_DB"

  log "导入数据库转储"
  dc exec -T db pg_restore -U "$PG_USER_SRC" -d "$TARGET_DB" --no-owner < "$BACKUP_DIR/db.dump"

  log "解包文件到 $TARGET_FILES"
  # 目标目录在 files 卷内，通过 app 容器的一次性运行写入；先确认它为空，避免覆盖。
  dc run --rm --no-deps -T app sh -c "
    set -e
    target='$TARGET_FILES'
    mkdir -p \"\$target\"
    if [ -n \"\$(ls -A \"\$target\")\" ]; then echo \"目标目录非空：\$target\" >&2; exit 3; fi
    tar -xzf - -C \"\$target\"
  " < "$BACKUP_DIR/files.tar.gz"
else
  [ -n "${CUSTOMS_PG_PASSWORD:-}" ] && export PGPASSWORD="$CUSTOMS_PG_PASSWORD"
  log "建库 ${TARGET_DB}（本机 PostgreSQL）"
  if psql $(pg_native_admin_args) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$TARGET_DB'" | grep -q 1; then
    die "目标库已存在：${TARGET_DB}（换一个名字，或手工确认后删除；本脚本不覆盖）"
  fi
  createdb $(pg_native_admin_args) -O "${CUSTOMS_PG_USER:-customs_agent}" -T template0 -E UTF8 "$TARGET_DB"
  log "导入数据库转储"
  pg_restore $(pg_native_args) -d "$TARGET_DB" --no-owner "$BACKUP_DIR/db.dump"
  log "解包文件到 $TARGET_FILES"
  if [ -d "$TARGET_FILES" ] && [ -n "$(ls -A "$TARGET_FILES" 2>/dev/null)" ]; then
    die "目标目录非空：${TARGET_FILES}（本脚本不覆盖）"
  fi
  mkdir -p "$TARGET_FILES"
  tar -xzf "$BACKUP_DIR/files.tar.gz" -C "$TARGET_FILES"
fi

cat > "$BACKUP_DIR/restored-$(echo "$TARGET_DB" | tr -c 'A-Za-z0-9_.-' '_').txt" <<TXT
恢复目标
  数据库   $TARGET_DB
  文件目录 $TARGET_FILES
  时间     $(date -u '+%Y-%m-%dT%H:%M:%SZ')
  模式     $BACKUP_MODE
TXT

log "恢复完成。下一步比对："
log "  scripts/operations/verify_restore.sh $BACKUP_DIR --target-db $TARGET_DB --target-files $TARGET_FILES"
printf '%s\n' "$TARGET_DB" "$TARGET_FILES"

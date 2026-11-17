#!/usr/bin/env bash
# P22 备份：数据库（pg_dump -Fc）+ 受控文件（原件/图片/导出）+ 行数与文件清单。
#
# 一致性口径（必须理解，否则会误以为备份一定一致）：
#   - 默认先「短暂停写」：compose 模式下先 stop app 与 web，再依次 dump 数据库和打包文件。
#     停写期间没有新写入，因此数据库与文件是同一时点的；代价是这段时间服务不可用（通常几秒到几十秒）。
#   - 加 --no-quiesce 则不停服务，数据库与文件是两个相邻时点，可能出现
#     “数据库引用了尚未落盘的文件”或反之。只适合演示库，不要当正式备份用。
#   - 备份只读：不会删除、修改现有数据库或任何文件。
#
# 用法：
#   scripts/operations/backup.sh                # 停写备份
#   scripts/operations/backup.sh --no-quiesce   # 不停写（不推荐）
#   scripts/operations/backup.sh --label before-upgrade
#   BACKUP_MODE=native ... scripts/operations/backup.sh   # 本机 PG 演练
set -euo pipefail

# shellcheck source=./lib.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

LABEL="manual"
QUIESCE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-quiesce) QUIESCE=0; shift ;;
    --label) LABEL="${2:?--label 需要值}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

if [ "$BACKUP_MODE" = "compose" ]; then
  require_compose
  PG_DB="$(env_get CUSTOMS_PG_NAME customs_agent)"
  PG_USER="$(env_get CUSTOMS_PG_USER customs_agent)"
  ENV_SOURCE="$ENV_FILE"
else
  PG_DB="${CUSTOMS_PG_NAME:-customs_agent}"
  PG_USER="${CUSTOMS_PG_USER:-customs_agent}"
  FILE_ROOT="${CUSTOMS_FILE_ROOT:-$REPO_ROOT/.local/files}"
  ENV_SOURCE="进程环境变量"
  [ -d "$FILE_ROOT" ] || warn "文件根目录不存在：${FILE_ROOT}（本次备份的文件部分会是空的）"
fi

BACKUP_DIR="$(new_backup_dir "$LABEL")"
log "备份目录：$BACKUP_DIR"
log "模式=$BACKUP_MODE 数据库=$PG_DB 用户=$PG_USER 停写=$([ "$QUIESCE" = 1 ] && echo 是 || echo 否)"

resume_services() {
  if [ "$BACKUP_MODE" = "compose" ] && [ "$QUIESCE" = 1 ]; then
    log "恢复服务：web / app"
    dc start db >/dev/null 2>&1 || true
    dc up -d app web >/dev/null || warn "恢复 app/web 失败，请手动执行 docker compose -f $COMPOSE_FILE up -d"
  fi
}
trap 'resume_services' EXIT

# ---- 1. 停写 ----------------------------------------------------------------
if [ "$QUIESCE" = 1 ]; then
  if [ "$BACKUP_MODE" = "compose" ]; then
    log "停写：stop web app"
    dc stop web app >/dev/null
  else
    warn "native 模式无法替你停写；请确认没有进程在写库/写文件（演练时本机应无服务在跑）"
  fi
else
  warn "已选择不停写：数据库与文件不是同一时点，见脚本头部说明"
fi

# ---- 2. 数据库 --------------------------------------------------------------
log "导出数据库 → db.dump"
if [ "$BACKUP_MODE" = "compose" ]; then
  dc exec -T db sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > "$BACKUP_DIR/db.dump"
else
  PGPASSWORD="${CUSTOMS_PG_PASSWORD:-}" pg_dump -Fc \
    $(pg_native_args) -d "$PG_DB" -f "$BACKUP_DIR/db.dump"
fi

# ---- 3. 文件 ----------------------------------------------------------------
log "打包受控文件 → files.tar.gz（排除 incoming 暂存区）"
if [ "$BACKUP_MODE" = "compose" ]; then
  dc exec -T app sh -c "tar -czf - -C $APP_FILES_DIR --exclude=./incoming ." > "$BACKUP_DIR/files.tar.gz"
else
  mkdir -p "$FILE_ROOT"
  tar -czf "$BACKUP_DIR/files.tar.gz" -C "$FILE_ROOT" --exclude=./incoming .
fi

# ---- 4. 行数与文件清单 ------------------------------------------------------
log "统计行数与文件哈希 → counts.json"
if [ "$BACKUP_MODE" = "compose" ]; then
  dc run --rm --no-deps -T app \
    python scripts/operations/table_counts.py --files-root "$APP_FILES_DIR" \
    > "$BACKUP_DIR/counts.json"
else
  ( cd "$REPO_ROOT" && CUSTOMS_FILE_ROOT="$FILE_ROOT" \
      DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings}" \
      "${PYTHON:-$REPO_ROOT/.venv-customs/bin/python}" scripts/operations/table_counts.py \
        --files-root "$FILE_ROOT" ) > "$BACKUP_DIR/counts.json"
fi

# ---- 5. 清单与元信息 --------------------------------------------------------
log "生成校验清单 → manifest.sha256"
( cd "$BACKUP_DIR" && sha256sum db.dump files.tar.gz counts.json 2>/dev/null \
    || shasum -a 256 db.dump files.tar.gz counts.json ) > "$BACKUP_DIR/manifest.sha256"

cat > "$BACKUP_DIR/meta.env" <<META
BACKUP_CREATED_AT=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
BACKUP_MODE=$BACKUP_MODE
BACKUP_QUIESCED=$QUIESCE
BACKUP_LABEL=$LABEL
BACKUP_DB_NAME=$PG_DB
BACKUP_DB_USER=$PG_USER
BACKUP_ENV_SOURCE=$ENV_SOURCE
BACKUP_GIT_COMMIT=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)
BACKUP_HOSTNAME=$(hostname 2>/dev/null || echo unknown)
META

cat > "$BACKUP_DIR/README.txt" <<'TXT'
本目录是一次备份的完整内容：

  db.dump           PostgreSQL 自定义格式转储（pg_restore 用，不要直接 psql 导入）
  files.tar.gz      受控存储根目录（原件/图片/导出），已排除 incoming 暂存区
  counts.json       备份时点的表行数与文件哈希，恢复后用它比对
  manifest.sha256   以上三个文件的 sha256；恢复前先校验
  meta.env          备份时间、模式、库名、代码提交等

恢复步骤见 deploy/README.md 第 5 节，或直接执行：
  scripts/operations/restore.sh <本目录>
演练（备份→恢复到隔离位置→比对）：
  scripts/operations/drill.sh
TXT

log "完成。大小：$(du -sh "$BACKUP_DIR" | cut -f1)"
log "恢复前校验： cd $BACKUP_DIR && sha256sum -c manifest.sha256（macOS 用 shasum -a 256 -c）"
echo "$BACKUP_DIR"

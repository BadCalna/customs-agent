#!/usr/bin/env bash
# P22 恢复校验：把“恢复出来的库 + 文件目录”与备份时的 counts.json 逐项比对。
#
# 用法：
#   scripts/operations/verify_restore.sh <备份目录> --target-db <恢复库名> --target-files <恢复目录>
#
# 退出码：0 一致；1 有差异；2 参数/环境错误。
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

BACKUP_DIR="${1:?用法：verify_restore.sh <备份目录> --target-db <库名> --target-files <目录>}"
shift || true
TARGET_DB=""
TARGET_FILES=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target-db) TARGET_DB="${2:?}"; shift 2 ;;
    --target-files) TARGET_FILES="${2:?}"; shift 2 ;;
    *) die "未知参数：$1" ;;
  esac
done
[ -n "$TARGET_DB" ] || die "必须给 --target-db"
[ -n "$TARGET_FILES" ] || die "必须给 --target-files"
[ -f "$BACKUP_DIR/counts.json" ] || die "备份目录里没有 counts.json：$BACKUP_DIR"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

log "统计恢复后的行数与文件哈希（库=$TARGET_DB 目录=${TARGET_FILES}）"
if [ "$BACKUP_MODE" = "compose" ]; then
  require_compose
  dc run --rm --no-deps -T \
    -e CUSTOMS_PG_NAME="$TARGET_DB" \
    -e CUSTOMS_FILE_ROOT="$TARGET_FILES" \
    app python scripts/operations/table_counts.py --files-root "$TARGET_FILES" \
    > "$WORK_DIR/restored.json"
else
  ( cd "$REPO_ROOT" \
    && CUSTOMS_PG_NAME="$TARGET_DB" CUSTOMS_FILE_ROOT="$TARGET_FILES" \
       DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings}" \
       "${PYTHON:-$REPO_ROOT/.venv-customs/bin/python}" scripts/operations/table_counts.py \
         --files-root "$TARGET_FILES" ) > "$WORK_DIR/restored.json"
fi

log "比对"
# 备份目录里的 counts.json 在宿主机上，直接在本机比较即可（比较脚本不依赖 Django）。
PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "$REPO_ROOT/.venv-customs/bin/python" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv-customs/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi
"$PYTHON_BIN" "$REPO_ROOT/scripts/operations/compare_counts.py" \
  "$BACKUP_DIR/counts.json" "$WORK_DIR/restored.json"

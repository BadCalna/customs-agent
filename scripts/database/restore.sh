#!/usr/bin/env bash
# Legacy DB-only dump restore. For DB+files use scripts/operations/restore.sh.
# Never drops an existing target, including when old FORCE variables are set.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/pg_env.sh"
dump="${1:-}"
target="${2:-${PGDATABASE_NAME}_restore}"
if [ -z "$dump" ] || [ ! -f "$dump" ]; then
  echo "用法：$0 <pg_dump 文件> [全新目标库名]" >&2; exit 2
fi
if [[ ! "$target" =~ ^[a-zA-Z_][a-zA-Z0-9_]{0,62}$ ]] || \
   [ "$target" = "$PGDATABASE_NAME" ] || [[ "$target" =~ ^(postgres|template0|template1)$ ]]; then
  echo "拒绝恢复到业务库/系统库或无效库名：$target" >&2; exit 2
fi
exists="$("$PG_BIN/psql" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" -d postgres \
  -v ON_ERROR_STOP=1 -tAc "SELECT 1 FROM pg_database WHERE datname = '$target'")"
if [ -n "$exists" ]; then
  echo "目标库 $target 已存在；必须换一个全新库名。" >&2; exit 2
fi
"$PG_BIN/createdb" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" -O "$PGROLE" -E UTF8 -T template0 "$target"
"$PG_BIN/pg_restore" -h "$PGHOST" -p "$PGPORT" -U "$PGROLE" -d "$target" \
  --no-owner --no-privileges --exit-on-error "$dump"
echo "已恢复数据库到 $target；此旧格式不含附件，不能代替 P22 完整恢复验收。"

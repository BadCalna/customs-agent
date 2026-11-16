#!/usr/bin/env bash
# P12：把 pg_dump 产物恢复到一个**独立**数据库，用于演练与核对，不覆盖业务库。
#
#   scripts/database/restore.sh <dump> [目标库名]
#
# 目标库已存在时默认拒绝；确需覆盖时显式设置 CUSTOMS_PG_RESTORE_FORCE=yes。
# 回退到 SQLite 不需要本脚本：改回 CUSTOMS_DB_BACKEND=sqlite 即可（旧库未被删除）。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./pg_env.sh
. "$SCRIPT_DIR/pg_env.sh"

dump="${1:-}"
target="${2:-${PGDATABASE_NAME}_restore}"
if [ -z "$dump" ] || [ ! -f "$dump" ]; then
  echo "用法：$0 <pg_dump 文件> [目标库名]" >&2
  exit 2
fi

"$SCRIPT_DIR/pg_ctl.sh" start >/dev/null

exists="$("$PG_BIN/psql" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" -d postgres -tAc \
  "select 1 from pg_database where datname = '$target'")"
if [ -n "$exists" ]; then
  if [ "${CUSTOMS_PG_RESTORE_FORCE:-no}" != "yes" ]; then
    echo "目标库 $target 已存在；换一个库名，或设置 CUSTOMS_PG_RESTORE_FORCE=yes 覆盖。" >&2
    exit 2
  fi
  "$PG_BIN/dropdb" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" --if-exists "$target"
fi

"$PG_BIN/createdb" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" -O "$PGROLE" -E UTF8 -T template0 "$target"
# 以业务角色恢复：目标库由该角色所有，恢复出来的表才归它，否则只有超级用户能读。
"$PG_BIN/pg_restore" -h "$PGHOST" -p "$PGPORT" -U "$PGROLE" -d "$target" \
  --no-owner --no-privileges --exit-on-error "$dump"

echo "已恢复到 ${target}。核对："
echo "  CUSTOMS_PG_NAME=$target make check-postgres"
echo "  CUSTOMS_PG_NAME=$target $REPO_ROOT/.venv-customs/bin/python $SCRIPT_DIR/verify_copy.py --skip-source"
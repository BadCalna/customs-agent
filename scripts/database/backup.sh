#!/usr/bin/env bash
# P12：备份 PostgreSQL（自定义格式 dump + 全局对象）与 SQLite 原库，只读不删除。
#
#   scripts/database/backup.sh [标签]
#
# 产物在 .local/backups/<时间戳>-<标签>/ 下，附 sha256 清单；备份本身不进 Git。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./pg_env.sh
. "$SCRIPT_DIR/pg_env.sh"

label="${1:-manual}"
stamp="$(date +%Y%m%dT%H%M%S)"
out="$PG_RUN_DIR/backups/${stamp}-${label}"
mkdir -p "$out"

sqlite_src="${CUSTOMS_SQLITE_PATH:-$PG_RUN_DIR/db.sqlite3}"
case "$sqlite_src" in
  /*) ;;
  *) sqlite_src="$REPO_ROOT/$sqlite_src" ;;
esac

if "$PG_BIN/pg_isready" -h "$PGHOST" -p "$PGPORT" -q; then
  "$PG_BIN/pg_dump" -h "$PGHOST" -p "$PGPORT" -U "$PGROLE" -d "$PGDATABASE_NAME" \
    -Fc -f "$out/pg-$PGDATABASE_NAME.dump"
  "$PG_BIN/pg_dumpall" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" --globals-only \
    -f "$out/pg-globals.sql"
  echo "已备份 PostgreSQL：$out/pg-$PGDATABASE_NAME.dump"
else
  echo "PostgreSQL 未运行，跳过数据库 dump（SQLite 仍会备份）" >&2
fi

if [ -f "$sqlite_src" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    # .backup 是 SQLite 在线备份，导出期间不加写锁也不破坏原库。
    sqlite3 "$sqlite_src" ".backup '$out/sqlite-db.sqlite3'"
  else
    cp "$sqlite_src" "$out/sqlite-db.sqlite3"
  fi
  echo "已备份 SQLite：$out/sqlite-db.sqlite3（来源 ${sqlite_src}，原库保持不动）"
else
  echo "未找到 SQLite 源库：${sqlite_src}（跳过）" >&2
fi

( cd "$out" && shasum -a 256 ./* > MANIFEST.sha256 )
echo "清单：$out/MANIFEST.sha256"
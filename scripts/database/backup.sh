#!/usr/bin/env bash
# P12 entry: retain a consistent SQLite rollback snapshot, then P22 DB+files backup.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/pg_env.sh"
cd "$REPO_ROOT"
umask 077
python_bin="${PYTHON:-$REPO_ROOT/.venv-customs/bin/python}"
sqlite_src="${CUSTOMS_SQLITE_PATH:-$PG_RUN_DIR/db.sqlite3}"
case "$sqlite_src" in /*) ;; *) sqlite_src="$REPO_ROOT/$sqlite_src" ;; esac
if [ -f "$sqlite_src" ]; then
  mkdir -p "$PG_RUN_DIR/p12"
  snapshot_dir="$(mktemp -d "$PG_RUN_DIR/p12/rollback-XXXXXX")"
  "$python_bin" "$SCRIPT_DIR/snapshot_sqlite.py" "$sqlite_src" "$snapshot_dir/source.sqlite3"
  echo "SQLite 回退快照：$snapshot_dir/source.sqlite3" >&2
fi
# Fails if PostgreSQL/files are unavailable; never returns a partial backup as success.
exec env BACKUP_MODE=native PYTHON="$python_bin" \
  "$REPO_ROOT/scripts/operations/backup.sh" --label "${1:-manual}"

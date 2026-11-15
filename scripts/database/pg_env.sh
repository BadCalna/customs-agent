#!/usr/bin/env bash
# P12：本机 PostgreSQL 集群约定，所有 database 脚本 source 本文件。
# 路径与端口只有这一处定义；说明见同目录 README.md。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PG_BIN="${CUSTOMS_PG_BIN:-}"
if [ -z "$PG_BIN" ]; then
  if [ -d /opt/homebrew/opt/postgresql@17/bin ]; then
    PG_BIN=/opt/homebrew/opt/postgresql@17/bin
  elif command -v pg_config >/dev/null 2>&1; then
    PG_BIN="$(pg_config --bindir)"
  else
    echo "找不到 PostgreSQL 可执行文件：先 brew install postgresql@17，或设置 CUSTOMS_PG_BIN。" >&2
    exit 2
  fi
fi

PG_RUN_DIR="$REPO_ROOT/.local"
PGDATA="${CUSTOMS_PGDATA:-$PG_RUN_DIR/pgdata}"
PGPORT="${CUSTOMS_PG_PORT:-5432}"
PGSOCKET="${CUSTOMS_PG_SOCKET:-$PG_RUN_DIR/pgsocket}"
PGLOG="${CUSTOMS_PG_LOG:-$PG_RUN_DIR/pg.log}"
PGHOST="${CUSTOMS_PG_HOST:-127.0.0.1}"
PGDATABASE_NAME="${CUSTOMS_PG_NAME:-customs_agent}"
PGROLE="${CUSTOMS_PG_USER:-customs_agent}"
PGSUPERUSER="${CUSTOMS_PG_SUPERUSER:-postgres}"

export PGDATA PGPORT PGSOCKET
#!/usr/bin/env bash
# P12：本机 PostgreSQL 集群约定，所有 database 脚本 source 本文件。
# 路径与端口只有这一处定义；说明见同目录 README.md。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Match Django's dotenv precedence without executing .env as shell code.
# Process environment wins, interpolation is disabled, values are shell-quoted.
if [ -f "$REPO_ROOT/.env" ]; then
  env_python="${PYTHON:-$REPO_ROOT/.venv-customs/bin/python}"
  if [ ! -x "$env_python" ]; then echo "先 make setup，缺少 $env_python" >&2; exit 2; fi
  dotenv_exports="$("$env_python" - "$REPO_ROOT/.env" <<'PY'
import os, re, shlex, sys
from dotenv import dotenv_values
for key, value in dotenv_values(sys.argv[1], interpolate=False).items():
    if re.fullmatch(r'CUSTOMS_PG[A-Z_]*|CUSTOMS_SQLITE_PATH|CUSTOMS_FILE_ROOT', key) and key not in os.environ and value is not None:
        print(f'export {key}={shlex.quote(value)}')
PY
)"
  eval "$dotenv_exports"
fi

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
# Unix socket names have a small platform limit; repo paths can contain long Unicode names.
repo_key="$(printf '%s' "$REPO_ROOT" | shasum -a 256 | cut -c 1-12)"
PGSOCKET="${CUSTOMS_PG_SOCKET:-/tmp/customs-pg-$(id -u)-$repo_key}"
PGLOG="${CUSTOMS_PG_LOG:-$PG_RUN_DIR/pg.log}"
PGHOST="${CUSTOMS_PG_HOST:-127.0.0.1}"
PGDATABASE_NAME="${CUSTOMS_PG_NAME:-customs_agent}"
PGROLE="${CUSTOMS_PG_USER:-customs_agent}"
PGSUPERUSER="${CUSTOMS_PG_SUPERUSER:-postgres}"

export PGDATA PGPORT PGSOCKET
export CUSTOMS_PG_NAME="$PGDATABASE_NAME" CUSTOMS_PG_USER="$PGROLE"
export CUSTOMS_PG_HOST="$PGHOST" CUSTOMS_PG_PORT="$PGPORT"
export CUSTOMS_PG_ADMIN_USER="${CUSTOMS_PG_ADMIN_USER:-$PGSUPERUSER}"
# P22 native requires an absolute root; match Django's expanduser/repo-relative rules.
file_root_python="${PYTHON:-$REPO_ROOT/.venv-customs/bin/python}"
CUSTOMS_FILE_ROOT="$("$file_root_python" - "$REPO_ROOT" "${CUSTOMS_FILE_ROOT:-}" <<'PY'
from pathlib import Path
import sys
root, raw = Path(sys.argv[1]), sys.argv[2]
path = Path(raw).expanduser() if raw.strip() else root / '.local/files'
print((path if path.is_absolute() else root / path).resolve())
PY
)"
export CUSTOMS_FILE_ROOT
export PATH="$PG_BIN:$PATH"
export PGPASSWORD="${CUSTOMS_PG_PASSWORD:-}"

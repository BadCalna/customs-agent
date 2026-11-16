#!/usr/bin/env bash
# P12：一条命令把本机 PostgreSQL 集群准备好（初始化 → 启动 → 建角色/库 → migrate）。
#
#   scripts/database/bootstrap.sh                 # 初始化并 migrate
#   scripts/database/bootstrap.sh --with-demo     # 再跑 seed_demo + seed_contracts
#   scripts/database/bootstrap.sh --no-migrate    # 只准备到库创建
#
# 幂等：集群已存在、角色/库已存在时跳过，不删除任何已有数据。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./pg_env.sh
. "$SCRIPT_DIR/pg_env.sh"

do_migrate=1
with_demo=0
for arg in "$@"; do
  case "$arg" in
    --no-migrate) do_migrate=0 ;;
    --with-demo) with_demo=1 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

"$SCRIPT_DIR/pg_ctl.sh" init
"$SCRIPT_DIR/pg_ctl.sh" start

PSQL=("$PG_BIN/psql" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" -d postgres -v ON_ERROR_STOP=1 -q)
"${PSQL[@]}" -v role="$PGROLE" -v db="$PGDATABASE_NAME" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L TEMPLATE template0', :'db', :'role', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db') \gexec
-- 开发角色需要建测试库（make check-postgres / manage.py test）；生产角色不应带此权限。
SELECT format('ALTER ROLE %I CREATEDB', :'role') \gexec
SQL

"$PG_BIN/psql" -h "$PGHOST" -p "$PGPORT" -U "$PGROLE" -d "$PGDATABASE_NAME" -tAc \
  "select current_user || ' @ ' || current_database() || ' / ' || version()" | sed 's/^/连接自检：/'

export CUSTOMS_DB_BACKEND=postgres
export CUSTOMS_PG_NAME="$PGDATABASE_NAME"
export CUSTOMS_PG_USER="$PGROLE"
export CUSTOMS_PG_HOST="$PGHOST"
export CUSTOMS_PG_PORT="$PGPORT"
export CUSTOMS_PG_PASSWORD="${CUSTOMS_PG_PASSWORD:-}"

cd "$REPO_ROOT"
if [ "$do_migrate" = 1 ]; then
  make migrate
fi
if [ "$with_demo" = 1 ]; then
  make seed-demo
  make seed-contracts
fi

cat <<EOF

PostgreSQL 已就绪。切换后端只需在 .env 中写入（或直接导出）：

  CUSTOMS_DB_BACKEND=postgres
  CUSTOMS_PG_NAME=$PGDATABASE_NAME
  CUSTOMS_PG_USER=$PGROLE
  CUSTOMS_PG_HOST=$PGHOST
  CUSTOMS_PG_PORT=$PGPORT

常用命令：
  make check-postgres      # 确认后端为 PostgreSQL 并跑 P12 检查
  scripts/database/pg_ctl.sh stop
  scripts/database/backup.sh
EOF
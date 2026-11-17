#!/usr/bin/env bash
# P12：本机 PostgreSQL 集群的初始化与启停。用法：pg_ctl.sh init|start|stop|restart|status|log
set -euo pipefail
# shellcheck source=./pg_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/pg_env.sh"

require_bin() {
  if [ ! -x "$PG_BIN/$1" ]; then
    echo "找不到 $PG_BIN/$1。先安装：brew install postgresql@17，或设置 CUSTOMS_PG_BIN。" >&2
    exit 2
  fi
}

is_running() {
  "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1
}

wait_ready() {
  for _ in $(seq 1 60); do
    if "$PG_BIN/pg_isready" -h "$PGHOST" -p "$PGPORT" -q; then
      return 0
    fi
    sleep 0.5
  done
  echo "PostgreSQL 未在 $PGHOST:$PGPORT 就绪，见日志 $PGLOG" >&2
  return 1
}

cmd="${1:-status}"
# This helper provisions trust authentication. Validate before init/start/restart
# can create directories, stop an existing cluster, or invoke any PG command.
case "$cmd" in
  init|start|restart)
    "${PYTHON:-$REPO_ROOT/.venv-customs/bin/python}" - "$PGHOST" <<'PY'
import ipaddress, sys
try:
    host = ipaddress.ip_address(sys.argv[1])
    if not host.is_loopback or '%' in sys.argv[1]:
        raise ValueError
except ValueError:
    sys.exit('本机 trust 集群仅允许单个回环 IP（127.0.0.1 / ::1）；非回环生产连接请使用独立配置并启用密码认证。')
PY
    ;;
esac
case "$cmd" in
  init)
    require_bin initdb
    mkdir -p "$PG_RUN_DIR"
    if [ -f "$PGDATA/PG_VERSION" ]; then
      echo "集群已存在：${PGDATA}（版本 $(cat "$PGDATA/PG_VERSION")）"
      exit 0
    fi
    mkdir -p "$PGDATA" "$PGSOCKET"
    chmod 700 "$PGDATA"
    # macOS sandbox may deny SysV shared memory; mmap/posix uses the same PG locks/transactions.
    init_options=()
    if [ "$(uname -s)" = Darwin ]; then
      init_options=(-c shared_memory_type=mmap -c dynamic_shared_memory_type=posix)
    fi
    "$PG_BIN/initdb" -D "$PGDATA" -U "$PGSUPERUSER" \
      --encoding=UTF8 --locale=en_US.UTF-8 \
      --auth-local=trust --auth-host=trust "${init_options[@]}" >/dev/null
    echo "已初始化集群：${PGDATA}（superuser=${PGSUPERUSER}）"
    ;;
  start)
    require_bin pg_ctl
    if is_running; then
      echo "已在运行：${PGDATA}（$PGHOST:${PGPORT}）"
      exit 0
    fi
    if [ ! -f "$PGDATA/PG_VERSION" ]; then
      echo "集群不存在，先执行：scripts/database/pg_ctl.sh init" >&2
      exit 2
    fi
    mkdir -p "$PGSOCKET"
    printf -v server_options '%q ' -p "$PGPORT" -k "$PGSOCKET" -h "$PGHOST"
    "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$PGLOG" -w -t 60 \
      -o "$server_options" start
    wait_ready
    echo "已启动：$PGHOST:${PGPORT}，日志 $PGLOG"
    ;;
  stop)
    require_bin pg_ctl
    if ! is_running; then
      echo "未在运行：$PGDATA"
      exit 0
    fi
    "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast -w -t 60 stop
    echo "已停止：$PGDATA"
    ;;
  restart)
    "$0" stop
    "$0" start
    ;;
  status)
    require_bin pg_ctl
    if is_running; then
      "$PG_BIN/pg_ctl" -D "$PGDATA" status
      "$PG_BIN/psql" -h "$PGHOST" -p "$PGPORT" -U "$PGSUPERUSER" -d postgres -tAc "select version()" 2>/dev/null || true
    else
      echo "未运行：$PGDATA"
      exit 3
    fi
    ;;
  log)
    tail -n "${2:-40}" "$PGLOG"
    ;;
  *)
    echo "用法：$0 init|start|stop|restart|status|log [行数]" >&2
    exit 2
    ;;
esac
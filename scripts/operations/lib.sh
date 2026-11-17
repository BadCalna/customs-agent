#!/usr/bin/env bash
# P22 运维脚本公共部分：定位仓库、选择执行模式、统一日志。
#
# 两种模式：
#   compose（默认）—— 通过 docker compose 在容器里执行。云端测试环境用这个。
#   native         —— 直接用本机 PostgreSQL 与本机 venv 执行。用于本机演练脚本逻辑，
#                     以及在没有 Docker 的机器上验证“dump→还原→比对”这条链路。
#
# 环境变量：
#   BACKUP_MODE          compose | native，默认 compose
#   BACKUP_ROOT          备份根目录。compose 默认 deploy/backups；native 默认 .local/backups
#   CUSTOMS_COMPOSE_FILE compose 文件路径，默认 deploy/docker-compose.yml
#   CUSTOMS_ENV_FILE     compose 用的 .env，默认 deploy/.env
#   PYTHON               native 模式下的解释器，默认 .venv-customs/bin/python，没有则 python3
set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$OPS_DIR/../.." && pwd)"

BACKUP_MODE="${BACKUP_MODE:-compose}"
COMPOSE_FILE="${CUSTOMS_COMPOSE_FILE:-$REPO_ROOT/deploy/docker-compose.yml}"
ENV_FILE="${CUSTOMS_ENV_FILE:-$REPO_ROOT/deploy/.env}"
if [ "$BACKUP_MODE" = "compose" ]; then
  BACKUP_ROOT="${BACKUP_ROOT:-$REPO_ROOT/deploy/backups}"
else
  BACKUP_ROOT="${BACKUP_ROOT:-$REPO_ROOT/.local/backups}"
fi

# 容器内路径（与 deploy/docker-compose.yml 保持一致）
APP_FILES_DIR="${APP_FILES_DIR:-/srv/customs/files}"

# 导出给子脚本（drill.sh 会调用 backup/restore/verify，必须继承同一模式与路径）。
export BACKUP_MODE BACKUP_ROOT

log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] 警告：%s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die()  { printf '[%s] 失败：%s\n' "$(date '+%H:%M:%S')" "$*" >&2; exit 1; }

# 读 deploy/.env 里的值（只取需要的少数几个），避免在脚本里硬编码库名。
env_get() {
  local key="$1" default="${2:-}"
  if [ -f "$ENV_FILE" ]; then
    local value
    value="$(grep -E "^${key}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
    if [ -n "$value" ]; then printf '%s' "$value"; return 0; fi
  fi
  printf '%s' "$default"
}

require_compose() {
  command -v docker >/dev/null 2>&1 || die "找不到 docker 命令；云端部署需要 Docker + Compose v2。"
  docker compose version >/dev/null 2>&1 || die "docker compose 不可用（需要 Compose v2 插件）。"
  [ -f "$COMPOSE_FILE" ] || die "找不到 compose 文件：$COMPOSE_FILE"
}

# compose 包装：固定 -f 与 --env-file，避免在别的目录执行时读错 .env。
dc() {
  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

# 在应用容器里跑 python（一次性容器；不启动 web/nginx）。
app_python() {
  dc run --rm --no-deps -T app python "$@"
}

native_python() {
  local python_bin="${PYTHON:-}"
  if [ -z "$python_bin" ]; then
    if [ -x "$REPO_ROOT/.venv-customs/bin/python" ]; then
      python_bin="$REPO_ROOT/.venv-customs/bin/python"
    else
      python_bin="python3"
    fi
  fi
  ( cd "$REPO_ROOT" && DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings}" "$python_bin" "$@" )
}

# 本机 PostgreSQL 连接参数（native 模式）。
pg_native_args() {
  printf '%s\n' \
    "--host=${CUSTOMS_PG_HOST:-127.0.0.1}" \
    "--port=${CUSTOMS_PG_PORT:-5432}" \
    "--username=${CUSTOMS_PG_USER:-customs_agent}"
}

# 建库/删库等 DDL 用管理角色；业务角色不需要（也不应该有）CREATEDB。
pg_native_admin_args() {
  printf '%s\n' \
    "--host=${CUSTOMS_PG_HOST:-127.0.0.1}" \
    "--port=${CUSTOMS_PG_PORT:-5432}" \
    "--username=${CUSTOMS_PG_ADMIN_USER:-postgres}"
}

new_backup_dir() {
  local label="${1:-manual}"
  local stamp
  stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
  local dir="$BACKUP_ROOT/${stamp}-${label}"
  mkdir -p "$dir"
  printf '%s' "$dir"
}

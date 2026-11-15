#!/usr/bin/env bash
# P12：把现有 SQLite 演示库迁到 PostgreSQL，并做数量 / 外键 / 内容 / 附件引用核对。
#
#   scripts/database/copy_sqlite_to_pg.sh [SQLite 路径] [--fresh]
#
# 原则（D6 第 3 节）：
# - 源 SQLite 只读：脚本先复制一份工作副本，在副本上补迁移，再导出；原文件不删、不改、不 move。
# - 默认拒绝往非空目标库灌数据；--fresh 会先 flush 目标库（仅限本机开发库）。
# - 导出按模型依赖顺序，避免外键前向引用失败；导入后修正整数主键序列。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./pg_env.sh
. "$SCRIPT_DIR/pg_env.sh"

fresh=0
sqlite_src=""
for arg in "$@"; do
  case "$arg" in
    --fresh) fresh=1 ;;
    *) sqlite_src="$arg" ;;
  esac
done
sqlite_src="${sqlite_src:-$REPO_ROOT/.local/db.sqlite3}"
case "$sqlite_src" in
  /*) ;;
  *) sqlite_src="$REPO_ROOT/$sqlite_src" ;;
esac
if [ ! -f "$sqlite_src" ]; then
  echo "找不到 SQLite 源库：${sqlite_src}" >&2
  exit 2
fi

# 依赖顺序：父表在前。reviews.issue 引用 runs.run，因此必须排在 run 之后。
MODELS=(
  auth.Group
  accounts.User
  reviews.Task
  accounts.TaskAssignment
  reviews.FileVersion
  reviews.Item
  reviews.ItemMember
  reviews.ItemImage
  reviews.Confirmation
  reviews.Submission
  reviews.SubmissionItem
  runs.Run
  reviews.Issue
  reviews.Decision
  reviews.DecisionItem
  reviews.AuditEvent
  reviews.IdempotencyRecord
  runs.RunItem
  notifications.Message
)

workdir="$PG_RUN_DIR/p12"
mkdir -p "$workdir"
stamp="$(date +%Y%m%dT%H%M%S)"
workcopy="$workdir/source-upgraded-$stamp.sqlite3"
fixture="$workdir/sqlite-dump-$stamp.json"

cd "$REPO_ROOT"
python_bin="$REPO_ROOT/.venv-customs/bin/python"
if [ ! -x "$python_bin" ]; then
  echo "缺少 ${python_bin}；先执行 make setup" >&2
  exit 2
fi

echo "1/5 复制源库并补迁移（源文件不动）：$sqlite_src"
cp "$sqlite_src" "$workcopy"
CUSTOMS_DB_BACKEND=sqlite CUSTOMS_SQLITE_PATH="$workcopy" \
  "$python_bin" manage.py migrate --noinput >/dev/null

if command -v sqlite3 >/dev/null 2>&1; then
  echo "2/5 迁移前后行数比对（只比原库已有的表）"
  drifted=0
  while IFS= read -r table; do
    [ -n "$table" ] || continue
    before="$(sqlite3 "$sqlite_src" "select count(*) from \"$table\"" 2>/dev/null || echo "?")"
    after="$(sqlite3 "$workcopy" "select count(*) from \"$table\"" 2>/dev/null || echo "?")"
    if [ "$before" != "$after" ]; then
      echo "    行数变化：$table $before → ${after}（需人工确认）"
      drifted=1
    fi
  done < <(sqlite3 "$sqlite_src" "select name from sqlite_master where type='table' and name not like 'sqlite_%'")
  [ "$drifted" = 0 ] && echo "    所有原表行数一致"
else
  echo "2/5 未安装 sqlite3 命令行，跳过行数比对"
fi

echo "3/5 从升级后的副本导出（保留微秒，见 dumpdata_full.py）：$fixture"
CUSTOMS_DB_BACKEND=sqlite CUSTOMS_SQLITE_PATH="$workcopy" \
  "$python_bin" "$SCRIPT_DIR/dumpdata_full.py" "$fixture" "${MODELS[@]}"
echo "    $(wc -c < "$fixture" | tr -d ' ') 字节"

export CUSTOMS_DB_BACKEND=postgres
# pg_env.sh 用的是 PG* 变量名，Django 读 CUSTOMS_PG_*，必须显式映射；
# 只 export 未赋值的变量等于什么都没导出。
export CUSTOMS_PG_NAME="$PGDATABASE_NAME"
export CUSTOMS_PG_USER="$PGROLE"
export CUSTOMS_PG_HOST="$PGHOST"
export CUSTOMS_PG_PORT="$PGPORT"
"$SCRIPT_DIR/pg_ctl.sh" start >/dev/null

if [ "$fresh" = 1 ]; then
  echo "4/5 清空目标库（--fresh）并导入"
  "$python_bin" manage.py flush --noinput
else
  echo "4/5 检查目标库为空（loaddata 对相同主键是覆盖，不是报错）"
  # loaddata 遇到相同主键会更新已有记录，单靠唯一约束不会拦住覆盖，所以必须先显式拒绝非空库。
  nonempty="$("$python_bin" - <<'PY'
import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()
from django.apps import apps
total = 0
for model in apps.get_models():
    if model._meta.auto_created:
        continue
    total += model._default_manager.count()
    if total:
        break
print(total)
PY
)"
  if [ "$nonempty" != "0" ]; then
    echo "目标库非空（至少 ${nonempty} 行）：loaddata 会按主键覆盖已有记录，已拒绝导入。" >&2
    echo "请换一个空库，或确认是本机开发库后加 --fresh 清空再导入。" >&2
    exit 3
  fi
fi
"$python_bin" manage.py loaddata --ignorenonexistent "$fixture"
# 整数主键表（auth.Group 等）的序列要跟着实际数据往前走，否则后续插入撞主键。
"$python_bin" manage.py sqlsequencereset auth | "$PG_BIN/psql" -h "$PGHOST" -p "$PGPORT" \
  -U "$PGROLE" -d "$PGDATABASE_NAME" -v ON_ERROR_STOP=1 -q

echo "5/5 数量 / 外键 / 内容 / 附件引用核对"
"$python_bin" "$SCRIPT_DIR/verify_copy.py" --source "$workcopy" --json "$workdir/verify-$stamp.json"
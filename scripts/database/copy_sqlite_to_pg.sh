#!/usr/bin/env bash
# P12: consistent SQLite snapshot -> PostgreSQL -> strict data/files verification.
# Source and its WAL remain untouched. Stop all writers throughout the cutover.
# --fresh additionally requires CUSTOMS_PG_ALLOW_FLUSH=<exact target database>.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/pg_env.sh"
umask 077
fresh=0
sqlite_src="${CUSTOMS_SQLITE_PATH:-$REPO_ROOT/.local/db.sqlite3}"
source_files_root="${CUSTOMS_SOURCE_FILE_ROOT:-}"
target_files_root="$CUSTOMS_FILE_ROOT"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --fresh) fresh=1; shift ;;
    --source-files-root|--target-files-root)
      if [ "$#" -lt 2 ] || [ -z "$2" ] || [[ "$2" = --* ]]; then
        echo "$1 需要一个非空目录参数" >&2; exit 2
      fi
      if [ "$1" = --source-files-root ]; then source_files_root="$2"; else target_files_root="$2"; fi
      shift 2 ;;
    --help|-h)
      echo "用法：$0 [source.sqlite3] --source-files-root <旧附件目录> [--target-files-root <目标附件目录>] [--fresh]"
      exit 0 ;;
    --*) echo "未知参数：$1" >&2; exit 2 ;;
    *) sqlite_src="$1"; shift ;;
  esac
done
if [ -z "$source_files_root" ]; then
  echo "必须显式提供 --source-files-root（或 CUSTOMS_SOURCE_FILE_ROOT），禁止用当前部署的文件根猜测旧库附件。" >&2
  exit 2
fi
case "$source_files_root" in /*) ;; *) source_files_root="$REPO_ROOT/$source_files_root" ;; esac
case "$target_files_root" in /*) ;; *) target_files_root="$REPO_ROOT/$target_files_root" ;; esac
if [ ! -d "$source_files_root" ]; then echo "源附件根不存在：$source_files_root" >&2; exit 2; fi
export CUSTOMS_FILE_ROOT="$target_files_root"
case "$sqlite_src" in /*) ;; *) sqlite_src="$REPO_ROOT/$sqlite_src" ;; esac
if [ ! -f "$sqlite_src" ]; then
  echo "找不到 SQLite 源库：${sqlite_src}" >&2; exit 2
fi
cd "$REPO_ROOT"
python_bin="${PYTHON:-$REPO_ROOT/.venv-customs/bin/python}"
if [ ! -x "$python_bin" ]; then
  echo "缺少 ${python_bin}；先执行 make setup" >&2; exit 2
fi
# Each run has exclusive artifacts; retries never overwrite prior evidence.
mkdir -p "$PG_RUN_DIR/p12"
workdir="$(mktemp -d "$PG_RUN_DIR/p12/copy-$(date +%Y%m%dT%H%M%S)-XXXXXX")"
workcopy="$workdir/source-upgraded.sqlite3"
fixture="$workdir/sqlite-dump.json"
echo "1/5 SQLite 在线快照（包含 WAL 已提交行，原库不动）：$sqlite_src"
"$python_bin" "$SCRIPT_DIR/snapshot_sqlite.py" "$sqlite_src" "$workcopy"
CUSTOMS_DB_BACKEND=sqlite CUSTOMS_SQLITE_PATH="$workcopy" "$python_bin" - <<'PY'
import os, sqlite3
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from scripts.database.model_inventory import validate_source_history, validate_source_schema
with sqlite3.connect(os.environ['CUSTOMS_SQLITE_PATH']) as source:
    validate_source_history(source)
    validate_source_schema(source)
PY
CUSTOMS_DB_BACKEND=sqlite CUSTOMS_SQLITE_PATH="$workcopy" \
  "$python_bin" manage.py migrate --noinput >"$workdir/source-migrate.log"
# Validate the upgraded schema and physical file references BEFORE touching PG.
CUSTOMS_DB_BACKEND=sqlite CUSTOMS_SQLITE_PATH="$workcopy" CUSTOMS_FILE_ROOT="$source_files_root" \
  "$python_bin" - "$workdir/source-files.json" <<'PY'
import json, os, sqlite3, sys
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.conf import settings
from scripts.database.model_inventory import validate_source_schema, validate_generated_metadata
from scripts.database.verify_copy import attachment_manifest
with sqlite3.connect(os.environ['CUSTOMS_SQLITE_PATH']) as source:
    validate_source_schema(source)
    validate_generated_metadata(source)
files = attachment_manifest(settings.CUSTOMS_FILE_ROOT)
with open(sys.argv[1], 'x', encoding='utf-8') as handle:
    json.dump(files, handle, ensure_ascii=False, indent=2)
print(f'源附件 {len(files)} 个实体，已保存迁移前大小/SHA-256')
PY
MODELS=()
while IFS= read -r label; do
  MODELS+=("$label")
done < <(CUSTOMS_DB_BACKEND=sqlite CUSTOMS_SQLITE_PATH="$workcopy" \
  "$python_bin" "$SCRIPT_DIR/model_inventory.py")
if [ "${#MODELS[@]}" -eq 0 ]; then echo "模型清单为空，拒绝迁移" >&2; exit 2; fi
echo "2/5 导出 ${#MODELS[@]} 个模型（含导入/认领记录、M2M、完整微秒）：$fixture"
CUSTOMS_DB_BACKEND=sqlite CUSTOMS_SQLITE_PATH="$workcopy" \
  "$python_bin" "$SCRIPT_DIR/dumpdata_full.py" "$fixture" "${MODELS[@]}"
export CUSTOMS_DB_BACKEND=postgres
export CUSTOMS_PG_NAME="$PGDATABASE_NAME"
export CUSTOMS_PG_USER="$PGROLE"
export CUSTOMS_PG_HOST="$PGHOST"
export CUSTOMS_PG_PORT="$PGPORT"
if ! "$PG_BIN/pg_isready" -h "$PGHOST" -p "$PGPORT" -q; then
  echo "目标 PostgreSQL 未就绪；先执行 bootstrap.sh 或启动目标集群" >&2; exit 2
fi
# Fail before any data import/flush if the target schema is not current.
"$python_bin" manage.py migrate --check
echo "3/5 检查目标库为空（loaddata 对相同主键是覆盖）"
nonempty="$("$python_bin" - "${MODELS[@]}" <<'PY'
import os, sys, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.apps import apps
from django.db import connection
from scripts.database.model_inventory import validate_target_metadata
# Only the exact migrate-generated baseline may be ignored as target data.
validate_target_metadata(connection)
# Unknown nonempty tables are protected too.
ignored = {'django_migrations', 'auth_permission', 'django_content_type'}
with connection.cursor() as cursor:
    total = 0
    for table in connection.introspection.table_names():
        if table not in ignored:
            cursor.execute(f'SELECT COUNT(*) FROM {connection.ops.quote_name(table)}')
            total += cursor.fetchone()[0]
    for label in sys.argv[1:]:
        apps.get_model(label)  # confirm the actual import model list resolves
print(total)
PY
)"
if [ "$fresh" = 1 ] && [ "${CUSTOMS_PG_ALLOW_FLUSH:-}" != "$PGDATABASE_NAME" ]; then
  echo "--fresh 会清空目标；需 CUSTOMS_PG_ALLOW_FLUSH=$PGDATABASE_NAME 精确授权。" >&2; exit 3
elif [ "$fresh" = 0 ] && [ "$nonempty" != "0" ]; then
  echo "目标库非空（${nonempty} 行）：拒绝覆盖，请换全新空库。" >&2; exit 3
fi
# Copy referenced bytes to the TARGET root before import/flush. All collisions
# are checked first; identical existing files can be reused, never overwritten.
"$python_bin" "$SCRIPT_DIR/copy_attachments.py" --source-files-root "$source_files_root" \
  --target-files-root "$target_files_root" --manifest "$workdir/source-files.json" \
  --json "$workdir/file-transfer.json"
if [ "$fresh" = 1 ]; then "$python_bin" manage.py flush --noinput; fi
echo "4/5 导入并校正所有整数主键序列"
# No --ignorenonexistent: an unknown field must fail instead of being discarded.
"$python_bin" manage.py loaddata "$fixture"
"$python_bin" - <<'PY'
import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()
from django.core.management.color import no_style
from django.db import connection
from scripts.database.model_inventory import verification_models
with connection.cursor() as cursor:
    for statement in connection.ops.sequence_reset_sql(no_style(), verification_models()):
        cursor.execute(statement)
PY
echo "5/5 全模型 / M2M / 外键 / 内容 / 附件实体核对"
"$python_bin" "$SCRIPT_DIR/verify_copy.py" --source "$workcopy" \
  --files-manifest "$workdir/source-files.json" --source-files-root "$source_files_root" \
  --target-files-root "$target_files_root" --json "$workdir/verify.json"
echo "迁移通过；证据：${workdir}。切换 .env 前请完成 P22 数据库+文件备份恢复演练。"

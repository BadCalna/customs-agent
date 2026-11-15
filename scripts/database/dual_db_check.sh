#!/usr/bin/env bash
# P12：同一组测试在 SQLite 与 PostgreSQL 上各跑一遍，生成双库差异报告。
#
#   scripts/database/dual_db_check.sh [套件目录...]
#
# 默认套件：P12（数据库专项）、P09/P10（提交与版本场景）、P01/P02（模型/权限/契约回归）。
# 报告写到 .local/p12/dual-db-report.md，并原样回显到标准输出。
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./pg_env.sh
. "$SCRIPT_DIR/pg_env.sh"

REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
PYTHON="$REPO_ROOT/.venv-customs/bin/python"
out_dir="$PG_RUN_DIR/p12"
mkdir -p "$out_dir"
report="$out_dir/dual-db-report.md"

suites=("$@")
if [ ${#suites[@]} -eq 0 ]; then
  suites=(tests/p12 tests/p09 tests/p10 tests/p01 tests/p02)
fi

"$SCRIPT_DIR/pg_ctl.sh" start >/dev/null

run_suite() {  # $1=backend $2=suite
  local backend="$1" suite="$2"
  local log="$out_dir/$(echo "$backend-$suite" | tr '/' '-').log"
  if [ "$backend" = "postgres" ]; then
    CUSTOMS_DB_BACKEND=postgres "$PYTHON" manage.py test "$suite" >"$log" 2>&1
  else
    CUSTOMS_DB_BACKEND=sqlite "$PYTHON" manage.py test "$suite" >"$log" 2>&1
  fi
  local code=$?
  local line
  line="$(grep -E '^(Ran [0-9]+ test|OK|FAILED)' "$log" | tr '\n' ' ' | sed 's/ *$//')"
  echo "$code|$line"
}

{
  echo "# 双库差异报告（$(date '+%Y-%m-%d %H:%M')）"
  echo
  echo "同一组测试、同一份代码，分别在 SQLite 与 PostgreSQL 上运行。"
  echo "「--」表示该后端跳过（PostgreSQL 专属检查）。"
  echo
  echo "| 套件 | SQLite | PostgreSQL | 差异说明 |"
  echo "|---|---|---|---|"
} > "$report"

exit_code=0
for suite in "${suites[@]}"; do
  sqlite_result="$(run_suite sqlite "$suite")"
  pg_result="$(run_suite postgres "$suite")"
  sqlite_code="${sqlite_result%%|*}"
  sqlite_text="${sqlite_result#*|}"
  pg_code="${pg_result%%|*}"
  pg_text="${pg_result#*|}"

  note="—"
  if [ "$sqlite_code" != "0" ] && [ "$pg_code" = "0" ]; then
    note="SQLite 失败、PostgreSQL 通过；按差异逐条确认是否后端限制"
  elif [ "$pg_code" != "0" ]; then
    note="**PostgreSQL 未通过**"
    exit_code=1
  elif [ "$sqlite_code" = "0" ] && [ "$pg_code" = "0" ]; then
    note="一致"
  fi
  # 跳过数不同（PG 专属用例）也记一笔。
  if echo "$sqlite_text" | grep -q "skipped" && ! echo "$pg_text" | grep -q "skipped"; then
    note="SQLite 跳过的 PostgreSQL 专属用例在 PG 上执行"
  fi
  echo "| $suite | ${sqlite_text:-失败}（退出 ${sqlite_code}） | ${pg_text:-失败}（退出 ${pg_code}） | $note |" >> "$report"
done

cat >> "$report" <<EOF

## 读法

- 退出码非 0 表示该后端下有失败；两边都 0 才说明“同一组场景在两个后端都成立”。
- SQLite 侧跳过的是并发与 DDL 目录检查：SQLite 只有单写者、没有 \`jsonb\`，
  无法给出生产可用的并发结论（D6 第 3 节）。
- 逐条日志在 \`.local/p12/*.log\`，不进 Git。
EOF

cat "$report"
exit "$exit_code"
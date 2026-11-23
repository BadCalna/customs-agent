UV ?= uv
export UV_PROJECT_ENVIRONMENT := .venv-customs
PYTHON := $(UV_PROJECT_ENVIRONMENT)/bin/python
MANAGE := $(PYTHON) manage.py
BIND ?= 127.0.0.1:8000
export TASK

.PHONY: setup migrate seed-demo seed-ops-demo ops-demo seed-contracts contracts demo-components workbench dev check test test-contracts worker dispatcher \
        pg-init pg-start pg-stop pg-status db-backup db-copy check-postgres \
        reference-scan reference-import reference-publish reference-releases test-integration
setup:
	$(UV) sync --locked
	$(PYTHON) -m config.bootstrap
	@echo "Local configuration ready. See .env.example for optional overrides."

migrate:
	$(MANAGE) migrate --noinput

seed-demo:
	$(MANAGE) seed_demo

# 01/02/03 本地演示：合成样例 + mock 调试台，不出站。密钥与密文目录在仓库外。
seed-ops-demo:
	$(MANAGE) seed_ops_demo

ops-demo:
	bash scripts/operations/ops_demo.sh

seed-contracts:
	$(MANAGE) seed_contracts

contracts:
	$(MANAGE) export_contracts

demo-components:
	$(MANAGE) seed_contracts
	$(MANAGE) export_contracts
	@echo ""
	@echo "组件演示壳：http://$(BIND)/demo/"
	@echo "  LIVE 模式按真实权限读只读接口；FIXTURE 模式读 DEMO-SET.json，不校验权限。"
	@echo "  写动作在演示壳里只展示按钮，点击提示 501。"
	@echo ""
	$(MANAGE) runserver $(BIND)

test-contracts:
	# Django 不支持以文件路径作为测试标签；契约、样例、端点与 OpenAPI 检查都在 tests/p02。
	$(MANAGE) test tests/p02

dev:
	$(MANAGE) runserver $(BIND)

QUEUE ?= $(shell $(PYTHON) -c "from config.settings import CUSTOMS_QUEUE_NAME; print(CUSTOMS_QUEUE_NAME)")
worker:
	$(MANAGE) rqworker $(QUEUE)

dispatcher:
	$(MANAGE) dispatch_runs --watch

workbench:
	@echo "关务预审工作台：http://$(BIND)/（未登录自动进入 /login/）"
	@echo "  运行与导出：/workbench/ ；上传与认领：/uploads/ 。三个入口共享业务会话。"
	@echo "  请核对任务版本与结果来源；模拟结果不能作为申报依据。"
	$(MANAGE) runserver $(BIND)

check:
	$(PYTHON) -m compileall -q manage.py config apps tests
	$(MANAGE) check
	$(MANAGE) makemigrations --check --dry-run

test:
	@case "$$TASK" in P[0-9][0-9]) ;; *) echo "Use make test TASK=Pxx" >&2; exit 2;; esac; \
	if [ "$$TASK" = P21 ]; then $(PYTHON) tests/integration/run.py --backend $(P21_BACKEND); exit $$?; fi; \
	dir="tests/$$(echo "$$TASK" | tr 'A-Z' 'a-z')"; \
	if [ ! -d "$$dir" ]; then echo "No test entry for $$TASK" >&2; exit 2; fi; \
	$(MANAGE) test "$$dir"

# P21 fixes mock/inline and isolated file storage; PG requires an explicit local P21 database.
P21_BACKEND ?= sqlite
test-integration:
	$(PYTHON) tests/integration/run.py --backend $(P21_BACKEND)

# ---------------------------------------------------------------------------
# P12：推荐 PostgreSQL（scripts/database/ 管理本机集群；无配置时仍可用 SQLite）
# ---------------------------------------------------------------------------
pg-init:
	scripts/database/pg_ctl.sh init

pg-start:
	scripts/database/pg_ctl.sh start

pg-stop:
	scripts/database/pg_ctl.sh stop

pg-status:
	scripts/database/pg_ctl.sh status

db-backup:
	scripts/database/backup.sh

# SOURCE_FILES_ROOT is mandatory; target defaults to CUSTOMS_FILE_ROOT.
# Example: make db-copy SOURCE_SQLITE=/path/source.sqlite3 SOURCE_FILES_ROOT=/path/files
SOURCE_SQLITE ?= $(CUSTOMS_SQLITE_PATH)
SOURCE_FILES_ROOT ?= $(CUSTOMS_SOURCE_FILE_ROOT)
TARGET_FILES_ROOT ?= $(CUSTOMS_FILE_ROOT)
db-copy:
	scripts/database/copy_sqlite_to_pg.sh $(if $(SOURCE_SQLITE),"$(SOURCE_SQLITE)") $(if $(SOURCE_FILES_ROOT),--source-files-root "$(SOURCE_FILES_ROOT)") $(if $(TARGET_FILES_ROOT),--target-files-root "$(TARGET_FILES_ROOT)")

# 只有“后端确实是 PostgreSQL 且连接可用”才算过；端口连通不算（D6 第 3 节）。
check-postgres:
	@$(PYTHON) -c "import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup(); from django.db import connection; print(connection.vendor)" | grep -qx postgresql || { \
		echo "check-postgres 需要 PostgreSQL 后端：先 scripts/database/bootstrap.sh，并设置 CUSTOMS_DB_BACKEND=postgres 与 CUSTOMS_PG_*。" >&2; exit 2; }
	$(MANAGE) check
	$(MANAGE) makemigrations --check --dry-run
	$(MANAGE) test --noinput tests/p12 tests/p05 tests/p06 tests/p09 tests/p10

# ---------------------------------------------------------------------------
# P23：官方参考数据导入（真实数据与报告写入 Git 忽略的 .local/reference-import）
# 不写业务数据库、不改 .env、不联网；发布只产出目录与指针。
# ---------------------------------------------------------------------------
REFERENCE_SOURCES ?= data/import-basis/2026-11-17/reference-sources.json
REFERENCE_OUT ?= .local/reference-import
reference-scan:
	$(PYTHON) scripts/import_reference_data.py scan --sources $(REFERENCE_SOURCES) --out $(REFERENCE_OUT)/scan.json

reference-import:
	$(PYTHON) scripts/import_reference_data.py import --sources $(REFERENCE_SOURCES) --out-root $(REFERENCE_OUT)

# 换版（覆盖已发布内容）必须显式声明：make reference-publish ... ALLOW_CHANGE=1 REASON="为什么换版"
reference-publish:
	@test -n "$(SNAPSHOT)" || { echo "用法：make reference-publish SNAPSHOT=$(REFERENCE_OUT)/candidates/<snapshot_id> [REVIEW=<复核文件>] [ALLOW_CHANGE=1 REASON=...]" >&2; exit 2; }
	@test -z "$(ALLOW_CHANGE)" || test -n "$(REASON)" || { echo "ALLOW_CHANGE=1 必须同时给 REASON=..." >&2; exit 2; }
	$(PYTHON) scripts/import_reference_data.py publish --snapshot "$(SNAPSHOT)" --out-root $(REFERENCE_OUT) \
		$(if $(REVIEW),--review "$(REVIEW)") \
		$(if $(ALLOW_CHANGE),--allow-content-change --reason "$(REASON)")

reference-releases:
	$(PYTHON) scripts/import_reference_data.py releases --out-root $(REFERENCE_OUT)

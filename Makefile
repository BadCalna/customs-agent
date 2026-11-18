UV ?= uv
export UV_PROJECT_ENVIRONMENT := .venv-customs
PYTHON := $(UV_PROJECT_ENVIRONMENT)/bin/python
MANAGE := $(PYTHON) manage.py
BIND ?= 127.0.0.1:8000
export TASK

.PHONY: setup migrate seed-demo seed-contracts contracts demo-components workbench dev check test test-contracts worker dispatcher \
        pg-init pg-start pg-stop pg-status db-backup db-copy check-postgres
setup:
	$(UV) sync --locked
	$(PYTHON) -m config.bootstrap
	@echo "Local configuration ready. See .env.example for optional overrides."

migrate:
	$(MANAGE) migrate --noinput

seed-demo:
	$(MANAGE) seed_demo

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
	@echo "工作台独立演示入口：http://$(BIND)/workbench/（现有首页不变）"
	@echo "  默认 FIXTURE 样例数据；?mode=live 读 /api/v1，?mode=demo 内置演示。"
	@echo "  还有商品未确认时显示『还有 N 条待确认』并禁用整批提交。"
	@echo "  真实读/审批接通后再设为首页；写动作待后端 P 任务。"
	$(MANAGE) runserver $(BIND)

check:
	$(PYTHON) -m compileall -q manage.py config apps tests
	$(MANAGE) check
	$(MANAGE) makemigrations --check --dry-run

test:
	@case "$$TASK" in P[0-9][0-9]) ;; *) echo "Use make test TASK=Pxx" >&2; exit 2;; esac; \
	dir="tests/$$(echo "$$TASK" | tr 'A-Z' 'a-z')"; \
	if [ ! -d "$$dir" ]; then echo "No test entry for $$TASK" >&2; exit 2; fi; \
	$(MANAGE) test "$$dir"

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

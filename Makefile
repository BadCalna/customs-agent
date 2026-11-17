UV ?= uv
export UV_PROJECT_ENVIRONMENT := .venv-customs
PYTHON := $(UV_PROJECT_ENVIRONMENT)/bin/python
MANAGE := $(PYTHON) manage.py
BIND ?= 127.0.0.1:8000
export TASK

.PHONY: setup migrate seed-demo seed-contracts contracts demo-components workbench dev check test test-contracts \
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

workbench:
	@echo "正式工作台：http://$(BIND)/workbench/（真实登录会话与 API）"
	@echo "  样例仅 DEBUG 下显式 ?mode=fixture 或 ?mode=demo。"
	@echo "  P13 队列/重试及专家工作簿布局仍待交付；mock AI 不构成申报依据。"
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
# P12：PostgreSQL（本机集群由 scripts/database/ 管理，默认仍用 SQLite）
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

db-copy:
	scripts/database/copy_sqlite_to_pg.sh

# 只有“后端确实是 PostgreSQL 且连接可用”才算过；端口连通不算（D6 第 3 节）。
check-postgres:
	@$(PYTHON) -c "import os, django; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings'); django.setup(); from django.db import connection; print(connection.vendor)" | grep -qx postgresql || { \
		echo "check-postgres 需要 PostgreSQL 后端：先 scripts/database/bootstrap.sh，并设置 CUSTOMS_DB_BACKEND=postgres 与 CUSTOMS_PG_*。" >&2; exit 2; }
	$(MANAGE) check
	$(MANAGE) makemigrations --check --dry-run
	$(MANAGE) test tests/p12 tests/p09 tests/p10

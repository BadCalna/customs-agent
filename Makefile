UV ?= uv
export UV_PROJECT_ENVIRONMENT := .venv-customs
PYTHON := $(UV_PROJECT_ENVIRONMENT)/bin/python
MANAGE := $(PYTHON) manage.py
BIND ?= 127.0.0.1:8000
export TASK

.PHONY: setup migrate seed-demo seed-contracts contracts demo-components workbench dev check test test-contracts
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

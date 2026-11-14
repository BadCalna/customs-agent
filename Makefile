UV ?= uv
export UV_PROJECT_ENVIRONMENT := .venv-customs
PYTHON := $(UV_PROJECT_ENVIRONMENT)/bin/python
MANAGE := $(PYTHON) manage.py
BIND ?= 127.0.0.1:8000
export TASK

.PHONY: setup migrate seed-demo dev check test
setup:
	$(UV) sync --locked
	$(PYTHON) -m config.bootstrap
	@echo "Local configuration ready. See .env.example for optional overrides."

migrate:
	$(MANAGE) migrate --noinput

seed-demo:
	$(MANAGE) seed_demo

dev:
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

PYTHON ?= ./.venv/bin/python
EVAL_ARGS ?=

.PHONY: start stop status test lint eval migrate dev check-architecture setup-agents setup-main-agent setup-coder-agent setup-embedding-agent setup-evaluation-agent sync-main-agent-prompt check-main-agent validate-runtimes db-export db-export-git db-import db-export-all db-import-all

start:
	./run.sh start

stop:
	./run.sh stop

status:
	./run.sh status

test:
	$(PYTHON) -m unittest

lint:
	$(PYTHON) -m compileall app tests
	$(PYTHON) -m unittest tests.test_legacy_import_check tests.test_architecture_validation

eval:
	$(PYTHON) scripts/run_evals.py $(EVAL_ARGS)

migrate:
	$(PYTHON) -m alembic upgrade head

dev:
	SSL_CERT_FILE=certs/local_cloudflare.cert $(PYTHON) -m uvicorn app:app --reload

check-architecture:
	$(PYTHON) -m unittest tests.test_legacy_import_check tests.test_architecture_validation

setup-agents:
	$(PYTHON) scripts/setup.py

setup-main-agent:
	$(PYTHON) scripts/setup.py main-agent

setup-coder-agent:
	$(PYTHON) scripts/setup.py coder-agent

setup-embedding-agent:
	$(PYTHON) scripts/setup.py embedding-agent

setup-evaluation-agent:
	$(PYTHON) scripts/setup.py evaluation-agent

sync-main-agent-prompt:
	$(PYTHON) scripts/setup.py sync-main-agent-prompt

check-main-agent:
	$(PYTHON) scripts/setup.py check-main-agent

validate-runtimes:
	$(PYTHON) scripts/validate_runtime_paths.py

db-export:
	$(PYTHON) scripts/db_snapshot.py export --database agency

db-export-git:
	$(PYTHON) scripts/db_snapshot.py export --database agency --git-add

db-import:
	$(PYTHON) scripts/db_snapshot.py import --database agency --yes

db-export-all:
	$(PYTHON) scripts/db_snapshot.py export --database all

db-import-all:
	$(PYTHON) scripts/db_snapshot.py import --database all --yes

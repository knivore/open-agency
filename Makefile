PYTHON ?= ./.venv/bin/python
EVAL_ARGS ?=

.PHONY: test lint eval migrate dev bootstrap start restart stop status doctor check-architecture check-tool-registry check-browser-runtime check-optional-modules check-module-separation secret-scan setup-local-onboarding sync-recommended-agents setup-main-agent setup-coder-agent setup-embedding-agent setup-evaluation-agent sync-main-agent-prompt check-main-agent setup-chat-channel chat-main-agent validate-runtimes db-export db-import db-export-all db-import-all

test:
	$(PYTHON) -m unittest

lint:
	$(PYTHON) -m compileall app tests
	$(PYTHON) -m unittest tests.test_documentation_consistency tests.test_legacy_import_check tests.test_architecture_validation tests.test_repo_hygiene
	$(MAKE) check-optional-modules PYTHON=$(PYTHON)

eval:
	$(PYTHON) scripts/run_evals.py $(EVAL_ARGS)

migrate:
	$(PYTHON) -m alembic upgrade head

dev:
	SSL_CERT_FILE=certs/local_cloudflare.cert $(PYTHON) -m uvicorn app:app --reload

bootstrap:
	./run.sh bootstrap

start:
	./run.sh start

restart:
	./run.sh restart

stop:
	./run.sh stop

status:
	./run.sh status

doctor:
	./run.sh doctor

check-architecture:
	$(PYTHON) -m unittest tests.test_documentation_consistency tests.test_legacy_import_check tests.test_architecture_validation tests.test_repo_hygiene
	$(MAKE) check-optional-modules PYTHON=$(PYTHON)

check-tool-registry:
	$(PYTHON) -m unittest tests.test_tool_migration tests.test_tool_cli

check-browser-runtime:
	docker build -f docker/browser-runtime/Dockerfile -t agency-browser-runtime:test .
	docker run --rm --shm-size=1gb --memory=4g --cpus=4 --pids-limit=512 agency-browser-runtime:test python -m app.browser_runtime.selftest

check-optional-modules:
	$(PYTHON) scripts/validate_optional_module_persistence.py --check-paths

check-module-separation:
	$(PYTHON) -m pytest tests/test_api_main.py::ApiMainTests::test_core_only_mode_omits_builtin_optional_module_specs tests/test_api_main.py::ApiMainTests::test_capability_execution_metadata_uses_active_optional_module_specs tests/test_optional_module_registry.py -q

secret-scan:
	$(PYTHON) scripts/scan_secrets.py --repo .

setup-local-onboarding:
	$(PYTHON) scripts/setup.py local-onboarding

sync-recommended-agents:
	$(PYTHON) scripts/setup.py recommended-agents

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

chat-main-agent:
	$(PYTHON) -m app.cli chat-main-agent

setup-chat-channel:
	$(PYTHON) -m app.cli setup-chat-channel $(CHANNEL)

validate-runtimes:
	$(PYTHON) scripts/validate_runtime_paths.py

db-export:
	$(PYTHON) scripts/db_snapshot.py export --database agency

db-import:
	$(PYTHON) scripts/db_snapshot.py import --database agency --yes

db-export-all:
	$(PYTHON) scripts/db_snapshot.py export --database all

db-import-all:
	$(PYTHON) scripts/db_snapshot.py import --database all --yes

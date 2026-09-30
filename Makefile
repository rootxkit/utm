# UTM — developer entry points.
#
# Every acceptance criterion in TASKS.md is phrased as a make target, so this
# file is the contract. Recipes are POSIX shell: on Windows run them from Git
# Bash or WSL.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

# Do NOT add --project-directory here. Compose resolves relative bind-mount
# paths against the project directory, so overriding it to the repository root
# makes ./initdb in the compose file point at a path that does not exist —
# Docker then creates it empty and the init SQL silently never runs. Leaving it
# unset puts the project directory at infra/, where those paths are correct.
#
# The consequence is that .env is no longer picked up automatically, so it is
# passed explicitly when present. The compose file defaults every variable, so
# a fresh clone with no .env still comes up.
ENV_FILE := $(wildcard .env)
COMPOSE := docker compose -f infra/docker-compose.dev.yml $(if $(ENV_FILE),--env-file $(ENV_FILE))

N ?= 1

PYTHON ?= $(shell command -v python3 2>/dev/null || command -v python)
VENV := .venv
ifeq ($(OS),Windows_NT)
VENV_BIN := $(VENV)/Scripts
else
VENV_BIN := $(VENV)/bin
endif

# The probe also runs on the ground-station PC, which has QGC and a vehicle but
# is not a development machine and may have no venv. Use the venv interpreter
# when it is there and fall back to a bare one otherwise.
#
# The fallback is `python` on Windows, not `python3`: Windows ships a
# python3.exe App Execution Alias that is not an interpreter — it prints a
# Microsoft Store advertisement and exits non-zero, which reads as a confusing
# failure rather than a missing interpreter.
ifeq ($(OS),Windows_NT)
PROBE_FALLBACK_PYTHON := python
else
PROBE_FALLBACK_PYTHON := python3
endif
PROBE_PYTHON := $(if $(wildcard $(VENV_BIN)/python*),$(VENV_BIN)/python,$(PROBE_FALLBACK_PYTHON))

PROBE := $(PROBE_PYTHON) tools/mavlink_probe.py

# Left empty so the probe's own defaults stay the single source of truth;
# set any of them on the command line to override.
PROBE_HOST ?=
PROBE_PORT ?=
PROBE_JSON ?=
PROBE_SECONDS ?= 30
PROBE_PARAM ?= SYSID_THISMAV
PROBE_OPTS := $(if $(PROBE_HOST),--host $(PROBE_HOST)) $(if $(PROBE_PORT),--port $(PROBE_PORT))

# Coverage ratchets, set just under the figures measured on 2026-09-21.
# Raise them when coverage rises; lowering one needs a reason in the commit.
COVERAGE_MIN_AGENT ?= 95
COVERAGE_MIN_PROBE ?= 35
COVERAGE_MIN_GATEWAY ?= 92
# The database-only modules are excluded from the figure above and gated
# separately by `make test-db`: their tests need a database, so counting them
# in a run that skips those tests would ratchet against coverage that was
# never measured.
COVERAGE_DB_ONLY := gateway/ingest_store_pg.py,gateway/retention.py,gateway/binding.py,gateway/state_writer.py,gateway/firmware_store.py
COVERAGE_MIN_DB_MODULES ?= 85

.PHONY: help hooks up down stop ps logs reset psql psql-telemetry sim sim-stop \
        venv lint fmt typecheck test test-cov test-slow test-sitl cover clean probe probe-roundtrip migrate migrate-down migrate-relational migrate-relational-down api

help: ## Show this help
	@echo "UTM — available targets:"
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

hooks: ## Install the repository git hooks via core.hooksPath
	@git config core.hooksPath .githooks
	@chmod +x .githooks/* 2>/dev/null || true
	@echo "hooks: core.hooksPath -> .githooks"

up: ## Start the dev stack and block until every service is healthy
	$(COMPOSE) up -d --wait
	@$(MAKE) --no-print-directory ps

down: ## Stop the dev stack, keeping data volumes
	$(COMPOSE) down

stop: ## Stop containers without removing them
	$(COMPOSE) stop

ps: ## Show stack status and health
	@$(COMPOSE) ps --format 'table {{.Service}}\t{{.Status}}\t{{.Ports}}'

logs: ## Tail stack logs (make logs S=postgres for one service)
	$(COMPOSE) logs -f --tail=100 $(S)

reset: ## Destroy the stack AND its data volumes, then start clean
	$(COMPOSE) down -v
	$(MAKE) --no-print-directory up

psql: ## Open a psql shell on the relational database
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER:-courier} -d $${POSTGRES_DB:-courier}

psql-telemetry: ## Open a psql shell on the telemetry database
	$(COMPOSE) exec timescale psql -U $${TIMESCALE_USER:-courier} -d $${TIMESCALE_DB:-courier_telemetry}

sim: ## Launch N SITL vehicles (make sim N=10)
	./sim/run_sitl.sh -n $(N)

sim-stop: ## Stop every SITL vehicle
	./sim/stop_sitl.sh

# U-16: the SITL vehicles of `make sim` as Remote ID broadcasts, heard by one
# simulated receiver and sent to the Remote ID ingest. The geoid must be the
# ingest's (GEOID_PATH), or the heights are wrong by the difference. Set
# RID_KEY_FILE= (empty) for an ingest that runs without receiver keys.
RID_RECEIVER ?= sitl-rx-1
RID_KEY_FILE ?= local/remote-id-receivers.keys
RID_SERIAL ?= SITLRID{sysid:04d}
RID_OPERATOR ?= GEO-OP-SITL
GEOID_PATH ?= local/geoid/egm2008-2_5.pgm
RID_OPTS ?=

sitl-rid: venv ## Broadcast N SITL vehicles as Remote ID (make sitl-rid N=3)
	$(VENV_BIN)/python -m tools.sitl_remote_id --count $(N) 		--serial '$(RID_SERIAL)' --operator-id '$(RID_OPERATOR)' 		--receiver-id '$(RID_RECEIVER)' $(if $(RID_KEY_FILE),--key-file '$(RID_KEY_FILE)') 		--geoid '$(GEOID_PATH)' $(RID_OPTS)

probe: ## Inventory what QGC forwarding delivers (PROBE_SECONDS=30, PROBE_JSON=path)
	$(PROBE) listen $(PROBE_OPTS) --seconds $(PROBE_SECONDS) $(if $(PROBE_JSON),--json $(PROBE_JSON))

probe-roundtrip: ## Test whether the QGC forwarding socket is bidirectional
	$(PROBE) roundtrip $(PROBE_OPTS) --param $(PROBE_PARAM)

$(VENV_BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(VENV_BIN)/python -m pip install --quiet --upgrade pip
	$(VENV_BIN)/python -m pip install --quiet -e '.[dev]'

venv: $(VENV_BIN)/python ## Create .venv and install the workspace with dev extras

fmt: venv ## Format and apply safe lint fixes
	$(VENV_BIN)/ruff format .
	$(VENV_BIN)/ruff check --fix .

lint: venv typecheck ## Lint and typecheck everything
	$(VENV_BIN)/ruff check .
	$(VENV_BIN)/ruff format --check .

typecheck: venv ## mypy, strict on the safety-relevant modules
	$(VENV_BIN)/mypy

test: venv ## Run unit tests (slow and SITL tests excluded)
	$(VENV_BIN)/pytest -m 'not sitl and not slow and not postgres and not nats and not redis'

test-cov: venv ## Run unit tests with a coverage report
	$(VENV_BIN)/pytest -m 'not sitl and not slow and not postgres and not nats and not redis' --cov --cov-branch --cov-report=term-missing

cover: venv ## Branch coverage with the thresholds enforced (what CI runs)
	$(VENV_BIN)/pytest -m 'not sitl and not slow and not postgres and not nats and not redis' --cov --cov-branch --cov-report=term-missing:skip-covered --cov-report=xml
	@echo
	@echo "=== agent/ - uncovered lines and branch arcs ==="
	$(VENV_BIN)/coverage report --include='agent/*' --show-missing --fail-under=$(COVERAGE_MIN_AGENT)
	@echo
	@echo "=== gateway/ - uncovered lines and branch arcs ==="
	$(VENV_BIN)/coverage report --include='gateway/*' --omit='$(COVERAGE_DB_ONLY)' --show-missing --fail-under=$(COVERAGE_MIN_GATEWAY)
	@echo
	@echo "=== tools/mavlink_probe.py - uncovered ==="
	$(VENV_BIN)/coverage report --include='tools/mavlink_probe.py' --show-missing --fail-under=$(COVERAGE_MIN_PROBE)

# Two migration trees, two databases, never merged. See CLAUDE.md.
TELEMETRY_ALEMBIC := $(VENV_BIN)/alembic -c infra/migrations/telemetry/alembic.ini

migrate: venv ## Apply telemetry database migrations
	$(TELEMETRY_ALEMBIC) upgrade head

migrate-down: venv ## Roll the telemetry database back one revision
	$(TELEMETRY_ALEMBIC) downgrade -1

RELATIONAL_ALEMBIC := $(VENV_BIN)/alembic -c infra/migrations/relational/alembic.ini

migrate-relational: venv ## Apply relational database migrations (P2-01)
	$(RELATIONAL_ALEMBIC) upgrade head

migrate-relational-down: venv ## Roll the relational database back one revision
	$(RELATIONAL_ALEMBIC) downgrade -1

api: venv ## Serve the core API on :8010 (needs .env and `make up`)
	$(VENV_BIN)/python -m api

console: venv ## Serve the P1-08 map on :8000 (needs .env and `make up`)
	$(VENV_BIN)/python -m api.console

test-bus: venv ## Run tests that need NATS (make up first)
	$(VENV_BIN)/pytest -m nats -v

# Database tests create and drop their OWN database. The name must end in
# _test; the fixture refuses anything else, because pointing them at the
# development database once marked 14,288 archive rows deleted.
TELEMETRY_TEST_DATABASE_URL ?= postgresql+asyncpg://courier:courier_dev@127.0.0.1:5433/courier_telemetry_test
RELATIONAL_TEST_DATABASE_URL ?= postgresql+asyncpg://courier:courier_dev@127.0.0.1:5432/courier_test

test-db: venv ## Run tests that need the telemetry database (make up first)
	TELEMETRY_TEST_DATABASE_URL="$(TELEMETRY_TEST_DATABASE_URL)" RELATIONAL_TEST_DATABASE_URL="$(RELATIONAL_TEST_DATABASE_URL)" $(VENV_BIN)/pytest -m postgres -v --cov=gateway.ingest_store_pg --cov=gateway.retention --cov=gateway.binding --cov=gateway.state_writer --cov=gateway.firmware_store --cov-branch --cov-report=term-missing
	$(VENV_BIN)/coverage report --include='$(COVERAGE_DB_ONLY)' --show-missing --fail-under=$(COVERAGE_MIN_DB_MODULES)

test-slow: venv ## Run the slow tests excluded from `make test`
	$(VENV_BIN)/pytest -m slow -v

test-sitl: venv ## Run integration tests against already-running SITL vehicles
	@test -n "$${SITL_INSTANCE_COUNT:-}" \
		|| { echo "test-sitl: set SITL_INSTANCE_COUNT to the number of running vehicles" >&2; exit 1; }
	$(VENV_BIN)/pytest -m sitl -v

clean: ## Remove caches, build output and the virtualenv
	rm -rf $(VENV) .mypy_cache .ruff_cache .pytest_cache .coverage htmlcov \
	       coverage.xml build dist ./*.egg-info sim/out
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

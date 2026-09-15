# Customer 360 Intelligence Platform — single entry point for tooling (task 0.2).
#
# `make check` is the gate: formatting, linting, typing and tests across both backend and frontend.
# CI runs exactly this target, so a green local run means a green pipeline.
#
# On Windows without `make`, `npm run check` at the repository root does the same thing.

SHELL := /bin/sh

BACKEND  := backend
FRONTEND := frontend

# Prefer the backend virtualenv so the pinned tool versions are used, not whatever is on PATH.
VENV_BIN := $(BACKEND)/.venv/bin
ifeq ($(OS),Windows_NT)
	VENV_BIN := $(BACKEND)/.venv/Scripts
endif
PY := $(VENV_BIN)/python

NPM := npm --prefix $(FRONTEND)

.DEFAULT_GOAL := help
.PHONY: help install check check-backend check-frontend \
        fmt fmt-backend fmt-frontend \
        lint lint-backend lint-frontend \
        types types-backend types-frontend \
        test test-backend test-frontend \
        eval-ci eval-full \
        dev-api dev-web obs-up obs-down clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------- setup
install: ## Install backend and frontend dependencies from the lock files
	cd $(BACKEND) && uv sync --extra dev
	$(NPM) ci

# ---------------------------------------------------------------- the gate
check: check-backend check-frontend ## Run every quality gate (this is the phase gate)

check-backend: fmt-backend lint-backend types-backend test-backend

check-frontend: ## Format, lint, typecheck and test the frontend
	$(NPM) run check

# ---------------------------------------------------------------- formatting
fmt: fmt-backend fmt-frontend ## Apply formatters

fmt-backend:
	$(PY) -m black $(BACKEND)/src $(BACKEND)/tests $(BACKEND)/migrations
	$(PY) -m ruff check --fix $(BACKEND)/src $(BACKEND)/tests $(BACKEND)/migrations

fmt-frontend:
	$(NPM) run format

# ---------------------------------------------------------------- linting
lint: lint-backend lint-frontend ## Lint without modifying files

# Migrations are linted too: they carry the schema, and E501 in a CHECK constraint is as much a
# review problem there as anywhere else.
lint-backend:
	cd $(BACKEND) && .venv/$(if $(filter Windows_NT,$(OS)),Scripts,bin)/python -m ruff check src tests migrations
	cd $(BACKEND) && .venv/$(if $(filter Windows_NT,$(OS)),Scripts,bin)/python -m black --check src tests migrations

lint-frontend:
	$(NPM) run lint

# ---------------------------------------------------------------- typing
types: types-backend types-frontend ## Type-check both sides

types-backend:
	cd $(BACKEND) && .venv/$(if $(filter Windows_NT,$(OS)),Scripts,bin)/python -m mypy

types-frontend:
	$(NPM) run typecheck

# ---------------------------------------------------------------- tests
test: test-backend test-frontend ## Run all tests with coverage

test-backend:
	cd $(BACKEND) && .venv/$(if $(filter Windows_NT,$(OS)),Scripts,bin)/python -m pytest

test-frontend:
	$(NPM) run test

# ---------------------------------------------------------------- evaluation (Phase 11)
# `eval-ci` is the deterministic gate that runs on every change (design §14.5/§14.8): the mock
# provider, hard gates on schema, groundedness, provenance, entitlement, adversarial and the
# denial-safety of Q&A. It seeds its own throwaway panel database, so it needs no prior `c360 seed`;
# it exits non-zero when any hard gate fails, which is what blocks a merge.
eval-ci: ## Run the deterministic evaluation gate (mock provider); blocks on hard-gate failure
	cd $(BACKEND) && .venv/$(if $(filter Windows_NT,$(OS)),Scripts,bin)/python -m c360 eval run --mode ci

# `eval-full` is the scheduled Bedrock run (nightly / pre-release): all sixteen dimensions including
# latency, cost and ranking quality. Requires AWS credentials and BEDROCK_MODEL_ID; not part of the
# per-commit gate.
eval-full: ## Run the full evaluation against Bedrock with cost reporting (scheduled, not per-commit)
	cd $(BACKEND) && .venv/$(if $(filter Windows_NT,$(OS)),Scripts,bin)/python -m c360 eval run --mode full

# ---------------------------------------------------------------- running
dev-api: ## Start the API with reload (no module-level app; a factory is used deliberately)
	cd $(BACKEND) && .venv/$(if $(filter Windows_NT,$(OS)),Scripts,bin)/python -m uvicorn \
	  c360.main:create_app --factory --reload

dev-web: ## Start the Vite dev server, proxying the API
	$(NPM) run dev

obs-up: ## Start Jaeger, the OTel collector and Prometheus
	docker compose -f docker/docker-compose.yml up -d

obs-down: ## Stop the observability stack
	docker compose -f docker/docker-compose.yml down

clean: ## Remove caches and build output
	rm -rf $(FRONTEND)/dist $(FRONTEND)/coverage
	rm -rf $(BACKEND)/.mypy_cache $(BACKEND)/.ruff_cache $(BACKEND)/.pytest_cache
	rm -f  $(BACKEND)/.coverage $(BACKEND)/coverage.xml

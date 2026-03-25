# ─────────────────────────────────────────────────────────────────────────────
# LinkedIn AI Content System — Makefile
# ─────────────────────────────────────────────────────────────────────────────
# Usage:
#   make setup          First-time setup (generates .env)
#   make up             Start all services
#   make down           Stop all services
#   make test           Run all tests
#   make lint           Run ruff linter
#   make logs           Tail all service logs
#   make shell          Open shell in api container
#   make migrate        Print reminder to run SQL migrations
#   make webhook        Register Telegram webhook
#   make manage         Run management CLI (e.g. make manage CMD="users list")
#   make clean          Remove containers and volumes

.PHONY: setup up down test test-watch lint format logs logs-worker \
        shell migrate webhook manage clean rebuild check-env stats

# ── Setup ──────────────────────────────────────────────────────────────────

setup:
	@echo "Running setup wizard..."
	python scripts/setup.py

check-env:
	@test -f .env || (echo "❌ .env not found. Run: make setup" && exit 1)
	@echo "✓ .env found"

# ── Docker ─────────────────────────────────────────────────────────────────

up: check-env
	docker-compose up --build -d
	@echo ""
	@echo "✓ Services started:"
	@echo "  API:    http://localhost:8000"
	@echo "  Health: http://localhost:8000/health"
	@echo "  Docs:   http://localhost:8000/docs"
	@echo ""
	@echo "Next: run 'make webhook' to register Telegram webhook"

down:
	docker-compose down

rebuild: down
	docker-compose build --no-cache
	docker-compose up -d

clean:
	docker-compose down -v --remove-orphans
	@echo "✓ Containers and volumes removed"

# ── Development ────────────────────────────────────────────────────────────

logs:
	docker-compose logs -f

logs-api:
	docker-compose logs -f api

logs-worker:
	docker-compose logs -f worker

logs-beat:
	docker-compose logs -f beat

shell:
	docker-compose exec api /bin/bash

shell-worker:
	docker-compose exec worker /bin/bash

# ── Testing ────────────────────────────────────────────────────────────────

test:
	pytest tests/ -v --tb=short

test-fast:
	pytest tests/ -v --tb=short -x  # Stop on first failure

test-watch:
	ptw tests/ -- -v --tb=short  # pip install pytest-watch

test-coverage:
	pytest tests/ --cov=app --cov-report=term-missing --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

test-unit:
	pytest tests/test_fsm.py tests/test_post_rules.py tests/test_channels.py \
	       tests/test_dedup.py tests/test_encryption.py tests/test_style_memory.py \
	       tests/test_rate_limiter.py -v --tb=short

test-integration:
	pytest tests/test_integration_gateway.py tests/test_scheduling.py -v --tb=short

# ── Code quality ───────────────────────────────────────────────────────────

lint:
	ruff check app/ tests/ scripts/
	@echo "✓ Lint passed"

format:
	ruff format app/ tests/ scripts/
	@echo "✓ Formatted"

format-check:
	ruff format --check app/ tests/ scripts/

typecheck:
	mypy app/ --ignore-missing-imports

check: lint format-check typecheck test
	@echo "✓ All checks passed"

# ── Deployment helpers ─────────────────────────────────────────────────────

webhook:
	python scripts/register_telegram_webhook.py

webhook-dry:
	python scripts/register_telegram_webhook.py --dry-run

migrate:
	@echo ""
	@echo "Manual step — run this SQL in Supabase Dashboard → SQL Editor:"
	@echo "  File: app/db/migrations/001_initial.sql"
	@echo ""
	@echo "  1. Open: https://app.supabase.com → your project → SQL Editor"
	@echo "  2. Click 'New query'"
	@echo "  3. Paste the contents of app/db/migrations/001_initial.sql"
	@echo "  4. Click 'Run'"
	@echo ""

# ── Management ─────────────────────────────────────────────────────────────

# Usage: make manage CMD="users list"
#        make manage CMD="posts list --status=scheduled"
#        make manage CMD="db stats"
manage:
	python scripts/manage.py $(CMD)

stats:
	python scripts/manage.py db stats

users:
	python scripts/manage.py users list

jobs:
	python scripts/manage.py jobs list

# ── Local dev helpers ──────────────────────────────────────────────────────

ngrok:
	@echo "Starting ngrok tunnel on port 8000..."
	@echo "After ngrok starts, copy the https URL and:"
	@echo "  1. Update OAUTH_CALLBACK_BASE_URL in .env"
	@echo "  2. Run: make webhook"
	ngrok http 8000

install-dev:
	pip install -e ".[dev]"

# ── Health checks ──────────────────────────────────────────────────────────

health:
	curl -s http://localhost:8000/health | python -m json.tool

health-wait:
	@echo "Waiting for API to be healthy..."
	@for i in $$(seq 1 30); do \
		if curl -sf http://localhost:8000/health > /dev/null 2>&1; then \
			echo "✓ API is healthy"; break; \
		fi; \
		echo "  Attempt $$i/30..."; sleep 2; \
	done

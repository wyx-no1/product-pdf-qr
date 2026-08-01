.PHONY: \
	build build-reproducible check-docs dev down format format-check \
	lint migration-downgrade migration-upgrade test test-integration test-unit typecheck

UV ?= uv

build:
	rm -rf build dist
	SOURCE_DATE_EPOCH=1754006400 $(UV) run python -m build --sdist --wheel

build-reproducible:
	./scripts/verify_reproducible_build.sh

typecheck:
	mkdir -p reports
	$(UV) run mypy

lint:
	mkdir -p reports
	$(UV) run ruff check --output-format junit --output-file reports/ruff.xml .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

test:
	mkdir -p reports
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml:reports/coverage.xml \
		--junitxml=reports/pytest.xml

test-unit:
	mkdir -p reports
	$(UV) run pytest -m "not integration" --cov --cov-report=term-missing \
		--cov-report=xml:reports/coverage-unit.xml --junitxml=reports/pytest-unit.xml

test-integration:
	mkdir -p reports
	$(UV) run pytest -m integration --junitxml=reports/pytest-integration.xml

migration-upgrade:
	DATABASE_URL="$(MIGRATION_DATABASE_URL)" $(UV) run alembic upgrade head

migration-downgrade:
	DATABASE_URL="$(MIGRATION_DATABASE_URL)" $(UV) run alembic downgrade base

check-docs:
	$(UV) run python scripts/check_markdown_links.py

dev:
	@test -f .env || cp .env.example .env
	docker compose up --build --wait

down:
	docker compose down

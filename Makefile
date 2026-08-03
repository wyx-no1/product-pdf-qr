.PHONY: \
	build build-reproducible check-docs dev down format format-check \
	lint migration-downgrade migration-upgrade test test-integration test-unit typecheck \
	verify-clean-start

UV ?= uv
CLEAN_START_ATTEMPTS ?= 3

build:
	rm -rf build dist
	SOURCE_DATE_EPOCH=1754006400 $(UV) run python -m build --sdist --wheel

build-reproducible:
	./scripts/verify_reproducible_build.sh

typecheck:
	mkdir -p reports
	$(UV) run mypy --config-file pyproject.toml

lint:
	mkdir -p reports
	$(UV) run ruff check --config pyproject.toml \
		--output-format junit --output-file reports/ruff.xml .
	$(UV) run ruff format --config pyproject.toml --check .

format:
	$(UV) run ruff check --config pyproject.toml --fix .
	$(UV) run ruff format --config pyproject.toml .

test:
	mkdir -p reports
	$(UV) run pytest -c pyproject.toml --cov --cov-config=pyproject.toml \
		--cov-report=term-missing --cov-report=xml:reports/coverage.xml \
		--junitxml=reports/pytest.xml

test-unit:
	mkdir -p reports
	$(UV) run pytest -c pyproject.toml -m "not integration" --cov \
		--cov-config=pyproject.toml --cov-report=term-missing \
		--cov-report=xml:reports/coverage-unit.xml --junitxml=reports/pytest-unit.xml

test-integration:
	mkdir -p reports
	$(UV) run pytest -c pyproject.toml -m integration \
		--junitxml=reports/pytest-integration.xml

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

verify-clean-start:
	@test -f .env || cp .env.example .env
	./scripts/verify_clean_start.sh "$(CLEAN_START_ATTEMPTS)"

.PHONY: \
	build build-reproducible check-docs dev down format format-check \
	lint migration-downgrade migration-upgrade test test-integration test-unit typecheck \
	prod-config-check prod-env-check verify-clean-start \
	backup-contract-check backup-config-check backup-image-build backup-image-reproducible \
	backup-image-scan backup-recovery-rehearsal deploy-rollback-rehearsal

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

prod-config-check:
	docker compose --env-file .env.prod.example -f compose.prod.yaml \
		config --format json | $(UV) run python scripts/production/validate_compose.py

prod-env-check:
	$(UV) run python scripts/production/validate_env_contract.py

backup-contract-check:
	$(UV) run python -m scripts.backup_recovery.cli \
		--contract deploy/backup/contract.json validate-contract

backup-config-check:
	SOURCE_COMMIT=0000000000000000000000000000000000000000 \
		docker compose \
		--env-file .env.prod.example \
		--env-file .env.backup.example \
		-f compose.prod.yaml -f compose.backup.yaml \
		--profile backup --profile restore --profile backup-volume-init \
		config --format json | \
		$(UV) run python scripts/backup_recovery/validate_compose.py

backup-image-build:
	SOURCE_DATE_EPOCH=1754006400 BUILDKIT_MULTI_PLATFORM=1 \
		docker build --pull --provenance=false --target backup-recovery-runtime \
		--tag product-pdf-qr-backup-recovery:local .

backup-image-reproducible:
	./scripts/backup_recovery/verify-reproducible-image.sh

backup-image-scan: backup-image-build
	docker run --rm --volume /var/run/docker.sock:/var/run/docker.sock \
		aquasec/trivy@sha256:cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f \
		image --exit-code 1 --ignore-unfixed=false --severity CRITICAL,HIGH \
		product-pdf-qr-backup-recovery:local

backup-recovery-rehearsal:
	./scripts/backup_recovery/rehearse-local.sh

deploy-rollback-rehearsal:
	./scripts/deploy_rollback/rehearse-local.sh

dev:
	@test -f .env || cp .env.example .env
	docker compose up --build --wait

down:
	docker compose down

verify-clean-start:
	@test -f .env || cp .env.example .env
	./scripts/verify_clean_start.sh "$(CLEAN_START_ATTEMPTS)"

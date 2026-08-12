#!/bin/sh
# Repeatable G-19 rehearsal through real PR2B wrappers and PR2A restore entrypoint.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
run_root="$(mktemp -d "${TMPDIR:-/tmp}/product-pdf-qr-pr2b.XXXXXXXX")"
case "$run_root" in
  */product-pdf-qr-pr2b.*) ;;
  *) echo "unsafe rehearsal temporary path" >&2; exit 2 ;;
esac
[ ! -L "$run_root" ] || {
  echo "rehearsal temporary path may not be a symlink" >&2
  exit 2
}
synthetic_project=""
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ -n "$synthetic_project" ]; then
    POSTGRES_HOST_PORT="$synthetic_port" docker compose \
      --env-file "$repository_root/.env.example" \
      --project-name "$synthetic_project" down --volumes >/dev/null 2>&1 || status=1
  fi
  rm -rf "$run_root" || status=1
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

if [ -z "${TEST_MIGRATION_DATABASE_URL:-}" ] ||
  [ -z "${TEST_BACKUP_DATABASE_URL:-}" ]; then
  synthetic_port="$(
    uv run python -c \
      'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
  )"
  synthetic_project="synthetic-pr2b-e2e-$$"
  POSTGRES_HOST_PORT="$synthetic_port" docker compose \
    --env-file "$repository_root/.env.example" \
    --project-name "$synthetic_project" up --detach --wait db
  TEST_MIGRATION_DATABASE_URL="postgresql+psycopg://app_migrate:local-migrate-only@127.0.0.1:${synthetic_port}/product_pdf_qr_test"
  TEST_BACKUP_DATABASE_URL="postgresql://app_backup:local-backup-only@127.0.0.1:${synthetic_port}/product_pdf_qr_test"
  export TEST_MIGRATION_DATABASE_URL TEST_BACKUP_DATABASE_URL
fi

started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
started_monotonic="$(
  uv run python -c 'import time; print(time.monotonic_ns())'
)"
printf 'G-19 local synthetic rehearsal started_at=%s\n' "$started_at"

printf '%s\n' \
  "command=docker compose [synthetic example env] PR2B watermark config validation"
(
  cd "$repository_root"
  SOURCE_COMMIT=0000000000000000000000000000000000000000 \
    docker compose \
    --env-file .env.prod.example \
    --env-file .env.backup.example \
    -f compose.prod.yaml -f compose.rollback.yaml \
    --profile rollback-watermark config --format json >/dev/null
)
printf 'compose_config_exit_code=0\n'

round=1
while [ "$round" -le 2 ]; do
  round_root="$run_root/round-$round"
  mkdir "$round_root"
  printf '%s\n' \
    "round=$round command=real publication/rollback/authorized-lossy/PR2A wrappers"
  (
    cd "$repository_root"
    uv run python -m scripts.deploy_rollback.rehearsal_e2e "$round_root"
  )
  printf 'round=%s exit_code=0\n' "$round"
  round=$((round + 1))
done

ended_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
ended_monotonic="$(
  uv run python -c 'import time; print(time.monotonic_ns())'
)"
elapsed_seconds=$(((ended_monotonic - started_monotonic) / 1000000000))
printf '%s\n' \
  "G-19 local synthetic rehearsal completed_at=$ended_at" \
  "elapsed_seconds=$elapsed_seconds" \
  "rto_limit_seconds=14400" \
  "real_production_access=0" \
  "g17_claimed=false" \
  "g18_claimed=false" \
  "exit_code=0"

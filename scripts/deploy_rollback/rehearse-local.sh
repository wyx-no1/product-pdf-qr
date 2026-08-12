#!/bin/sh
# Repeatable G-19 implementation rehearsal using only pytest synthetic adapters.

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
trap 'rm -rf "$run_root"' EXIT HUP INT TERM

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
  printf 'round=%s command=uv run pytest -q tests/unit/test_deploy_rollback.py\n' "$round"
  (
    cd "$repository_root"
    uv run pytest -q tests/unit/test_deploy_rollback.py \
      --basetemp "$run_root/round-$round"
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

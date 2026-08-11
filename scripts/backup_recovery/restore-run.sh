#!/bin/sh
# Authorized host recovery. Public proxy remains stopped through all validation.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
. "$repository_root/scripts/backup_recovery/lock.sh"

backup_id="${1:-}"
production_environment="$repository_root/.env.prod"
backup_environment="$repository_root/.env.backup"
production_compose="$repository_root/compose.prod.yaml"
backup_compose="$repository_root/compose.backup.yaml"

fail() {
  echo "restore refused: $1" >&2
  exit 2
}

check_private_file() {
  path="$1"
  [ -f "$path" ] && [ ! -L "$path" ] || {
    echo "restore refused: $path must be a regular non-symlink file" >&2
    exit 2
  }
  if permissions="$(stat -f '%Lp' "$path" 2>/dev/null)"; then
    owner="$(stat -f '%u' "$path")"
  else
    permissions="$(stat -c '%a' "$path")"
    owner="$(stat -c '%u' "$path")"
  fi
  [ "$permissions" = "600" ] && [ "$owner" = "$(id -u)" ] || {
    echo "restore refused: $path must be mode 0600 and owned by the operator" >&2
    exit 2
  }
}
check_private_file "$production_environment"
check_private_file "$backup_environment"
[ -z "$(git -C "$repository_root" status --porcelain --untracked-files=normal)" ] ||
  fail "deployment checkout must be clean before restore"
source_commit="$(git -C "$repository_root" rev-parse HEAD)"
case "$source_commit" in
  *[!0-9a-f]*)
    fail "deployment checkout commit is not a full Git SHA"
    ;;
esac
[ "${#source_commit}" -eq 40 ] || fail "deployment checkout commit is not a full Git SHA"

case "$backup_id" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z-[0-9a-f][0-9a-f]*)
    ;;
  *)
    echo "restore refused: a full valid backup_id is required" >&2
    exit 2
    ;;
esac
[ "${#backup_id}" -eq 49 ] || {
  echo "restore refused: invalid backup_id length" >&2
  exit 2
}

compose() {
  SOURCE_COMMIT="$source_commit" docker compose \
    --env-file "$production_environment" \
    --env-file "$backup_environment" \
    -f "$production_compose" \
    -f "$backup_compose" \
    "$@"
}

prod() {
  "$repository_root/scripts/production/prod-compose.sh" "$@"
}

assert_stopped() {
  service="$1"
  container_id="$(docker compose --env-file "$production_environment" \
    -f "$production_compose" ps -q "$service")"
  [ -n "$container_id" ] || {
    echo "restore refused: $service container missing" >&2
    exit 2
  }
  [ "$(docker inspect --format '{{.State.Running}}' "$container_id")" = "false" ] || {
    echo "restore refused: $service is not fully stopped" >&2
    exit 2
  }
}

wait_healthy() {
  service="$1"
  attempts=0
  while [ "$attempts" -lt 120 ]; do
    container_id="$(docker compose --env-file "$production_environment" \
      -f "$production_compose" ps -q "$service")"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
      "$container_id")"
    [ "$health" = "healthy" ] && return
    attempts=$((attempts + 1))
    sleep 1
  done
  return 1
}

service_initial_state() {
  service="$1"
  container_id="$(docker compose --env-file "$production_environment" \
    -f "$production_compose" ps -q "$service")"
  [ -n "$container_id" ] || fail "$service container missing"
  if [ "$(docker inspect --format '{{.State.Running}}' "$container_id")" = "false" ]; then
    printf 'stopped\n'
    return
  fi
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
    "$container_id")"
  if [ "$health" = "healthy" ]; then
    printf 'healthy\n'
  else
    printf 'running_unhealthy\n'
  fi
}

restore_pre_destructive_services() {
  recovery_failed=0
  if [ "$app_initial_state" = "healthy" ]; then
    if ! prod start app || ! wait_healthy app; then
      recovery_failed=1
    fi
  elif [ "$app_initial_state" = "running_unhealthy" ]; then
    recovery_failed=1
  fi
  if [ "$recovery_failed" = "0" ] &&
    [ "$proxy_initial_state" = "healthy" ] &&
    [ "$app_initial_state" = "healthy" ]; then
    if ! prod start proxy || ! wait_healthy proxy; then
      recovery_failed=1
    fi
  elif [ "$proxy_initial_state" != "stopped" ] &&
    { [ "$proxy_initial_state" != "healthy" ] ||
      [ "$app_initial_state" != "healthy" ]; }; then
    recovery_failed=1
  fi
  if [ "$recovery_failed" = "1" ]; then
    # A proxy that cannot become healthy must never remain published.
    prod stop --timeout 30 proxy >/dev/null 2>&1 || true
    "$repository_root/scripts/backup_recovery/emit-alert.sh" \
      "restore-pre-destructive-service-recovery-failed:$backup_id" || true
    return 1
  fi
}

restore_failure() {
  status=$?
  trap - EXIT HUP INT TERM
  if [ "$services_need_recovery" = "0" ]; then
    :
  elif [ "$destructive_restore_started" = "0" ] &&
    [ "$services_need_recovery" = "1" ]; then
    if ! restore_pre_destructive_services; then
      status=1
    fi
  else
    prod stop --timeout 30 proxy >/dev/null 2>&1 || true
    "$repository_root/scripts/backup_recovery/emit-alert.sh" \
      "restore-failed-public-remains-isolated:$backup_id" || status=1
  fi
  release_pr2a_lock || status=1
  exit "$status"
}

acquire_pr2a_lock
services_need_recovery=0
destructive_restore_started=0
app_initial_state=unknown
proxy_initial_state=unknown
trap restore_failure EXIT HUP INT TERM

# Persist the RTO clock before preflight. Retries with the same authorization
# reuse its declaration; a separately authorized restore of this backup gets
# an independent operation and cannot reuse completed destructive checkpoints.
compose --profile restore run --rm restore declare --backup-id "$backup_id"

# Authentication, all hashes, compatibility, and space are checked before any
# service is stopped or target DB/file write occurs.
compose --profile restore run --rm restore preflight --backup-id "$backup_id"

app_initial_state="$(service_initial_state app)"
proxy_initial_state="$(service_initial_state proxy)"
services_need_recovery=1
prod stop --timeout 60 proxy app
assert_stopped proxy
assert_stopped app

compose --profile restore run --rm restore retain-site --backup-id "$backup_id"
destructive_restore_started=1
compose --profile restore run --rm restore restore-database --backup-id "$backup_id"
compose --profile restore run --rm restore restore-files --backup-id "$backup_id"
compose --profile restore run --rm restore offline-validate --backup-id "$backup_id"

# Start only app for isolated checks. The public proxy remains fully stopped.
prod start app
wait_healthy app || fail "isolated app readiness timeout"
assert_stopped proxy
compose --profile restore run --rm restore record-functional-validation \
  --backup-id "$backup_id" --evidence /run/config/functional-evidence.json
compose --profile restore run --rm restore authorize-proxy --backup-id "$backup_id"

prod start proxy
wait_healthy proxy || fail "proxy readiness timeout"
external_url="$(sed -n 's/^RESTORE_EXTERNAL_READINESS_URL=//p' "$backup_environment")"
expected_sha256="$(sed -n 's/^RESTORE_EXTERNAL_EXPECTED_SHA256=//p' "$backup_environment")"
[ -n "$external_url" ] && [ -n "$expected_sha256" ] || {
  echo "restore failed: external readiness identity is missing" >&2
  exit 1
}
case "$expected_sha256" in
  *[!0-9a-f]*)
    echo "restore failed: external readiness SHA-256 is invalid" >&2
    exit 1
    ;;
esac
[ "${#expected_sha256}" -eq 64 ] || {
  echo "restore failed: external readiness SHA-256 length is invalid" >&2
  exit 1
}
observed_sha256="$(
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error "$external_url" |
    shasum -a 256 | awk '{print $1}'
)"
[ "$observed_sha256" = "$expected_sha256" ] || {
  echo "restore failed: external readiness content digest mismatch" >&2
  exit 1
}
ready_result="$(
  compose --profile restore run --rm restore external-ready --backup-id "$backup_id"
)"
elapsed="$(
  printf '%s\n' "$ready_result" |
    sed -n 's/.*"elapsed_seconds":\([0-9][0-9]*\).*/\1/p'
)"
[ -n "$elapsed" ] || fail "external readiness did not report persisted RTO elapsed time"
services_need_recovery=0
trap - EXIT HUP INT TERM
release_pr2a_lock
printf 'restore completed backup_id=%s elapsed_seconds=%s\n' "$backup_id" "$elapsed"

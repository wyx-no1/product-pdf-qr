#!/bin/sh
# Host orchestrator for read-only daytime precopy and the nightly stop window.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
. "$repository_root/scripts/backup_recovery/lock.sh"

production_environment="$repository_root/.env.prod"
backup_environment="$repository_root/.env.backup"
production_compose="$repository_root/compose.prod.yaml"
backup_compose="$repository_root/compose.backup.yaml"
mode="${1:-}"

fail() {
  echo "backup orchestrator refused: $1" >&2
  exit 2
}

check_private_file() {
  path="$1"
  [ -f "$path" ] || fail "$path is missing"
  [ ! -L "$path" ] || fail "$path may not be a symlink"
  if permissions="$(stat -f '%Lp' "$path" 2>/dev/null)"; then
    owner="$(stat -f '%u' "$path")"
  else
    permissions="$(stat -c '%a' "$path")"
    owner="$(stat -c '%u' "$path")"
  fi
  [ "$permissions" = "600" ] || fail "$path must have mode 0600"
  [ "$owner" = "$(id -u)" ] || fail "$path must be owned by the invoking user"
}

check_private_file "$production_environment"
check_private_file "$backup_environment"
[ -z "$(git -C "$repository_root" status --porcelain --untracked-files=normal)" ] ||
  fail "deployment checkout must be clean before backup"

compose() {
  SOURCE_COMMIT="$(git -C "$repository_root" rev-parse HEAD)" \
    docker compose \
    --env-file "$production_environment" \
    --env-file "$backup_environment" \
    -f "$production_compose" \
    -f "$backup_compose" \
    "$@"
}

app_container_id() {
  docker compose --env-file "$production_environment" -f "$production_compose" ps -q app
}

wait_for_app_stopped() {
  app_id="$(app_container_id)"
  [ -n "$app_id" ] || fail "stable app container is missing"
  attempts=0
  while [ "$attempts" -lt 60 ]; do
    running="$(docker inspect --format '{{.State.Running}}' "$app_id")"
    [ "$running" = "false" ] && return
    attempts=$((attempts + 1))
    sleep 1
  done
  fail "app did not reach fully stopped state"
}

assert_migrate_not_running() {
  running="$(docker compose --env-file "$production_environment" -f "$production_compose" \
    ps --status running --services migrate)"
  [ -z "$running" ] || fail "migrate is still running"
}

assert_database_network_members() {
  members="$(docker network inspect product_pdf_qr_database \
    --format '{{range .Containers}}{{.Name}} {{end}}')"
  for member in $members; do
    case "$member" in
      product-pdf-qr-prod-db-1 | product_pdf_qr_backup_job)
        ;;
      *)
        fail "undeclared database network member detected"
        ;;
    esac
  done
}

restore_app() {
  if ! "$repository_root/scripts/production/prod-compose.sh" start app; then
    ALERT_SEVERITY=critical "$repository_root/scripts/backup_recovery/emit-alert.sh" \
      "backup-app-restart-failed"
    return 1
  fi
  attempts=0
  while [ "$attempts" -lt 120 ]; do
    app_id="$(app_container_id)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$app_id")"
    [ "$health" = "healthy" ] && return
    [ "$(date +%s)" -lt "$window_deadline" ] || {
      ALERT_SEVERITY=critical "$repository_root/scripts/backup_recovery/emit-alert.sh" \
        "backup-stop-window-deadline-exceeded"
      return 1
    }
    attempts=$((attempts + 1))
    sleep 1
  done
  ALERT_SEVERITY=critical "$repository_root/scripts/backup_recovery/emit-alert.sh" \
    "backup-app-readiness-failed"
  return 1
}

run_finalizer_with_deadline() {
  compose --profile backup run --rm --name product_pdf_qr_backup_job backup finalize &
  finalizer_pid=$!
  while kill -0 "$finalizer_pid" 2>/dev/null; do
    elapsed=$(($(date +%s) - window_started))
    if [ "$elapsed" -ge 780 ]; then
      docker rm --force product_pdf_qr_backup_job >/dev/null 2>&1 || true
      wait "$finalizer_pid" || true
      fail "finalizer exceeded the 13-minute work budget"
    fi
    app_id="$(app_container_id)"
    [ "$(docker inspect --format '{{.State.Running}}' "$app_id")" = "false" ] || {
      docker rm --force product_pdf_qr_backup_job >/dev/null 2>&1 || true
      wait "$finalizer_pid" || true
      fail "app restarted during finalization"
    }
    assert_database_network_members
    if docker inspect product_pdf_qr_backup_job >/dev/null 2>&1; then
      if writer_output="$(docker exec --user 10002:10002 product_pdf_qr_backup_job \
        psql --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 \
        --command "SELECT count(*) FROM pg_stat_activity
          WHERE pid <> pg_backend_pid()
          AND (usename IN ('app_rw','app_migrate')
          OR (backend_xid IS NOT NULL AND usename <> 'app_backup'));" |
        tr -d '[:space:]')"; then
        writer_count="$writer_output"
      elif kill -0 "$finalizer_pid" 2>/dev/null; then
        docker rm --force product_pdf_qr_backup_job >/dev/null 2>&1 || true
        wait "$finalizer_pid" || true
        fail "database quiet monitor failed"
      else
        break
      fi
      if [ "$writer_count" != "0" ]; then
        docker rm --force product_pdf_qr_backup_job >/dev/null 2>&1 || true
        wait "$finalizer_pid" || true
        fail "database quiet condition was broken during finalization"
      fi
    fi
    sleep 1
  done
  wait "$finalizer_pid"
}

case "$mode" in
  precopy)
    acquire_pr2a_lock
    compose --profile backup run --rm --name product_pdf_qr_backup_job backup precopy
    ;;
  finalize)
    acquire_pr2a_lock
    window_started="$(date +%s)"
    window_deadline=$((window_started + 900))
    app_needs_recovery=0
    cleanup() {
      status=$?
      trap - EXIT HUP INT TERM
      docker rm --force product_pdf_qr_backup_job >/dev/null 2>&1 || true
      if [ "$app_needs_recovery" = "1" ]; then
        restore_app || status=1
      fi
      release_pr2a_lock || status=1
      exit "$status"
    }
    trap cleanup EXIT HUP INT TERM

    app_needs_recovery=1
    "$repository_root/scripts/production/prod-compose.sh" stop --timeout 60 app
    wait_for_app_stopped
    assert_migrate_not_running
    assert_database_network_members
    compose --profile backup run --rm --name product_pdf_qr_backup_job \
      backup assert-quiescent
    run_finalizer_with_deadline
    restore_app || fail "app failed to become healthy within the 15-minute stop window"
    app_needs_recovery=0
    elapsed=$(($(date +%s) - window_started))
    [ "$elapsed" -le 900 ] || fail "15-minute stop window exceeded"
    ;;
  *)
    fail "expected precopy or finalize"
    ;;
esac

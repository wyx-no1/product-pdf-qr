#!/bin/sh
set -eu

attempts="${1:-3}"
evidence_directory="${EVIDENCE_DIRECTORY:-reports/clean-start}"

case "$attempts" in
  "" | 0 | *[!0-9]*)
    echo "attempt count must be a positive integer" >&2
    exit 1
    ;;
esac

print_file() {
  line=""
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%s\n' "$line"
  done <"$1"
}

mkdir -p "$evidence_directory"

attempt=1
while [ "$attempt" -le "$attempts" ]; do
  evidence_file="$evidence_directory/attempt-$attempt.log"
  events_file="$evidence_directory/attempt-$attempt-events.log"

  docker compose down --volumes --remove-orphans
  started_at="$(date +%s)"

  if ! {
    printf 'clean-start attempt=%s\n' "$attempt"
    docker compose up --detach --wait
  } >"$evidence_file" 2>&1; then
    print_file "$evidence_file"
    exit 1
  fi

  finished_at="$(date +%s)"
  docker events \
    --since "$started_at" \
    --until "$((finished_at + 1))" \
    --filter type=container \
    --format '{{.TimeNano}}|{{.Action}}|{{.Actor.ID}}|{{index .Actor.Attributes "com.docker.compose.service"}}' \
    >"$events_file"

  db_container="$(docker compose ps --quiet db)"
  migrate_container="$(docker compose ps --all --quiet migrate)"

  db_healthy_at=""
  migrate_started_at=""
  while IFS="|" read -r event_time event_action event_container event_service; do
    if [ "$event_container" = "$db_container" ] &&
      [ "$event_service" = "db" ] &&
      [ "$event_action" = "health_status: healthy" ] &&
      [ -z "$db_healthy_at" ]; then
      db_healthy_at="$event_time"
    fi
    if [ "$event_container" = "$migrate_container" ] &&
      [ "$event_service" = "migrate" ] &&
      [ "$event_action" = "start" ] &&
      [ -z "$migrate_started_at" ]; then
      migrate_started_at="$event_time"
    fi
  done <"$events_file"

  db_health_status="$(
    docker inspect --format '{{.State.Health.Status}}' "$db_container"
  )"
  migrate_exit_code="$(
    docker inspect --format '{{.State.ExitCode}}' "$migrate_container"
  )"
  health_output="$(
    docker inspect \
      --format '{{range .State.Health.Log}}{{.Output}}{{end}}' \
      "$db_container"
  )"

  if [ -z "$db_healthy_at" ] || [ -z "$migrate_started_at" ]; then
    echo "missing database-health or migration-start event" >&2
    exit 1
  fi
  if [ "$db_healthy_at" -gt "$migrate_started_at" ]; then
    echo "migration started before the database became healthy" >&2
    exit 1
  fi
  if [ "$db_health_status" != "healthy" ]; then
    echo "database did not remain healthy" >&2
    exit 1
  fi
  if [ "$migrate_exit_code" != "0" ]; then
    echo "migration exited with status $migrate_exit_code" >&2
    exit 1
  fi
  case "$health_output" in
    *tcp-and-initialization-ready*) ;;
    *)
      echo "database health output did not prove initialization" >&2
      exit 1
      ;;
  esac

  {
    printf 'db_healthy_at_ns=%s\n' "$db_healthy_at"
    printf 'migrate_started_at_ns=%s\n' "$migrate_started_at"
    printf 'migrate_exit_code=%s\n' "$migrate_exit_code"
    printf 'health_output=%s\n' "$health_output"
    docker compose ps --all
    docker compose logs --no-color --timestamps db migrate
  } >>"$evidence_file" 2>&1

  printf 'clean-start attempt=%s\n' "$attempt"
  printf 'db_healthy_at_ns=%s\n' "$db_healthy_at"
  printf 'migrate_started_at_ns=%s\n' "$migrate_started_at"
  printf 'migrate_exit_code=%s\n' "$migrate_exit_code"
  printf 'health_output=%s\n' "$health_output"

  attempt=$((attempt + 1))
done

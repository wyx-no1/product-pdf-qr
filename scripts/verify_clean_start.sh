#!/usr/bin/env bash
set -Eeuo pipefail

attempts="${1:-3}"
evidence_directory="${EVIDENCE_DIRECTORY:-reports/clean-start}"

if [[ ! "$attempts" =~ ^[1-9][0-9]*$ ]]; then
  echo "attempt count must be a positive integer" >&2
  exit 1
fi

mkdir -p "$evidence_directory"

for ((attempt = 1; attempt <= attempts; attempt += 1)); do
  evidence_file="$evidence_directory/attempt-$attempt.log"
  events_file="$evidence_directory/attempt-$attempt-events.jsonl"

  docker compose down --volumes --remove-orphans
  started_at="$(date +%s)"

  {
    echo "clean-start attempt=$attempt"
    docker compose up --detach --wait
  } 2>&1 | tee "$evidence_file"

  finished_at="$(date +%s)"
  docker events \
    --since "$started_at" \
    --until "$((finished_at + 1))" \
    --filter type=container \
    --format '{{json .}}' >"$events_file"

  project_name="$(docker compose config --format json | jq -r '.name')"
  db_container="$(docker compose ps --quiet db)"
  migrate_container="$(docker compose ps --all --quiet migrate)"

  db_healthy_at="$(
    jq -rs --arg project "$project_name" '
      map(
        select((.Action // .status) == "health_status: healthy")
        | select(.Actor.Attributes["com.docker.compose.project"] == $project)
        | select(.Actor.Attributes["com.docker.compose.service"] == "db")
      )
      | first
      | .timeNano // empty
    ' "$events_file"
  )"
  migrate_started_at="$(
    jq -rs --arg project "$project_name" '
      map(
        select((.Action // .status) == "start")
        | select(.Actor.Attributes["com.docker.compose.project"] == $project)
        | select(.Actor.Attributes["com.docker.compose.service"] == "migrate")
      )
      | first
      | .timeNano // empty
    ' "$events_file"
  )"
  migrate_exit_code="$(
    docker inspect "$migrate_container" |
      jq -r '.[0].State.ExitCode'
  )"
  health_output="$(
    docker inspect "$db_container" |
      jq -r '.[0].State.Health.Log | map(select(.ExitCode == 0)) | first | .Output'
  )"

  if [[ -z "$db_healthy_at" || -z "$migrate_started_at" ]]; then
    echo "missing database-health or migration-start event" >&2
    exit 1
  fi
  if ((db_healthy_at > migrate_started_at)); then
    echo "migration started before the database became healthy" >&2
    exit 1
  fi
  if [[ "$migrate_exit_code" != "0" ]]; then
    echo "migration exited with status $migrate_exit_code" >&2
    exit 1
  fi
  if [[ "$health_output" != *"tcp-and-initialization-ready"* ]]; then
    echo "database health output did not prove initialization" >&2
    exit 1
  fi

  {
    echo "project=$project_name"
    echo "db_healthy_at_ns=$db_healthy_at"
    echo "migrate_started_at_ns=$migrate_started_at"
    echo "migrate_exit_code=$migrate_exit_code"
    echo "health_output=$health_output"
    docker compose ps --all
    docker compose logs --no-color --timestamps db migrate
  } 2>&1 | tee -a "$evidence_file"
done

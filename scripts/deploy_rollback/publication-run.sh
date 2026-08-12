#!/bin/sh
# Shared-lease publication state transitions; cutover is durable before public command.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$repository_root"
. "$repository_root/scripts/backup_recovery/lock.sh"

action="${1:-}"
release_id="${2:-}"
operation_id="${3:-}"
operator="${4:-}"

fail() {
  echo "publication transition refused: $1" >&2
  exit 2
}

case "$action" in
  prepare | migrated | isolated_validated | public_cutover) ;;
  *) fail "action must be prepare, migrated, isolated_validated, or public_cutover" ;;
esac
for value in "$release_id" "$operation_id" "$operator"; do
  case "$value" in
    *[!A-Za-z0-9._-]* | "") fail "bounded release/operation/operator values are required" ;;
  esac
done

: "${PR2B_RELEASE_STORE:?PR2B_RELEASE_STORE is required}"
: "${PR2B_PUBLICATION_STATE:?PR2B_PUBLICATION_STATE is required}"
: "${PR2B_WATERMARK_FILE:?PR2B_WATERMARK_FILE is required}"
: "${PR2B_ENVIRONMENT_MARKER:?PR2B_ENVIRONMENT_MARKER is required}"
: "${PR2B_ENVIRONMENT_CONFIRMATION:?PR2B_ENVIRONMENT_CONFIRMATION is required}"
: "${PR2B_PYTHON:=python3}"

for path in \
  "$PR2B_RELEASE_STORE" \
  "$PR2B_PUBLICATION_STATE" \
  "$PR2B_WATERMARK_FILE" \
  "$PR2B_ENVIRONMENT_MARKER"; do
  case "$path" in
    /*) ;;
    *) fail "every store/state/watermark/marker path must be absolute" ;;
  esac
  [ "$path" != "/" ] || fail "root is never a valid target"
done

cli() {
  "$PR2B_PYTHON" -m scripts.deploy_rollback.cli "$@"
}

run_argv() {
  variable_name="$1"
  case "$variable_name" in
    PR2B_MIGRATION_COMMAND_JSON) value="${PR2B_MIGRATION_COMMAND_JSON:-}" ;;
    PR2B_ISOLATED_VALIDATION_COMMAND_JSON)
      value="${PR2B_ISOLATED_VALIDATION_COMMAND_JSON:-}"
      ;;
    PR2B_PUBLICATION_COMMAND_JSON) value="${PR2B_PUBLICATION_COMMAND_JSON:-}" ;;
    *) fail "unknown bounded publication command" ;;
  esac
  [ -n "$value" ] || fail "$variable_name is required for $action"
  "$PR2B_PYTHON" -c '
import json
import subprocess
import sys

arguments = json.loads(sys.argv[1])
if (
    not isinstance(arguments, list)
    or not arguments
    or any(not isinstance(item, str) or not item for item in arguments)
):
    raise SystemExit("publication command must be a non-empty JSON argv array")
raise SystemExit(subprocess.run(arguments, check=False).returncode)
' "$value"
}

acquire_pr2a_lock
PR2B_LEASE_OWNER_PID=$$
export PR2B_LEASE_OWNER_PID
environment="$(
  cli validate-environment \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --operation-id "$operation_id" \
    --operator "$operator" \
    --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
    --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION"
)"
DOCKER_CONTEXT="$(
  printf '%s\n' "$environment" |
    "$PR2B_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["docker_context"])'
)"
export DOCKER_CONTEXT

case "$action" in
  prepare)
    cli prepare-publication \
      --store "$PR2B_RELEASE_STORE" \
      --release-id "$release_id" \
      --state "$PR2B_PUBLICATION_STATE" \
      --watermark "$PR2B_WATERMARK_FILE"
    ;;
  migrated)
    run_argv PR2B_MIGRATION_COMMAND_JSON
    cli advance-publication \
      --store "$PR2B_RELEASE_STORE" \
      --release-id "$release_id" \
      --state "$PR2B_PUBLICATION_STATE" \
      --stage migrated
    ;;
  isolated_validated)
    run_argv PR2B_ISOLATED_VALIDATION_COMMAND_JSON
    cli advance-publication \
      --store "$PR2B_RELEASE_STORE" \
      --release-id "$release_id" \
      --state "$PR2B_PUBLICATION_STATE" \
      --stage isolated_validated
    ;;
  public_cutover)
    # A failed public command leaves the irreversible marker in place, so rollback
    # conservatively preserves forward data even when the proxy never became ready.
    cli verify-artifacts \
      --store "$PR2B_RELEASE_STORE" \
      --release-id "$release_id" \
      --operation-id "$operation_id" \
      --operator "$operator" \
      --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
      --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
      --repository-root "$repository_root"
    cli advance-publication \
      --store "$PR2B_RELEASE_STORE" \
      --release-id "$release_id" \
      --state "$PR2B_PUBLICATION_STATE" \
      --stage public_cutover
    cli authorize-proxy-start \
      --store "$PR2B_RELEASE_STORE" \
      --release-id "$release_id" \
      --state "$PR2B_PUBLICATION_STATE"
    run_argv PR2B_PUBLICATION_COMMAND_JSON
    ;;
esac

release_pr2a_lock

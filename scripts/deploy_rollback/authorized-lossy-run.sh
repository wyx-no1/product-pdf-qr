#!/bin/sh
# Human-authorized G-19 data-loss path. Never called by automatic rollback.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$repository_root"
. "$repository_root/scripts/backup_recovery/lock.sh"
release_id="${1:-}"
operation_id="${2:-}"
operator="${3:-}"
authorization="${4:-}"
challenge_file="${5:-}"
onsite_retention_sha256="${6:-}"

fail() {
  echo "authorized data-loss rollback refused: $1" >&2
  exit 2
}

for value in "$release_id" "$operation_id" "$operator"; do
  case "$value" in
    *[!A-Za-z0-9._-]* | "") fail "bounded release/operation/operator values are required" ;;
  esac
done
case "$onsite_retention_sha256" in
  *[!0-9a-f]* | "") fail "onsite retention SHA-256 is required" ;;
esac
[ "${#onsite_retention_sha256}" -eq 64 ] || fail "onsite retention SHA-256 length is invalid"

: "${PR2B_RELEASE_STORE:?PR2B_RELEASE_STORE is required}"
: "${PR2B_PUBLICATION_STATE:?PR2B_PUBLICATION_STATE is required}"
: "${PR2B_RTO_STATE:?PR2B_RTO_STATE is required}"
: "${PR2B_AUDIT_LOG:?PR2B_AUDIT_LOG is required}"
: "${PR2B_ENVIRONMENT_MARKER:?PR2B_ENVIRONMENT_MARKER is required}"
: "${PR2B_ENVIRONMENT_CONFIRMATION:?PR2B_ENVIRONMENT_CONFIRMATION is required}"
: "${PR2B_ENVIRONMENT_ID:?PR2B_ENVIRONMENT_ID is required}"
: "${PR2B_USED_CHALLENGES:?PR2B_USED_CHALLENGES is required}"
: "${PR2B_STABLE_CHECKOUT:?PR2B_STABLE_CHECKOUT is required}"
: "${PR2B_WATERMARK_FILE:?PR2B_WATERMARK_FILE is required}"
: "${PR2B_PUBLICATION_FENCE_STATE:?PR2B_PUBLICATION_FENCE_STATE is required}"
: "${PR2B_PYTHON:=python3}"

for path in \
  "$authorization" \
  "$challenge_file" \
  "$PR2B_RELEASE_STORE" \
  "$PR2B_PUBLICATION_STATE" \
  "$PR2B_RTO_STATE" \
  "$PR2B_AUDIT_LOG" \
  "$PR2B_ENVIRONMENT_MARKER" \
  "$PR2B_USED_CHALLENGES" \
  "$PR2B_STABLE_CHECKOUT" \
  "$PR2B_PUBLICATION_FENCE_STATE" \
  "$PR2B_WATERMARK_FILE"; do
  case "$path" in
    /*) ;;
    *) fail "every authorization/state/checkout path must be absolute" ;;
  esac
  [ "$path" != "/" ] || fail "root is never a valid target"
done

cli() {
  "$PR2B_PYTHON" -m scripts.deploy_rollback.cli "$@"
}

fence() {
  cli "$1" \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --operation-id "$operation_id" \
    --operator "$operator" \
    --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
    --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
    --fence-state "$PR2B_PUBLICATION_FENCE_STATE"
}

# The original declaration is reused across the preceding decision wait and this
# continuation. It cannot reset merely because the chosen path changed.
cli declare-rollback \
  --store "$PR2B_RELEASE_STORE" \
  --release-id "$release_id" \
  --operation-id "$operation_id" \
  --rto-state "$PR2B_RTO_STATE" >/dev/null

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
docker_context="$(
  printf '%s\n' "$environment" |
    "$PR2B_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["docker_context"])'
)"
export DOCKER_CONTEXT="$docker_context"
cli verify-artifacts \
  --store "$PR2B_RELEASE_STORE" \
  --release-id "$release_id" \
  --operation-id "$operation_id" \
  --operator "$operator" \
  --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
  --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
  --repository-root "$repository_root" >/dev/null
cli verify-stable-checkout \
  --store "$PR2B_RELEASE_STORE" \
  --release-id "$release_id" \
  --checkout "$PR2B_STABLE_CHECKOUT" \
  --production-env "$PR2B_STABLE_CHECKOUT/.env.prod" \
  --backup-env "$PR2B_STABLE_CHECKOUT/.env.backup" >/dev/null
cli authorize-lossy-pr2a \
  --store "$PR2B_RELEASE_STORE" \
  --release-id "$release_id" \
  --operation-id "$operation_id" \
  --rto-state "$PR2B_RTO_STATE" \
  --authorization "$authorization" \
  --operator "$operator" \
  --environment-id "$PR2B_ENVIRONMENT_ID" \
  --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
  --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
  --challenge-file "$challenge_file" \
  --onsite-retention-sha256 "$onsite_retention_sha256" \
  --used-challenges "$PR2B_USED_CHALLENGES" \
  --audit "$PR2B_AUDIT_LOG" \
  --preflight-only >/dev/null
fence fence-engage >/dev/null
"$repository_root/scripts/production/prod-compose.sh" stop --timeout 60 proxy app
activate_stable_stopped_identity() {
  "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
    up --detach --no-deps --wait db certbot
  "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
    create --force-recreate --no-deps app proxy
}
activate_stable_stopped_identity
handoff="$(
  cli authorize-lossy-pr2a \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --operation-id "$operation_id" \
    --rto-state "$PR2B_RTO_STATE" \
    --authorization "$authorization" \
    --operator "$operator" \
    --environment-id "$PR2B_ENVIRONMENT_ID" \
    --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
    --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
    --challenge-file "$challenge_file" \
    --onsite-retention-sha256 "$onsite_retention_sha256" \
    --used-challenges "$PR2B_USED_CHALLENGES" \
    --audit "$PR2B_AUDIT_LOG"
)"
release_pr2a_lock
authorization_reference="$(
  "$PR2B_PYTHON" -c \
    'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["authorization_record"])' \
    "$authorization"
)"
backup_id="$(
  printf '%s\n' "$handoff" |
    "$PR2B_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["backup_id"])'
)"

# PR2A independently revalidates its environment/backup/operator/loss-window/
# authorization/expiry/challenge before its first destructive checkpoint.
"$PR2B_STABLE_CHECKOUT/scripts/backup_recovery/restore-run.sh" "$backup_id"

acquire_pr2a_lock
PR2B_LEASE_OWNER_PID=$$
export PR2B_LEASE_OWNER_PID
if ! fence fence-assert >/dev/null; then
  "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
    stop --timeout 60 proxy >/dev/null 2>&1 || true
  fail "publication fence continuity could not be proved"
fi
"$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" stop --timeout 60 proxy
post_restore_watermark="${PR2B_WATERMARK_FILE}.post-authorized-restore.${operation_id}.$$"
[ ! -e "$post_restore_watermark" ] ||
  fail "post-restore watermark target already exists"
cli capture-watermark \
  --repository-root "$PR2B_STABLE_CHECKOUT" \
  --output "$post_restore_watermark" >/dev/null
cli verify-pr2a-result \
  --store "$PR2B_RELEASE_STORE" \
  --release-id "$release_id" \
  --operation-id "$operation_id" \
  --rto-state "$PR2B_RTO_STATE" \
  --state "$PR2B_PUBLICATION_STATE" \
  --watermark "$post_restore_watermark" \
  --audit "$PR2B_AUDIT_LOG" \
  --operator "$operator" \
  --authorized-data-loss \
  --authorization-reference "$authorization_reference" >/dev/null
"$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" start proxy
if ! readiness="$(
  cli verify-external-readiness \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --operation-id "$operation_id" \
    --operator "$operator" \
    --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
    --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
    --repository-root "$PR2B_STABLE_CHECKOUT"
)"; then
  "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
    stop --timeout 60 proxy >/dev/null 2>&1 || true
  fail "post-restore external readiness failed; proxy re-isolated"
fi
if ! publication="$(fence fence-publish)"; then
  "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
    stop --timeout 60 proxy >/dev/null 2>&1 || true
  fail "atomic publication fence release failed; proxy re-isolated"
fi
external_ready_at="$(
  printf '%s\n' "$publication" |
    "$PR2B_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["published_at"])'
)"
cli complete-pr2a \
  --store "$PR2B_RELEASE_STORE" \
  --release-id "$release_id" \
  --operation-id "$operation_id" \
  --rto-state "$PR2B_RTO_STATE" \
  --state "$PR2B_PUBLICATION_STATE" \
  --watermark "$post_restore_watermark" \
  --external-ready-at "$external_ready_at" \
  --audit "$PR2B_AUDIT_LOG" \
  --operator "$operator" \
  --authorized-data-loss \
  --authorization-reference "$authorization_reference"
release_pr2a_lock

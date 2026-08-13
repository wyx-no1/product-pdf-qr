#!/bin/sh
# G-19 rollback entrypoint.  Destructive work is delegated only to unchanged PR2A.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$repository_root"
. "$repository_root/scripts/backup_recovery/lock.sh"

release_id="${1:-}"
operation_id="${2:-}"
operator="${3:-}"

fail() {
  echo "release rollback refused: $1" >&2
  exit 2
}

case "$release_id" in
  *[!A-Za-z0-9._-]* | "") fail "a bounded release_id is required" ;;
esac
case "$operation_id" in
  *[!A-Za-z0-9._-]* | "") fail "a bounded operation_id is required" ;;
esac
case "$operator" in
  *[!A-Za-z0-9._-]* | "") fail "a bounded operator is required" ;;
esac

: "${PR2B_RELEASE_STORE:?PR2B_RELEASE_STORE is required}"
: "${PR2B_PUBLICATION_STATE:?PR2B_PUBLICATION_STATE is required}"
: "${PR2B_WATERMARK_FILE:?PR2B_WATERMARK_FILE is required}"
: "${PR2B_RTO_STATE:?PR2B_RTO_STATE is required}"
: "${PR2B_AUDIT_LOG:?PR2B_AUDIT_LOG is required}"
: "${PR2B_RUNTIME_IDENTITY:?PR2B_RUNTIME_IDENTITY is required}"
: "${PR2B_RESULT:?PR2B_RESULT is required}"
: "${PR2B_ENVIRONMENT_MARKER:?PR2B_ENVIRONMENT_MARKER is required}"
: "${PR2B_ENVIRONMENT_CONFIRMATION:?PR2B_ENVIRONMENT_CONFIRMATION is required}"
: "${PR2B_PUBLICATION_FENCE_STATE:?PR2B_PUBLICATION_FENCE_STATE is required}"
: "${PR2B_PROXY_CONTINUOUSLY_ISOLATED:?yes or no is required}"
: "${PR2B_PYTHON:=python3}"

for path in \
  "$PR2B_RELEASE_STORE" \
  "$PR2B_PUBLICATION_STATE" \
  "$PR2B_WATERMARK_FILE" \
  "$PR2B_RTO_STATE" \
  "$PR2B_AUDIT_LOG" \
  "$PR2B_RUNTIME_IDENTITY" \
  "$PR2B_RESULT" \
  "$PR2B_ENVIRONMENT_MARKER" \
  "$PR2B_PUBLICATION_FENCE_STATE" \
  "$repository_root"; do
  case "$path" in
    /*) ;;
    *) fail "every persistent/checkout path must be absolute" ;;
  esac
  [ "$path" != "/" ] || fail "root is never a valid target"
done

[ "$PR2B_PROXY_CONTINUOUSLY_ISOLATED" = "yes" ] ||
  [ "$PR2B_PROXY_CONTINUOUSLY_ISOLATED" = "no" ] ||
  fail "proxy isolation evidence must be yes or no"

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

# Persist the single RTO start before lock waits, artifact checks, or decisions.
cli declare-rollback \
  --store "$PR2B_RELEASE_STORE" \
  --release-id "$release_id" \
  --operation-id "$operation_id" \
  --rto-state "$PR2B_RTO_STATE" >/dev/null

inspection="$(
  cli inspect-action \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --operation-id "$operation_id" \
    --rto-state "$PR2B_RTO_STATE" \
    --state "$PR2B_PUBLICATION_STATE" \
    --watermark "$PR2B_WATERMARK_FILE" \
    --proxy-continuously-isolated "$PR2B_PROXY_CONTINUOUSLY_ISOLATED"
)"
action="$(
  printf '%s\n' "$inspection" |
    "$PR2B_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["action"])'
)"

execute() {
  expected_action="$1"
  execution_watermark="$2"
  execution_isolation="$3"
  execution_repository="${4:-$repository_root}"
  cli execute \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --operation-id "$operation_id" \
    --rto-state "$PR2B_RTO_STATE" \
    --state "$PR2B_PUBLICATION_STATE" \
    --watermark "$execution_watermark" \
    --proxy-continuously-isolated "$execution_isolation" \
    --operator "$operator" \
    --audit "$PR2B_AUDIT_LOG" \
    --runtime-identity "$PR2B_RUNTIME_IDENTITY" \
    --repository-root "$execution_repository" \
    --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
    --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
    --expected-action "$expected_action" \
    --result "$PR2B_RESULT"
}

set_operation_context() {
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
}

verify_exact_artifacts() {
  cli verify-artifacts \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --operation-id "$operation_id" \
    --operator "$operator" \
    --environment-marker "$PR2B_ENVIRONMENT_MARKER" \
    --environment-confirmation "$PR2B_ENVIRONMENT_CONFIRMATION" \
    --repository-root "$repository_root" >/dev/null
}

require_stable_checkout() {
  : "${PR2B_STABLE_CHECKOUT:?PR2B_STABLE_CHECKOUT is required for stable identity activation}"
  case "$PR2B_STABLE_CHECKOUT" in
    /*) ;;
    *) fail "stable checkout must be absolute" ;;
  esac
  [ "$PR2B_STABLE_CHECKOUT" != "/" ] || fail "root is not a stable checkout"
  cli verify-stable-checkout \
    --store "$PR2B_RELEASE_STORE" \
    --release-id "$release_id" \
    --checkout "$PR2B_STABLE_CHECKOUT" \
    --production-env "$PR2B_STABLE_CHECKOUT/.env.prod" \
    --backup-env "$PR2B_STABLE_CHECKOUT/.env.backup" >/dev/null
}

activate_stable_stopped_identity() {
  "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
    up --detach --no-deps --wait db certbot
  "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
    create --force-recreate --no-deps app proxy
}

case "$action" in
  NEEDS_ROLLBACK_DECISION)
    # execute records the non-zero decision node and makes no service/data call.
    acquire_pr2a_lock
    PR2B_LEASE_OWNER_PID=$$
    export PR2B_LEASE_OWNER_PID
    execute "$action" "$PR2B_WATERMARK_FILE" "$PR2B_PROXY_CONTINUOUSLY_ISOLATED"
    fail "NEEDS_ROLLBACK_DECISION unexpectedly returned success"
    ;;
  APP_ONLY_SWITCH)
    # Compatible path two consumes the exact PR2A owner lease.
    acquire_pr2a_lock
    PR2B_LEASE_OWNER_PID=$$
    export PR2B_LEASE_OWNER_PID
    execute "$action" "$PR2B_WATERMARK_FILE" "$PR2B_PROXY_CONTINUOUSLY_ISOLATED"
    release_pr2a_lock
    ;;
  INVOKE_UNMODIFIED_PR2A_RESTORE)
    # Freeze app/proxy and recheck the full watermark under the shared lease.
    # restore-run.sh then owns that lease for its complete restore state machine.
    acquire_pr2a_lock
    PR2B_LEASE_OWNER_PID=$$
    export PR2B_LEASE_OWNER_PID
    execute "$action" "$PR2B_WATERMARK_FILE" "$PR2B_PROXY_CONTINUOUSLY_ISOLATED" \
      >/dev/null
    set_operation_context
    verify_exact_artifacts
    require_stable_checkout
    "$repository_root/scripts/production/prod-compose.sh" stop --timeout 60 proxy app
    frozen_watermark="${PR2B_WATERMARK_FILE}.pre-public-freeze"
    cli capture-watermark \
      --repository-root "$repository_root" \
      --output "$frozen_watermark" >/dev/null
    frozen_inspection="$(
      cli inspect-action \
        --store "$PR2B_RELEASE_STORE" \
        --release-id "$release_id" \
        --operation-id "$operation_id" \
        --rto-state "$PR2B_RTO_STATE" \
        --state "$PR2B_PUBLICATION_STATE" \
        --watermark "$frozen_watermark" \
        --proxy-continuously-isolated yes
    )"
    frozen_action="$(
      printf '%s\n' "$frozen_inspection" |
        "$PR2B_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["action"])'
    )"
    if [ "$frozen_action" != "INVOKE_UNMODIFIED_PR2A_RESTORE" ]; then
      if [ "$frozen_action" = "APP_ONLY_SWITCH" ]; then
        execute "$frozen_action" "$frozen_watermark" yes
        release_pr2a_lock
        exit 0
      fi
      release_pr2a_lock
      execute "$frozen_action" "$frozen_watermark" yes
      fail "NEEDS_ROLLBACK_DECISION unexpectedly returned success after freeze"
    fi
    # The shared lease and stopped app/proxy made the frozen decision atomic.
    # Engage the persistent fence only after path one remains proven, so a
    # last-write fallback to app-only cannot return success behind a stale fence.
    fence fence-engage >/dev/null
    # app/proxy are now stopped and cannot change the W0 watermark. Release the
    # preparation lease so the unchanged PR2A restore can own it without nesting.
    activate_stable_stopped_identity
    release_pr2a_lock
    backup_id="$(
      "$PR2B_PYTHON" -c \
        'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["backup_id"])' \
        "$PR2B_RESULT"
    )"
    "$PR2B_STABLE_CHECKOUT/scripts/backup_recovery/restore-run.sh" "$backup_id"
    acquire_pr2a_lock
    PR2B_LEASE_OWNER_PID=$$
    export PR2B_LEASE_OWNER_PID
    if ! fence fence-assert >/dev/null; then
      "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" \
        stop --timeout 60 proxy >/dev/null 2>&1 || true
      fail "publication fence continuity could not be proved"
    fi
    # PR2A has completed its own state machine. Re-isolate before accepting the
    # result, then measure the actual restored DB/files/audit rather than W0 input.
    "$PR2B_STABLE_CHECKOUT/scripts/production/prod-compose.sh" stop --timeout 60 proxy
    post_restore_watermark="${PR2B_WATERMARK_FILE}.post-restore.${operation_id}.$$"
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
      --operator "$operator" >/dev/null
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
      --operator "$operator"
    release_pr2a_lock
    ;;
  *)
    fail "unknown rollback action"
    ;;
esac

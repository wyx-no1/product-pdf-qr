#!/bin/sh
# Create the immediate PR2A recovery point that every release record must bind.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$repository_root"
. "$repository_root/scripts/backup_recovery/lock.sh"
evidence_path="${1:-}"
watermark_path="${2:-}"

fail() {
  echo "pre-release backup refused: $1" >&2
  exit 2
}

case "$evidence_path" in
  /*) ;;
  *) fail "an absolute evidence transcript path is required" ;;
esac
case "$watermark_path" in
  /*) ;;
  *) fail "an absolute W0 watermark path is required" ;;
esac
[ "$evidence_path" != "/" ] || fail "root is not an evidence target"
[ "$watermark_path" != "/" ] || fail "root is not a watermark target"
[ ! -L "$evidence_path" ] || fail "evidence transcript may not be a symlink"
[ ! -L "$watermark_path" ] || fail "watermark may not be a symlink"
[ ! -e "$evidence_path" ] || fail "evidence transcript is append-only; choose a new path"
[ ! -e "$watermark_path" ] || fail "watermark is immutable; choose a new path"

evidence_parent="$(dirname -- "$evidence_path")"
[ -d "$evidence_parent" ] || fail "evidence parent must already exist"
watermark_parent="$(dirname -- "$watermark_path")"
[ -d "$watermark_parent" ] || fail "watermark parent must already exist"
: "${PR2B_PYTHON:=python3}"

umask 077
started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
{
  printf 'command=%s\n' "$repository_root/scripts/production/prod-compose.sh stop proxy"
  printf 'command=%s\n' "$repository_root/scripts/backup_recovery/backup-run.sh precopy"
  printf 'command=%s\n' "$repository_root/scripts/backup_recovery/backup-run.sh finalize"
  printf '%s\n' \
    "command=docker compose [PR1+PR2B] --profile rollback-watermark run app_backup watermark"
  printf 'started_at=%s\n' "$started_at"
} >"$evidence_path"

# The public entrypoint remains isolated from before B0 until publication-run
# writes public_cutover. This prevents a valid but stale B0/W0 gap.
acquire_pr2a_lock
"$repository_root/scripts/production/prod-compose.sh" stop --timeout 60 proxy
release_pr2a_lock

# Use a shell with pipefail solely for transcript fidelity. The called PR2A scripts
# remain the merged, unchanged implementations and each acquires their shared lease.
if command -v bash >/dev/null 2>&1; then
  bash -o pipefail -c '
    "$1/scripts/backup_recovery/backup-run.sh" precopy 2>&1 | tee -a "$2"
    "$1/scripts/backup_recovery/backup-run.sh" finalize 2>&1 | tee -a "$2"
  ' _ "$repository_root" "$evidence_path"
else
  fail "bash with pipefail is required to preserve exact backup exit status"
fi

"$repository_root/scripts/deploy_rollback/capture-watermark.sh" >"$watermark_path"
chmod 0600 "$watermark_path"
watermark_sha256="$(
  "$PR2B_PYTHON" -c \
    'import json,sys; from scripts.deploy_rollback.model import validate_watermark; print(validate_watermark(json.load(open(sys.argv[1], encoding="utf-8"))))' \
    "$watermark_path"
)"
completed_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
printf 'completed_at=%s\ng19_watermark_sha256=%s\nexit_code=0\n' \
  "$completed_at" "$watermark_sha256" >>"$evidence_path"
chmod 0600 "$evidence_path"
printf '%s\n' \
  "immediate PR2A backup completed with proxy isolated" \
  "bind completion-last backup_id and g19_watermark_sha256 from $evidence_path" \
  "W0=$watermark_path"

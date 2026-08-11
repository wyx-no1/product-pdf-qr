#!/bin/sh
# Build the backup/recovery image twice from scratch and compare OCI archives.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
temporary_root="$(mktemp -d "${TMPDIR:-/tmp}/product-pdf-qr-backup-image.XXXXXX")"

cleanup() {
  case "$temporary_root" in
    "${TMPDIR:-/tmp}"/product-pdf-qr-backup-image.*)
      rm -rf "$temporary_root"
      ;;
  esac
}
trap cleanup EXIT HUP INT TERM

docker_command() {
  if [ -n "${PR2A_DOCKER_CONTEXT:-}" ]; then
    docker --context "$PR2A_DOCKER_CONTEXT" "$@"
  else
    docker "$@"
  fi
}

build_once() {
  destination="$1"
  SOURCE_DATE_EPOCH=1754006400 BUILDKIT_MULTI_PLATFORM=1 \
    docker_command buildx build \
    --pull \
    --no-cache \
    --provenance=false \
    --target backup-recovery-runtime \
    --output "type=oci,dest=$destination,rewrite-timestamp=true" \
    "$repository_root"
}

build_once "$temporary_root/first.tar"
build_once "$temporary_root/second.tar"
shasum -a 256 "$temporary_root/first.tar" | awk '{print $1}' >"$temporary_root/first.sha256"
shasum -a 256 "$temporary_root/second.tar" | awk '{print $1}' >"$temporary_root/second.sha256"
diff -u "$temporary_root/first.sha256" "$temporary_root/second.sha256"
printf 'backup_recovery_oci_sha256=%s\n' "$(sed -n '1p' "$temporary_root/first.sha256")"

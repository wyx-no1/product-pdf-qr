#!/bin/sh
# Shared lease lock for precopy, finalizer, restore, and authorized retention.

set -eu

lock_directory="${PR2A_LOCK_DIRECTORY:-/run/lock/product-pdf-qr-pr2a}"

acquire_pr2a_lock() {
  if mkdir "$lock_directory" 2>/dev/null; then
    printf '%s\n' "$$" >"$lock_directory/owner"
    trap 'release_pr2a_lock' EXIT HUP INT TERM
    return
  fi

  owner=""
  if [ -f "$lock_directory/owner" ]; then
    owner="$(sed -n '1p' "$lock_directory/owner")"
  fi
  case "$owner" in
    *[!0-9]* | "")
      echo "PR2A lock has an invalid owner; operator inspection required" >&2
      exit 75
      ;;
  esac
  if kill -0 "$owner" 2>/dev/null; then
    echo "another PR2A operation owns the run lock" >&2
    exit 75
  fi
  # A dead owner is recoverable only by an explicit operator action. This avoids
  # deleting a live lock after PID namespace or host ambiguity.
  echo "PR2A lock owner is stale; remove the exact lock directory after inspection" >&2
  exit 75
}

release_pr2a_lock() {
  if [ -f "$lock_directory/owner" ] &&
    [ "$(sed -n '1p' "$lock_directory/owner")" = "$$" ]; then
    rm "$lock_directory/owner"
    rmdir "$lock_directory"
  fi
}

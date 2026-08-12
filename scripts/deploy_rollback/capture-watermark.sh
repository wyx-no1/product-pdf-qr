#!/bin/sh
# Emit one read-only full watermark through the isolated PR2B Compose profile.

set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
cd "$repository_root"

fail() {
  echo "watermark capture refused: $1" >&2
  exit 2
}

for environment_file in "$repository_root/.env.prod" "$repository_root/.env.backup"; do
  [ -f "$environment_file" ] && [ ! -L "$environment_file" ] ||
    fail "$environment_file must be a regular non-symlink file"
  if mode="$(stat -f '%Lp' "$environment_file" 2>/dev/null)"; then
    owner="$(stat -f '%u' "$environment_file")"
  else
    mode="$(stat -c '%a' "$environment_file")"
    owner="$(stat -c '%u' "$environment_file")"
  fi
  [ "$mode" = "600" ] && [ "$owner" = "$(id -u)" ] ||
    fail "$environment_file must be mode 0600 and operator-owned"
done

[ -z "$(git status --porcelain --untracked-files=normal)" ] ||
  fail "watermark checkout must be clean"

SOURCE_COMMIT="$(git rev-parse HEAD)" \
  docker compose \
  --env-file "$repository_root/.env.prod" \
  --env-file "$repository_root/.env.backup" \
  -f "$repository_root/compose.prod.yaml" \
  -f "$repository_root/compose.rollback.yaml" \
  --profile rollback-watermark run --rm --no-deps rollback-watermark

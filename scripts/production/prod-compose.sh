#!/bin/sh
set -eu

repository_root="$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
environment_file="$repository_root/.env.prod"
compose_file="$repository_root/compose.prod.yaml"

fail() {
  echo "production preflight failed: $1" >&2
  exit 1
}

[ -f "$environment_file" ] || fail ".env.prod is missing or is not a regular file"
[ ! -L "$environment_file" ] || fail ".env.prod must not be a symbolic link"

if mode="$(stat -f '%Lp' "$environment_file" 2>/dev/null)"; then
  owner="$(stat -f '%u' "$environment_file")"
else
  mode="$(stat -c '%a' "$environment_file")"
  owner="$(stat -c '%u' "$environment_file")"
fi

[ "$mode" = "600" ] || fail ".env.prod permissions must be 0600"
[ "$owner" = "$(id -u)" ] || fail ".env.prod must be owned by the invoking user"

compose() {
  docker compose --env-file "$environment_file" -f "$compose_file" "$@"
}

compose config --quiet

case "${1:-}" in
  up | start | restart)
    app_image="$(sed -n 's/^APP_IMAGE=//p' "$environment_file")"
    [ -n "$app_image" ] || fail "APP_IMAGE is missing"
    [ "$(printf '%s\n' "$app_image" | wc -l | tr -d ' ')" = "1" ] \
      || fail "APP_IMAGE must be declared exactly once"
    docker run --rm \
      --network none \
      --read-only \
      --cap-drop ALL \
      --security-opt no-new-privileges:true \
      --env-file "$environment_file" \
      --entrypoint python \
      "$app_image" \
      -c 'from product_pdf_qr.config import get_settings; get_settings(); print("production configuration valid")'
    ;;
esac

exec docker compose --env-file "$environment_file" -f "$compose_file" "$@"

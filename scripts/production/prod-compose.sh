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

compose config --format json |
  python3 "$repository_root/scripts/production/validate_compose.py" >/dev/null

ensure_bootstrap_certificate() {
  compose up --detach certbot
  if ! compose exec --no-TTY certbot sh -eu -c \
    'test -s /tmp/active/fullchain.pem && test -s /tmp/active/privkey.pem'; then
    PRODUCTION_CERTIFICATE_BOOTSTRAP=1 \
      "$repository_root/scripts/production/bootstrap-certificate.sh"
  fi
}

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
      --env DEPLOYMENT_MODE=production \
      --env APP_BIND_HOST=172.30.0.20 \
      --env FORWARDED_ALLOW_IPS=172.30.0.10 \
      --entrypoint python \
      "$app_image" \
      -c 'from product_pdf_qr.config import get_settings; get_settings(); print("production configuration valid")'
    if [ "${PRODUCTION_CERTIFICATE_BOOTSTRAP:-0}" != "1" ]; then
      ensure_bootstrap_certificate
    fi
    ;;
esac

exec docker compose --env-file "$environment_file" -f "$compose_file" "$@"

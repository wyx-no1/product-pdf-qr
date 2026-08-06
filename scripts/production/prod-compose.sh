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

validator_image="python:3.12.13-alpine3.24@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df"
compose config --format json |
  docker run --rm --interactive \
    --pull missing \
    --network none \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --user 65534:65534 \
    --volume "$repository_root/scripts/production/validate_compose.py:/validate_compose.py:ro" \
    --entrypoint python \
    "$validator_image" \
    /validate_compose.py >/dev/null

ensure_bootstrap_certificate() {
  compose up --detach certbot
  if compose exec --no-TTY certbot sh -eu -c \
    'test ! -e /tmp/active && test ! -L /tmp/active'; then
    PRODUCTION_CERTIFICATE_BOOTSTRAP=1 \
      "$repository_root/scripts/production/bootstrap-certificate.sh"
  fi
  if ! compose exec --no-TTY certbot sh -eu -c '
    certificate_path="/tmp/active/fullchain.pem"
    private_key_path="/tmp/active/privkey.pem"
    test -s "$certificate_path"
    test -s "$private_key_path"
    openssl x509 -in "$certificate_path" -noout -checkend 86400
    openssl x509 -in "$certificate_path" -noout -checkhost "$PUBLIC_DOMAIN"
    certificate_key="$(
      openssl x509 -in "$certificate_path" -pubkey -noout |
        openssl pkey -pubin -outform DER 2>/dev/null |
        openssl sha256
    )"
    private_key="$(
      openssl pkey -in "$private_key_path" -pubout -outform DER 2>/dev/null |
        openssl sha256
    )"
    test "$certificate_key" = "$private_key"
  '; then
    fail "active certificate is invalid; repair it or run bootstrap-certificate.sh explicitly"
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

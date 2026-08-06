#!/bin/sh
set -eu

script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$script_directory/prod-compose.sh"

"$compose" exec --no-TTY certbot sh -eu -c '
  insecure_option=""
  case "$ACME_CA_DIRECTORY" in
    https://pebble:* | https://pebble/*)
      insecure_option="--no-verify-ssl"
      ;;
  esac
  # The value is selected only from the fixed option above, never from input.
  # shellcheck disable=SC2086
  exec certbot certonly \
    --config-dir /tmp/letsencrypt \
    --work-dir /run/certbot/work \
    --logs-dir /run/certbot/logs \
    --server "$ACME_CA_DIRECTORY" \
    $insecure_option \
    --non-interactive \
    --agree-tos \
    --no-eff-email \
    --email "$ACME_EMAIL" \
    --preferred-challenges http \
    --webroot \
    --webroot-path /var/tmp \
    --cert-name "$PUBLIC_DOMAIN" \
    --domain "$PUBLIC_DOMAIN"
'

"$script_directory/activate-certificate.sh"
if ! "$compose" exec --no-TTY proxy nginx -t; then
  "$script_directory/rollback-certificate.sh"
  exit 1
fi
"$compose" exec --no-TTY proxy nginx -s reload

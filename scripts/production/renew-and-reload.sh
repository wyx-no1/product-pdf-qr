#!/bin/sh
set -eu

script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$script_directory/prod-compose.sh"
activation_started=0
renew_extra=""

case "${1:-}" in
  "")
    ;;
  --force-synthetic-renewal)
    "$compose" exec --no-TTY certbot sh -eu -c '
      case "$ACME_CA_DIRECTORY" in
        https://pebble:* | https://pebble/*) ;;
        *) exit 1 ;;
      esac
    '
    renew_extra="--force-renewal --no-verify-ssl"
    ;;
  *)
    echo "unsupported renewal option" >&2
    exit 2
    ;;
esac

send_failure_alert() {
  "$compose" exec --no-TTY certbot python -c '
import json
import os
import urllib.request

payload = json.dumps(
    {"event": "product-pdf-qr certificate renewal failed", "action": "operator review required"}
).encode()
request = urllib.request.Request(
    os.environ["ACME_ALERT_WEBHOOK_URL"],
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
urllib.request.urlopen(request, timeout=10).close()
' >/dev/null 2>&1 || echo "certificate renewal failed; alert delivery also failed" >&2
}

on_exit() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    if [ "$activation_started" -eq 1 ]; then
      "$script_directory/rollback-certificate.sh" >/dev/null 2>&1 || true
    fi
    send_failure_alert
  fi
  exit "$status"
}
trap on_exit EXIT

"$compose" exec --no-TTY certbot rm -f /run/certbot/renewed
# renew_extra is either empty or the fixed local-Pebble flag selected above.
# shellcheck disable=SC2086
"$compose" exec --no-TTY certbot certbot renew \
  --config-dir /tmp/letsencrypt \
  --work-dir /run/certbot/work \
  --logs-dir /run/certbot/logs \
  --no-random-sleep-on-renew \
  --deploy-hook "touch /run/certbot/renewed" \
  $renew_extra

if ! "$compose" exec --no-TTY certbot test -f /run/certbot/renewed; then
  echo "certificate is not due; proxy reload skipped"
  exit 0
fi

activation_started=1
"$script_directory/activate-certificate.sh"
"$compose" exec --no-TTY proxy nginx -t
"$compose" exec --no-TTY proxy nginx -s reload
activation_started=0

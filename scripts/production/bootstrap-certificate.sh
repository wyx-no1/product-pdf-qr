#!/bin/sh
set -eu

script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$script_directory/prod-compose.sh"

if ! "$compose" ps --status running --services | grep -qx certbot; then
  PRODUCTION_CERTIFICATE_BOOTSTRAP=1 "$compose" up --detach certbot
fi
"$compose" exec --no-TTY certbot sh -eu -c '
  stage="/run/certbot/bootstrap.$$"
  rm -rf "$stage"
  mkdir -p "$stage"
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
    -subj "/CN=$PUBLIC_DOMAIN" \
    -addext "subjectAltName=DNS:$PUBLIC_DOMAIN" \
    -keyout "$stage/privkey.pem" \
    -out "$stage/fullchain.pem" >/dev/null 2>&1
  chgrp 101 "$stage/privkey.pem" "$stage/fullchain.pem"
  chmod 0440 "$stage/privkey.pem"
  chmod 0444 "$stage/fullchain.pem"
  chmod 0750 "$stage"
  rm -rf /tmp/active.previous
  if [ -d /tmp/active ]; then
    mv /tmp/active /tmp/active.previous
  fi
  mv "$stage" /tmp/active
  echo "synthetic bootstrap certificate installed"
'

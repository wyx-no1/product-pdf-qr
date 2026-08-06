#!/bin/sh
set -eu

script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$script_directory/prod-compose.sh"

"$compose" exec --no-TTY certbot sh -eu -c '
  source_directory="/tmp/letsencrypt/live/$PUBLIC_DOMAIN"
  stage="/run/certbot/active.$$"
  test -s "$source_directory/fullchain.pem"
  test -s "$source_directory/privkey.pem"
  openssl x509 -in "$source_directory/fullchain.pem" -noout -checkend 86400
  openssl x509 -in "$source_directory/fullchain.pem" -noout -checkhost "$PUBLIC_DOMAIN"
  certificate_key="$(
    openssl x509 -in "$source_directory/fullchain.pem" -pubkey -noout |
      openssl pkey -pubin -outform DER 2>/dev/null |
      openssl sha256
  )"
  private_key="$(
    openssl pkey -in "$source_directory/privkey.pem" -pubout -outform DER 2>/dev/null |
      openssl sha256
  )"
  test "$certificate_key" = "$private_key"

  rm -rf "$stage"
  mkdir -p "$stage"
  cp "$source_directory/fullchain.pem" "$stage/fullchain.pem"
  cp "$source_directory/privkey.pem" "$stage/privkey.pem"
  chgrp 101 "$stage/fullchain.pem" "$stage/privkey.pem"
  chmod 0444 "$stage/fullchain.pem"
  chmod 0440 "$stage/privkey.pem"
  chmod 0750 "$stage"

  rm -rf /tmp/active.previous
  if [ -d /tmp/active ]; then
    mv /tmp/active /tmp/active.previous
  fi
  mv "$stage" /tmp/active
  openssl x509 -in /tmp/active/fullchain.pem -noout -fingerprint -sha256
'

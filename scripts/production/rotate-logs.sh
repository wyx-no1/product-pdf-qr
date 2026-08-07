#!/bin/sh
set -eu

script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$script_directory/prod-compose.sh"

rotate() {
  service="$1"
  log_file="$2"
  "$compose" exec --no-TTY "$service" sh -eu -c '
    umask 077
    log_file="$1"
    max_bytes=10485760
    max_age=86400
    retained=7
    total_limit=73400320
    now="$(date +%s)"
    test -e "$log_file" || : >"$log_file"
    bytes="$(wc -c <"$log_file")"
    modified="$(stat -c %Y "$log_file")"
    age=$((now - modified))
    if [ "$bytes" -lt "$max_bytes" ] && [ "$age" -lt "$max_age" ]; then
      exit 0
    fi
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    rotated="$log_file.$stamp"
    cp "$log_file" "$rotated"
    : >"$log_file"
    gzip "$rotated"

    count=0
    for candidate in $(ls -1t "$log_file".*.gz 2>/dev/null || true); do
      count=$((count + 1))
      if [ "$count" -gt "$retained" ]; then
        rm -f "$candidate"
      fi
    done

    total=0
    for candidate in $(ls -1t "$log_file".*.gz 2>/dev/null || true); do
      size="$(wc -c <"$candidate")"
      total=$((total + size))
      if [ "$total" -gt "$total_limit" ]; then
        rm -f "$candidate"
      fi
    done
  ' sh "$log_file"
}

rotate proxy /var/cache/nginx/access.log
rotate proxy /var/cache/nginx/error.log
rotate app /var/log/product-pdf-qr/application.log

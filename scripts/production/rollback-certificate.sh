#!/bin/sh
set -eu

script_directory="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
compose="$script_directory/prod-compose.sh"

"$compose" exec --no-TTY certbot sh -eu -c '
  test -d /tmp/active.previous
  rm -rf /tmp/active.failed
  if [ -d /tmp/active ]; then
    mv /tmp/active /tmp/active.failed
  fi
  mv /tmp/active.previous /tmp/active
  rm -rf /tmp/active.failed
'

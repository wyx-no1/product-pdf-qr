#!/bin/sh
set -eu

initialization_ready="$(
  PGPASSWORD="$POSTGRES_PASSWORD" PGCONNECT_TIMEOUT=2 psql \
    --host=127.0.0.1 \
    --username="$POSTGRES_USER" \
    --dbname="$APP_DATABASE_NAME" \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --tuples-only \
    --no-align <<'SQL'
SELECT count(*) = 3
FROM pg_roles
WHERE rolname IN ('app_migrate', 'app_rw', 'app_backup')
  AND NOT rolsuper
  AND NOT rolcreatedb
  AND NOT rolcreaterole
  AND NOT rolinherit;
SQL
)"

if [ "$initialization_ready" != "t" ]; then
  echo "PostgreSQL initialization contract is incomplete" >&2
  exit 1
fi

echo "tcp-and-initialization-ready"

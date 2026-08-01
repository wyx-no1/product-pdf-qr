#!/bin/sh
set -eu

initialization_ready="$(
  PGPASSWORD="$POSTGRES_PASSWORD" PGCONNECT_TIMEOUT=2 psql \
    --host=127.0.0.1 \
    --username="$POSTGRES_USER" \
    --dbname="$APP_DATABASE_NAME" \
    --no-psqlrc \
    --set=ON_ERROR_STOP=1 \
    --set=app_database_name="$APP_DATABASE_NAME" \
    --set=app_test_database_name="$APP_TEST_DATABASE_NAME" \
    --tuples-only \
    --no-align <<'SQL'
SELECT
  current_database() = :'app_database_name'
  AND (
    SELECT count(*) = 2
    FROM pg_database AS db
    JOIN pg_roles AS owner ON owner.oid = db.datdba
    WHERE db.datname IN (
      :'app_database_name',
      :'app_test_database_name'
    )
      AND owner.rolname = 'app_migrate'
  )
  AND (
    SELECT count(*) = 3
    FROM pg_roles
    WHERE rolname IN ('app_migrate', 'app_rw', 'app_backup')
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
  );
SQL
)"

if [ "$initialization_ready" != "t" ]; then
  echo "PostgreSQL initialization contract is incomplete" >&2
  exit 1
fi

echo "tcp-and-initialization-ready"

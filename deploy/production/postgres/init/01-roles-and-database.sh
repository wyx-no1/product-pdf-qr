#!/bin/sh
set -eu

required_variables="
POSTGRES_SUPERUSER
APP_MIGRATE_PASSWORD
APP_RW_PASSWORD
APP_BACKUP_PASSWORD
APP_DATABASE_NAME
"

for variable_name in $required_variables; do
  eval "variable_value=\${$variable_name:-}"
  if [ -z "$variable_value" ]; then
    echo "$variable_name is required" >&2
    exit 1
  fi
done

case "$APP_DATABASE_NAME" in
  [A-Za-z_]*)
    case "$APP_DATABASE_NAME" in
      *[!A-Za-z0-9_]*)
        echo "Database names may contain only letters, digits, and underscores" >&2
        exit 1
        ;;
    esac
    ;;
  *)
    echo "Database names must start with a letter or underscore" >&2
    exit 1
    ;;
esac

psql \
  --username "$POSTGRES_SUPERUSER" \
  --dbname postgres \
  --set=ON_ERROR_STOP=1 \
  --set=app_migrate_password="$APP_MIGRATE_PASSWORD" \
  --set=app_rw_password="$APP_RW_PASSWORD" \
  --set=app_backup_password="$APP_BACKUP_PASSWORD" <<'SQL'
CREATE ROLE app_migrate
  LOGIN PASSWORD :'app_migrate_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE app_rw
  LOGIN PASSWORD :'app_rw_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE app_backup
  LOGIN PASSWORD :'app_backup_password'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
SQL

psql --username "$POSTGRES_SUPERUSER" --dbname postgres --set=ON_ERROR_STOP=1 <<SQL
CREATE DATABASE "$APP_DATABASE_NAME" OWNER app_migrate;
REVOKE CONNECT, TEMPORARY ON DATABASE "$APP_DATABASE_NAME" FROM PUBLIC;
GRANT CONNECT ON DATABASE "$APP_DATABASE_NAME" TO app_migrate, app_rw, app_backup;
SQL

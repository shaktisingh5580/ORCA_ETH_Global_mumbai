#!/bin/sh
# Purpose: create the non-owner P02 runtime role from an environment-supplied secret.
# Behavior: execute only during fresh Postgres initialization; never print the password.
# Refs: main plan P02.1/P02.3; docs/04_database.md section 2; Phase: P02.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=runtime_password="$ORCA_RUNTIME_PASSWORD" <<-'EOSQL'
SELECT format(
  'CREATE ROLE orca_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD %L',
  :'runtime_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orca_runtime') \gexec
DROP EXTENSION IF EXISTS postgis CASCADE;
CREATE SCHEMA IF NOT EXISTS extensions;
CREATE EXTENSION postgis SCHEMA extensions;
EOSQL

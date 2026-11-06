-- Purpose: provision ORCA's non-owner runtime role in a dedicated Supabase project.
-- Behavior: create or harden the role using a psql variable; never embed its password.
-- Refs: main plan P02.1/P02.3; Supabase production deployment; Phase: P02.
-- Run with psql --set=runtime_password='...' against the postgres/admin session URL.
SELECT format(
  'CREATE ROLE orca_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD %L',
  :'runtime_password'
) WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'orca_runtime') \gexec
ALTER ROLE orca_runtime NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
CREATE SCHEMA IF NOT EXISTS extensions;

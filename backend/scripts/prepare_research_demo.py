"""Purpose: prepare the explicitly requested research demo using configured Supabase services.
Behavior: inspect first; apply additive migrations and private runtime configuration only with --apply.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated P04/P06/P11 demo.
"""

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg
from dotenv import dotenv_values, set_key
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    values: dict[str, Any] = dict(dotenv_values(ROOT / ".env"))
    admin = values.get("ORCA_MIGRATION_DATABASE_URL") or values.get("ORCA_TEST_DATABASE_URL")
    runtime = values.get("ORCA_DATABASE_URL") or values.get("ORCA_TEST_RUNTIME_DATABASE_URL")
    if not admin or not runtime:
        print("Database admin and runtime configuration required")
        return 2
    with psycopg.connect(admin, connect_timeout=10) as connection:
        revision_row = connection.execute("SELECT to_regclass('public.alembic_version')").fetchone()
        revision_table = revision_row[0] if revision_row else None
        print("Migration registry present:", bool(revision_table))
        if not args.apply:
            return 0
        role = connection.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='orca_runtime'"
        ).fetchone()
        if role is None:
            password = values.get("ORCA_RUNTIME_PASSWORD")
            if not password:
                raise ValueError("Provision runtime role/password first")
            connection.execute(
                sql.SQL(
                    "CREATE ROLE orca_runtime LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS PASSWORD {}"
                ).format(sql.Literal(password))
            )
        elif any(role):
            raise ValueError("Runtime role must not bypass RLS")
    migration_env = dict(
        os.environ, ORCA_DATABASE_URL=admin.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    migration = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            str(ROOT / "backend/alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=ROOT / "backend",
        env=migration_env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if migration.returncode:
        # Store diagnostic details locally; never print credential-bearing driver exceptions.
        folder = ROOT / ".cache/demo"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "migration-error.log").write_text(migration.stderr, encoding="utf-8")
        print("Migration failed; private diagnostic saved in .cache/demo/migration-error.log")
        return 1
    target = ROOT / ".env.demo"
    if not target.exists():
        target.write_text("# Private demo runtime configuration; never commit.\n", encoding="utf-8")
    mapping = {
        "ORCA_AUTH_MODE": "supabase",
        "ORCA_SUPABASE_ANON_KEY": values.get("SUPABASE_ANON_KEY"),
        "ORCA_ENVIRONMENT": "development",
        "ORCA_DATABASE_URL": runtime.replace("postgresql://", "postgresql+psycopg://", 1),
        "ORCA_MONITORING_TOKEN": secrets.token_urlsafe(48),
        "ORCA_RESEARCH_DEMO_ENABLED": "true",
        "ORCA_OBJECT_STORE_BUCKET": "orca-research",
        "ORCA_OBJECT_STORE_ENDPOINT": values.get("ORCA_TEST_S3_ENDPOINT"),
        "ORCA_OBJECT_STORE_ACCESS_KEY": values.get("ORCA_TEST_S3_ACCESS_KEY"),
        "ORCA_OBJECT_STORE_SECRET_KEY": values.get("ORCA_TEST_S3_SECRET_KEY"),
        "ORCA_OBJECT_STORE_REGION": values.get("ORCA_TEST_S3_REGION", "ap-south-1"),
        "ORCA_OIDC_ISSUER": values.get("ORCA_OIDC_ISSUER"),
        "ORCA_OIDC_AUDIENCE": values.get("ORCA_OIDC_AUDIENCE"),
        "ORCA_OIDC_JWKS_URL": values.get("ORCA_OIDC_JWKS_URL"),
    }
    for key, value in mapping.items():
        if value:
            set_key(str(target), key, str(value))
    print("Additive migrations applied; private runtime saved in .env.demo")
    print("Automated tests were not executed; this is deployment setup, not gate completion")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("Demo setup unavailable:", type(error).__name__)
        raise SystemExit(1) from None

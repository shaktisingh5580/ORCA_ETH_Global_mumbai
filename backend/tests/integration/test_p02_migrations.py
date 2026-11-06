"""Purpose: verify P02 migrations against an actual disposable PostGIS database.
Behavior: rebuild only an explicitly named *_test database schema and probe tenant constraints.
Refs: main plan P02.1; docs/04_database.md sections 2,8,9; Phase: P02.
"""

import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest

BACKEND = Path(__file__).resolve().parents[2]


def database_url() -> str:
    value = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not value:
        pytest.skip("live PostGIS unavailable: set ORCA_TEST_DATABASE_URL")
    database = value.rsplit("/", 1)[-1].split("?", 1)[0]
    host = urlparse(value.replace("postgresql+psycopg", "postgresql", 1)).hostname or ""
    dedicated_supabase = os.environ.get(
        "ORCA_TEST_DATABASE_PURPOSE"
    ) == "dedicated-staging" and host.endswith((".supabase.co", ".pooler.supabase.com"))
    if not database.endswith("_test") and not dedicated_supabase:
        pytest.fail(
            "migration tests require a *_test database or an explicitly dedicated Supabase staging project"
        )
    return value


def rebuild(target: str = "head") -> str:
    url = database_url()
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS research CASCADE")
        connection.execute("DROP SCHEMA IF EXISTS iam CASCADE")
        connection.execute("DROP TABLE IF EXISTS public.alembic_version")
    env = dict(os.environ, ORCA_DATABASE_URL=url)
    result = subprocess.run(
        [
            str(BACKEND / ".venv/Scripts/alembic.exe")
            if os.name == "nt"
            else str(BACKEND / ".venv/bin/alembic"),
            "-c",
            str(BACKEND / "alembic.ini"),
            "upgrade",
            target,
        ],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return url


@pytest.mark.live
def test_empty_db_upgrade_head():
    url = rebuild()
    with psycopg.connect(url) as connection:
        assert (
            connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == "0013_prototype_workflow"
        )


@pytest.mark.live
def test_upgrade_from_prior_revision():
    url = rebuild("0001_iam")
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT to_regclass('iam.organizations')").fetchone()[0]
        assert connection.execute("SELECT to_regclass('research.projects')").fetchone()[0] is None
    env = dict(os.environ, ORCA_DATABASE_URL=url)
    executable = str(
        BACKEND / (".venv/Scripts/alembic.exe" if os.name == "nt" else ".venv/bin/alembic")
    )
    subprocess.run(
        [executable, "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        check=True,
        timeout=120,
    )
    with psycopg.connect(url) as connection:
        assert connection.execute("SELECT to_regclass('research.projects')").fetchone()[0]


@pytest.mark.live
def test_postgis_extension_and_indexes():
    url = rebuild()
    with psycopg.connect(url) as connection:
        assert connection.execute(
            "SELECT extversion FROM pg_extension WHERE extname='postgis'"
        ).fetchone()
        assert (
            connection.execute("""SELECT n.nspname FROM pg_extension e
                JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='postgis'""").fetchone()[
                0
            ]
            == "extensions"
        )
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='research'"
            )
        }
        assert {"ix_projects_tenant_state", "ix_upload_project_status"} <= indexes


@pytest.mark.live
def test_fk_check_tenant_safely():
    url = rebuild()
    tenant_a, tenant_b, project = uuid4(), uuid4(), uuid4()
    with psycopg.connect(url) as connection:
        connection.execute(
            "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(%s,%s,'A','a')",
            (uuid4(), tenant_a),
        )
        connection.execute(
            "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(%s,%s,'B','b')",
            (uuid4(), tenant_b),
        )
        connection.execute(
            "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(%s,%s,'p','P')",
            (project, tenant_a),
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO research.project_members(id,tenant_id,project_id,principal_id,role) VALUES(%s,%s,%s,%s,'viewer')",
                    (uuid4(), tenant_b, project, uuid4()),
                )


@pytest.mark.live
def test_schema_no_old_tables_assumed():
    url = rebuild()
    with psycopg.connect(url) as connection:
        rows = connection.execute(
            "SELECT table_schema,table_name FROM information_schema.tables WHERE table_schema IN ('iam','research')"
        ).fetchall()
    assert set(rows) == {
        ("research", "catalog_products"),
        ("research", "catalog_versions"),
        ("research", "dataset_snapshots"),
        ("research", "snapshot_requests"),
        ("research", "snapshot_project_grants"),
        ("research", "source_probes"),
        ("research", "demo_results"),
        ("iam", "idempotency_records"),
        ("iam", "organizations"),
        ("iam", "organization_creation_keys"),
        ("iam", "membership_audit"),
        ("iam", "organization_policies"),
        ("iam", "invitations"),
        ("iam", "access_grants"),
        ("iam", "memberships"),
        ("research", "projects"),
        ("research", "project_members"),
        ("research", "uploads"),
        ("research", "prototype_investigations"),
        ("research", "prototype_runs"),
        ("research", "prototype_publications"),
    }

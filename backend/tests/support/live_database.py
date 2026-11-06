"""Purpose: prepare the explicitly disposable P02 database for live integration tests.
Behavior: require *_test naming, migrate to head and seed two isolated tenants.
Refs: main plan P02.1-P02.4; Phase: P02 test support only.
"""

import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest

BACKEND = Path(__file__).resolve().parents[2]


def prepare_database() -> tuple[str, str, UUID, UUID, UUID, UUID]:
    admin = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    runtime = os.environ.get("ORCA_TEST_RUNTIME_DATABASE_URL", "")
    if not admin or not runtime:
        pytest.skip("live PostGIS runtime-role database unavailable")
    database = admin.rsplit("/", 1)[-1].split("?", 1)[0]
    host = urlparse(admin.replace("postgresql+psycopg", "postgresql", 1)).hostname or ""
    dedicated_supabase = os.environ.get(
        "ORCA_TEST_DATABASE_PURPOSE"
    ) == "dedicated-staging" and host.endswith((".supabase.co", ".pooler.supabase.com"))
    if not database.endswith("_test") and not dedicated_supabase:
        pytest.fail(
            "integration tests require a *_test database or an explicitly dedicated Supabase staging project"
        )
    executable = str(
        BACKEND / (".venv/Scripts/alembic.exe" if os.name == "nt" else ".venv/bin/alembic")
    )
    subprocess.run(
        [executable, "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND,
        env=dict(os.environ, ORCA_DATABASE_URL=admin),
        check=True,
        timeout=120,
        capture_output=True,
    )
    tenant_a, tenant_b, project_a, project_b = uuid4(), uuid4(), uuid4(), uuid4()
    with psycopg.connect(admin) as connection:
        # The test administrator can truncate the disposable audit table; runtime
        # UPDATE/DELETE remains denied and the immutable-row trigger stays installed.
        connection.execute("TRUNCATE TABLE iam.membership_audit")
        connection.execute("DELETE FROM iam.organization_creation_keys")
        connection.execute("DELETE FROM iam.invitations")
        connection.execute("DELETE FROM iam.access_grants")
        connection.execute("DELETE FROM iam.idempotency_records")
        connection.execute("DELETE FROM research.uploads")
        connection.execute("DELETE FROM research.project_members")
        connection.execute("DELETE FROM research.projects")
        connection.execute("DELETE FROM iam.memberships")
        connection.execute("DELETE FROM iam.organizations")
        connection.execute(
            "INSERT INTO iam.organizations(id,tenant_id,name,slug) VALUES(%s,%s,'A',%s),(%s,%s,'B',%s)",
            (uuid4(), tenant_a, f"a-{tenant_a}", uuid4(), tenant_b, f"b-{tenant_b}"),
        )
        connection.execute(
            "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(%s,%s,'seed-a','A'),(%s,%s,'seed-b','B')",
            (project_a, tenant_a, project_b, tenant_b),
        )
    return admin, runtime, tenant_a, tenant_b, project_a, project_b

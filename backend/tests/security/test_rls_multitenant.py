"""Purpose: prove PostgreSQL RLS and tenant context under the actual non-owner role.
Behavior: seed through migrator access, then attempt cross-tenant operations via runtime pool.
Refs: main plan P02.3; API sections 10,12,16; Phase: P02.
"""

import os
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import create_engine, text

from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

BACKEND = Path(__file__).resolve().parents[2]


def urls() -> tuple[str, str]:
    admin = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    runtime = os.environ.get("ORCA_TEST_RUNTIME_DATABASE_URL", "")
    if not admin or not runtime:
        pytest.skip("live runtime-role database unavailable")
    if not admin.rsplit("/", 1)[-1].split("?", 1)[0].endswith("_test"):
        pytest.fail("RLS tests refuse a database not ending in _test")
    return admin, runtime


def prepare() -> tuple[str, str, UUID, UUID, UUID, UUID]:
    admin, runtime = urls()
    env = dict(os.environ, ORCA_DATABASE_URL=admin)
    executable = str(
        BACKEND / (".venv/Scripts/alembic.exe" if os.name == "nt" else ".venv/bin/alembic")
    )
    subprocess.run(
        [executable, "-c", str(BACKEND / "alembic.ini"), "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        check=True,
        timeout=120,
        capture_output=True,
    )
    tenant_a, tenant_b, project_a, project_b = uuid4(), uuid4(), uuid4(), uuid4()
    with psycopg.connect(admin) as connection:
        connection.execute(
            "TRUNCATE research.prototype_publications,research.prototype_runs,research.prototype_investigations"
        )
        connection.execute("TRUNCATE TABLE iam.membership_audit")
        connection.execute("DELETE FROM iam.organization_creation_keys")
        connection.execute("DELETE FROM iam.invitations")
        connection.execute("DELETE FROM iam.access_grants")
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
            "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(%s,%s,'a','A'),(%s,%s,'b','B')",
            (project_a, tenant_a, project_b, tenant_b),
        )
    return admin, runtime, tenant_a, tenant_b, project_a, project_b


@pytest.mark.live
def test_two_tenant_read_write_denied():
    _, runtime, tenant_a, tenant_b, project_a, project_b = prepare()
    with psycopg.connect(runtime) as connection:
        connection.execute("SELECT set_config('orca.tenant_id', %s, true)", (str(tenant_a),))
        assert connection.execute("SELECT id FROM research.projects").fetchall() == [(project_a,)]
        assert (
            connection.execute(
                "UPDATE research.projects SET name='changed' WHERE id=%s RETURNING id", (project_b,)
            ).fetchone()
            is None
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(%s,%s,'bad','Bad')",
                    (uuid4(), tenant_b),
                )


@pytest.mark.live
def test_cross_org_support_fk_rejected():
    admin, _, tenant_a, tenant_b, project_a, _ = prepare()
    with psycopg.connect(admin) as connection:
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO research.uploads(id,tenant_id,project_id,object_key,filename,media_type,size_bytes,sha256,classification) VALUES(%s,%s,%s,%s,'x','text/plain',1,%s,'restricted')",
                    (uuid4(), tenant_b, project_a, f"staging/{uuid4()}", "a" * 64),
                )


@pytest.mark.live
def test_session_pool_reuse_no_leak():
    _, runtime, tenant_a, tenant_b, project_a, project_b = prepare()
    engine = create_engine(runtime, pool_size=1, max_overflow=0, pool_reset_on_return="rollback")
    with TenantUnitOfWork(engine, tenant_a) as session:
        assert session.execute(text("SELECT id FROM research.projects")).scalar_one() == project_a
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM research.projects")).fetchall() == []
    with TenantUnitOfWork(engine, tenant_b) as session:
        assert session.execute(text("SELECT id FROM research.projects")).scalar_one() == project_b
    engine.dispose()


@pytest.mark.live
def test_runtime_role_bypassrls_false():
    _, runtime, *_ = prepare()
    with psycopg.connect(runtime) as connection:
        row = connection.execute(
            "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert row == (False, False)
        assert connection.execute(
            "SELECT tableowner=current_user FROM pg_tables WHERE schemaname='research' LIMIT 1"
        ).fetchone() == (False,)


@pytest.mark.live
def test_default_deny_no_context():
    _, runtime, _, tenant_b, _, _ = prepare()
    with psycopg.connect(runtime) as connection:
        assert connection.execute("SELECT count(*) FROM research.projects").fetchone()[0] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO research.projects(id,tenant_id,slug,name) VALUES(%s,%s,'bad','Bad')",
                    (uuid4(), tenant_b),
                )

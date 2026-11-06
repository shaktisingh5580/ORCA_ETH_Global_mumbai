"""Purpose: verify real organization bootstrap, authorization, ETags and last-admin protection.
Behavior: use only an isolated local database and roll back all created workspace records.
Refs: API 3.2; main plan P02; Phase: P02 repair.
"""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from orca.application.identity import LastAdministratorError
from orca.application.organizations import Organizations, bootstrap
from orca.application.projects import IdempotencyConflict, PreconditionRequired, StaleVersion
from orca.contracts.organizations import CreateOrganization, PatchMember, PatchOrganization
from orca.domain.identity import Principal


@pytest.fixture
def workspace():
    url = os.environ.get("ORCA_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("isolated local database required")
    if "@127.0.0.1:" not in url or not url.endswith("/orca_test"):
        pytest.fail("organization tests require disposable local orca_test")
    backend = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(backend / "alembic.ini"), "upgrade", "head"],
        cwd=backend,
        env=dict(os.environ, ORCA_DATABASE_URL=url),
        capture_output=True,
        check=True,
        timeout=120,
    )
    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.connect() as connection:
            outer = connection.begin()
            try:
                connection.execute(text("SET LOCAL ROLE orca_runtime"))
                with Session(bind=connection, join_transaction_mode="create_savepoint") as session:
                    subject = str(uuid4())
                    principal = Principal(
                        subject, "test", frozenset({"org:read", "org:write", "org:members"})
                    )
                    body = CreateOrganization(name="Test workspace", slug="org-" + str(uuid4()))
                    identifier = bootstrap(session, subject, body, "create-key")
                    tenant = session.execute(
                        text("SELECT iam.resolve_tenant(:subject,:organization)"),
                        {"subject": subject, "organization": identifier},
                    ).scalar_one()
                    session.execute(
                        text("SELECT set_config('orca.tenant_id',:tenant,true)"),
                        {"tenant": str(tenant)},
                    )
                    yield session, Organizations(session, tenant, identifier), principal, body
                    session.rollback()
            finally:
                outer.rollback()
    finally:
        engine.dispose()


@pytest.mark.live
def test_organization_creation_idempotency_and_isolation(workspace):
    session, service, principal, body = workspace
    assert bootstrap(session, principal.subject, body, "create-key") == service.organization
    with pytest.raises(IdempotencyConflict):
        bootstrap(
            session, principal.subject, body.model_copy(update={"name": "different"}), "create-key"
        )
    assert service.authorize(principal, "org:write") == {"org_owner"}
    assert service.get().verification_status == "unverified"
    assert len(service.members(None, 20)) == 1
    session.execute(
        text("SELECT set_config('orca.tenant_id',:tenant,true)"), {"tenant": str(uuid4())}
    )
    with pytest.raises(LookupError):
        service.get()
    assert service.members(None, 20) == []


@pytest.mark.live
def test_organization_etags_and_current_authorization(workspace):
    session, service, principal, _ = workspace
    service.authorize(principal, "org:write")
    with pytest.raises(PreconditionRequired):
        service.update(PatchOrganization(display_name="updated"), None)
    result = service.update(PatchOrganization(display_name="updated"), '"1"')
    assert result.name == "updated" and result.etag == '"2"'
    with pytest.raises(StaleVersion):
        service.update(PatchOrganization(display_name="stale"), '"1"')
    session.execute(
        text(
            "UPDATE iam.memberships SET status='revoked' WHERE tenant_id=:tenant AND subject=:subject"
        ),
        {"tenant": service.tenant, "subject": principal.subject},
    )
    with pytest.raises(PermissionError):
        service.authorize(principal, "org:write")


@pytest.mark.live
def test_member_last_admin_owner_boundary_and_audit(workspace):
    session, service, principal, _ = workspace
    roles = service.authorize(principal, "org:members")
    owner = service.members(None, 20)[0]
    with pytest.raises(LastAdministratorError):
        service.change_member(
            owner.id, PatchMember(status="revoked"), owner.etag, principal.subject, roles
        )
    other = uuid4()
    session.execute(
        text(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(:id,:tenant,:subject,'org_admin')"
        ),
        {"id": other, "tenant": service.tenant, "subject": str(uuid4())},
    )
    with pytest.raises(PermissionError):
        service.change_member(
            owner.id, PatchMember(status="suspended"), owner.etag, "admin", {"org_admin"}
        )
    changed = service.change_member(
        other, PatchMember(role="reviewer"), '"1"', principal.subject, roles
    )
    assert changed.role == "reviewer" and changed.etag == '"2"'
    with pytest.raises(StaleVersion):
        service.change_member(other, PatchMember(status="revoked"), '"1"', principal.subject, roles)
    revoked = service.change_member(
        other, PatchMember(status="revoked"), '"2"', principal.subject, roles
    )
    assert revoked.status == "revoked"
    assert (
        session.execute(
            text("SELECT count(*) FROM iam.membership_audit WHERE tenant_id=:tenant"),
            {"tenant": service.tenant},
        ).scalar_one()
        == 2
    )
    with pytest.raises(DBAPIError), session.begin_nested():
        session.execute(
            text("DELETE FROM iam.membership_audit WHERE tenant_id=:tenant"),
            {"tenant": service.tenant},
        )

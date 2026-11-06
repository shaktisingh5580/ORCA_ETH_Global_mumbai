"""Purpose: prove durable idempotency, ETags, last-admin safety and tenant pagination.
Behavior: execute application services against live PostgreSQL through the runtime role.
Refs: main plan P02.4; API sections 3.2-3.3,11.1; Phase: P02.
"""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from live_database import prepare_database
from sqlalchemy import select

from orca.application.identity import LastAdministratorError, revoke_membership
from orca.application.projects import (
    IdempotencyConflict,
    PreconditionRequired,
    ProjectService,
    StaleVersion,
)
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.models import Membership, Project
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork


@pytest.mark.live
def test_create_once_concurrent_same_key():
    _, runtime, tenant, *_ = prepare_database()
    engine = create_runtime_engine(runtime)
    payload = {"slug": "same", "name": "Same", "description": None}

    def create() -> str:
        with TenantUnitOfWork(engine, tenant) as session:
            return str(ProjectService().create(session, tenant, payload, "same-key").id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: create(), range(2)))
    assert len(set(ids)) == 1
    engine.dispose()


@pytest.mark.live
def test_idempotency_key_changed_body_409():
    _, runtime, tenant, *_ = prepare_database()
    engine = create_runtime_engine(runtime)
    with TenantUnitOfWork(engine, tenant) as session:
        ProjectService().create(session, tenant, {"slug": "one", "name": "One"}, "key")
    with pytest.raises(IdempotencyConflict):
        with TenantUnitOfWork(engine, tenant) as session:
            ProjectService().create(session, tenant, {"slug": "two", "name": "Two"}, "key")
    engine.dispose()


@pytest.mark.live
def test_etag_required_428():
    _, runtime, tenant, _, project_id, _ = prepare_database()
    engine = create_runtime_engine(runtime)
    with pytest.raises(PreconditionRequired):
        with TenantUnitOfWork(engine, tenant) as session:
            project = session.get(Project, project_id)
            assert project is not None
            ProjectService().update(session, project, {"name": "Changed"}, None)
    engine.dispose()


@pytest.mark.live
def test_stale_edit_412():
    _, runtime, tenant, _, project_id, _ = prepare_database()
    engine = create_runtime_engine(runtime)
    with pytest.raises(StaleVersion):
        with TenantUnitOfWork(engine, tenant) as session:
            project = session.get(Project, project_id)
            assert project is not None
            ProjectService().update(session, project, {"name": "Changed"}, '"0"')
    engine.dispose()


@pytest.mark.live
def test_last_admin_cannot_remove():
    admin, runtime, tenant, *_ = prepare_database()
    with psycopg.connect(admin) as connection:
        connection.execute(
            "INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(gen_random_uuid(),%s,'owner','org_owner')",
            (tenant,),
        )
    engine = create_runtime_engine(runtime)
    with pytest.raises(LastAdministratorError):
        with TenantUnitOfWork(engine, tenant) as session:
            membership = session.scalar(select(Membership).where(Membership.subject == "owner"))
            assert membership is not None
            revoke_membership(session, membership)
    engine.dispose()


@pytest.mark.live
def test_tenant_paginated_list():
    _, runtime, tenant, *_ = prepare_database()
    engine = create_runtime_engine(runtime)
    for number in range(3):
        with TenantUnitOfWork(engine, tenant) as session:
            ProjectService().create(
                session, tenant, {"slug": f"p-{number}", "name": f"P {number}"}, f"key-{number}"
            )
    with TenantUnitOfWork(engine, tenant) as session:
        first = ProjectService().list(session, tenant, 2)
        assert len(first.items) == 2 and first.next_cursor
        second = ProjectService().list(session, tenant, 2, first.items[-1].id)
        assert second.items and not {p.id for p in first.items} & {p.id for p in second.items}
    engine.dispose()

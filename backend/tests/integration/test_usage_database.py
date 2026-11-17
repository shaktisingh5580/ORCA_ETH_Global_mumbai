"""Purpose: verify P08 usage reports real budget reservations from disposable PostgreSQL.
Behavior: enqueue and cancel an approved run, then compare active and released ceilings.
Refs: main_implementation plan.md P08.1; api design.md §3.9; Phase: P08.
"""

from types import SimpleNamespace

import pytest
from fastapi import Request
from p05_queue import approved_queue
from sqlalchemy import text

from orca.api.routers.operations import organization_usage
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue


@pytest.mark.live
def test_usage_reflects_budget_reservation_and_release() -> None:
    database, tenant, project, _, command = approved_queue()
    try:
        with TenantUnitOfWork(database, tenant) as session:
            organization = session.execute(
                text("SELECT id FROM iam.organizations WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            ).scalar_one()
            subject = session.execute(
                text("SELECT subject FROM iam.memberships WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            ).scalar_one()
        request = Request(
            {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(runtime_engine=database))}
        )
        principal = Principal(subject, "test", frozenset({"org:read"}))
        before = organization_usage(organization, request, principal, window="24h")
        assert before.run_reservations_created == 0
        with TenantUnitOfWork(database, tenant) as session:
            receipt = PostgresQueue(session, tenant, project).enqueue_run(command)
        active = organization_usage(organization, request, principal, window="24h")
        assert active.run_reservations_created == 1
        assert active.active_reservations_created == 1
        assert active.reserved_budget_ceiling_usd == "2.00000000"
        assert active.actual_spend_usd is None
        with TenantUnitOfWork(database, tenant) as session:
            assert PostgresQueue(session, tenant, project).cancel(receipt.job_id)
        released = organization_usage(organization, request, principal, window="24h")
        assert released.active_reservations_created == 0
        assert released.reserved_budget_ceiling_usd == "0.00000000"
        assert released.released_budget_ceiling_usd == "2.00000000"
        assert released.actual_spend_usd is None
    finally:
        database.dispose()

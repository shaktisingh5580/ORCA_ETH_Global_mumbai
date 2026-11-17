"""Purpose: verify P08 job-health counts against tenant-scoped disposable PostgreSQL rows.
Behavior: enqueue a real run and read the RLS-protected operational summary.
Refs: main_implementation plan.md P08.1; api design.md §§3.9, 14; Phase: P08.
"""

from types import SimpleNamespace

import pytest
from fastapi import Request
from p05_queue import approved_queue
from sqlalchemy import text

from orca.api.routers.operations import job_health
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue


@pytest.mark.live
def test_job_health_reads_scoped_queue_rows() -> None:
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
            PostgresQueue(session, tenant, project).enqueue_run(command)
        app = SimpleNamespace(state=SimpleNamespace(runtime_engine=database))
        request = Request({"type": "http", "app": app})
        principal = Principal(subject, "test", frozenset({"ops:read"}))
        result = job_health(request, principal, organization, since=None)
        assert result.organization_id == organization
        assert result.queued == 1
        assert result.leased == result.overdue_leases == 0
        assert result.retry_attempts_since == result.expired_attempts_since == 0
        assert result.fencing_failures is None
    finally:
        database.dispose()

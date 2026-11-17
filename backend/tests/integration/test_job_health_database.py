"""Purpose: verify P08 job-health and operation reads against disposable PostgreSQL rows.
Behavior: enqueue a real run and read RLS-protected summaries with project authority.
Refs: main_implementation plan.md P08.1; api design.md §§3.9, 14; Phase: P08.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from p05_queue import approved_queue
from sqlalchemy import text

from orca.api.routers.operations import get_operation, job_health, list_operations
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
            receipt = PostgresQueue(session, tenant, project).enqueue_run(command)
        app = SimpleNamespace(state=SimpleNamespace(runtime_engine=database))
        request = Request({"type": "http", "app": app})
        principal = Principal(subject, "test", frozenset({"ops:read"}))
        result = job_health(request, principal, organization, since=None)
        assert result.organization_id == organization
        assert result.queued == 1
        assert result.leased == result.overdue_leases == 0
        assert result.retry_attempts_since == result.expired_attempts_since == 0
        assert result.fencing_failures is None
        detail = get_operation(
            receipt.job_id,
            organization,
            request,
            Principal(subject, "test", frozenset({"operation:read", "project:read"})),
        )
        assert detail.status == "queued"
        assert detail.id == receipt.job_id
        assert detail.resource_ref is not None
        assert detail.resource_ref.id == str(receipt.run_id)
        assert (
            get_operation(
                receipt.run_id,
                organization,
                request,
                Principal(subject, "test", frozenset({"operation:read", "project:read"})),
            ).id
            == receipt.run_id
        )
        page = list_operations(
            organization, request, principal, state=None, kind=None, cursor=None, limit=20
        )
        assert [item.id for item in page.items] == [receipt.job_id]
        assert page.next_cursor is None
        assert [
            item.id
            for item in list_operations(
                organization,
                request,
                principal,
                state="queued",
                kind="research_run",
                cursor=None,
                limit=20,
            ).items
        ] == [receipt.job_id]
        assert (
            list_operations(
                organization, request, principal, state="failed", kind=None, cursor=None, limit=20
            ).items
            == []
        )
        assert (
            list_operations(
                organization,
                request,
                principal,
                state=None,
                kind=None,
                cursor=str(receipt.job_id),
                limit=20,
            ).items
            == []
        )
        with pytest.raises(HTTPException) as denied:
            get_operation(
                receipt.job_id,
                organization,
                request,
                Principal(subject, "test", frozenset({"operation:read"})),
            )
        assert denied.value.status_code == 404
    finally:
        database.dispose()

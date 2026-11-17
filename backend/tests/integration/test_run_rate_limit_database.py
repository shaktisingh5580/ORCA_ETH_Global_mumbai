"""Purpose: verify P08 durable admission limits and P05 idempotent budget behavior.
Behavior: reject the eleventh run without charging budget; allow exact request replay.
Refs: main_implementation plan.md P08.1; api design.md §§2.1,3.9; Phase: P08.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request, Response
from p05_queue import approved_queue, queue_command
from sqlalchemy import text

from orca.api.routers.investigations import raise_service_error
from orca.api.routers.operations import (
    organization_audit_events,
    organization_quotas,
    patch_organization_quotas,
)
from orca.contracts.operations import PatchRunQuotaSet
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.jobs.postgres_queue import PostgresQueue, RunRateLimitExceeded


@pytest.mark.live
def test_budget_accounting_and_rate_limit() -> None:
    database, tenant, project, investigation, approved = approved_queue()
    try:
        first = None
        for index in range(10):
            command = queue_command(
                investigation,
                approved.plan_id,
                approved.approved_digest,
                f"rate-{index}",
                "0.10",
            )
            with TenantUnitOfWork(database, tenant) as session:
                receipt = PostgresQueue(session, tenant, project).enqueue_run(command)
            if first is None:
                first = (command, receipt)
        assert first is not None
        with pytest.raises(RunRateLimitExceeded):
            with TenantUnitOfWork(database, tenant) as session:
                PostgresQueue(session, tenant, project).enqueue_run(
                    queue_command(
                        investigation,
                        approved.plan_id,
                        approved.approved_digest,
                        "rate-eleventh",
                        "0.10",
                    )
                )
        with TenantUnitOfWork(database, tenant) as session:
            replay = PostgresQueue(session, tenant, project).enqueue_run(first[0])
            counts = session.execute(
                text("""SELECT
              (SELECT count(*) FROM research.compute_runs) AS runs,
              (SELECT count(*) FROM research.run_budget_reservations) AS reservations,
              (SELECT sum(reserved_amount) FROM research.plan_budget_ledgers) AS reserved""")
            )
            row = counts.mappings().one()
        assert replay == first[1]
        assert row["runs"] == 10
        assert row["reservations"] == 10
        assert row["reserved"] == 1
        with pytest.raises(HTTPException) as denied:
            raise_service_error(RunRateLimitExceeded())
        assert denied.value.status_code == 429
        assert denied.value.headers == {"Retry-After": "60"}
    finally:
        database.dispose()


@pytest.mark.live
def test_versioned_run_quota_enforced_and_audited() -> None:
    database, tenant, project, investigation, approved = approved_queue()
    try:
        with TenantUnitOfWork(database, tenant) as session:
            identity = (
                session.execute(
                    text("""SELECT o.id,m.subject FROM iam.organizations o
              JOIN iam.memberships m ON m.tenant_id=o.tenant_id
              WHERE o.tenant_id=:tenant"""),
                    {"tenant": tenant},
                )
                .mappings()
                .one()
            )
        request = Request(
            {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(runtime_engine=database))}
        )
        principal = Principal(
            identity["subject"], "test", frozenset({"org:read", "org:write", "audit:read"})
        )
        original = organization_quotas(identity["id"], request, Response(), principal)
        assert original.max_runs_per_minute == 10
        response = Response()
        changed = patch_organization_quotas(
            identity["id"],
            PatchRunQuotaSet(max_runs_per_minute=2),
            request,
            response,
            principal,
            if_match='"1"',
        )
        assert changed.version == 2
        assert response.headers["etag"] == '"2"'
        with pytest.raises(HTTPException) as stale:
            patch_organization_quotas(
                identity["id"],
                PatchRunQuotaSet(max_runs_per_minute=3),
                request,
                Response(),
                principal,
                if_match='"1"',
            )
        assert stale.value.status_code == 412
        for index in range(2):
            with TenantUnitOfWork(database, tenant) as session:
                PostgresQueue(session, tenant, project).enqueue_run(
                    queue_command(
                        investigation,
                        approved.plan_id,
                        approved.approved_digest,
                        f"configured-{index}",
                        "0.10",
                    )
                )
        with pytest.raises(RunRateLimitExceeded):
            with TenantUnitOfWork(database, tenant) as session:
                PostgresQueue(session, tenant, project).enqueue_run(
                    queue_command(
                        investigation,
                        approved.plan_id,
                        approved.approved_digest,
                        "configured-third",
                        "0.10",
                    )
                )
        audit = organization_audit_events(
            identity["id"],
            request,
            principal,
            start=None,
            end=None,
            actor=None,
            event_type="run_quota_updated",
            cursor=None,
            limit=20,
        )
        assert len(audit.items) == 1
        assert audit.items[0].event_type == "run_quota_updated"
    finally:
        database.dispose()

"""Purpose: expose P08 organization-scoped job health to authorized operators.
Behavior: intersect token and membership scope, read RLS-protected counts, and redact unknowns.
Refs: main_implementation plan.md P08.1; api design.md §§3.9, 14; Phase: P08.
"""

from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.contracts.operations import JobHealthSummary
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/admin", tags=["operations"])


@router.get("/job-health", response_model=JobHealthSummary)
def job_health(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    queue: Literal["research_run"] = "research_run",
    since: datetime | None = Query(default=None),
) -> JobHealthSummary:
    """Report current tenant queue state and bounded recent attempt history."""
    database = engine(request)
    tenant, roles = resolve_tenant(database, principal, organization_id)
    try:
        principal.require("ops:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Operations scope required") from error
    observed_at = datetime.now(timezone.utc)
    lower_bound = since or observed_at - timedelta(days=1)
    if lower_bound.tzinfo is None or lower_bound > observed_at:
        raise HTTPException(422, "since must be a past timestamp with timezone")
    if observed_at - lower_bound > timedelta(days=30):
        raise HTTPException(422, "since may be at most 30 days ago")
    with TenantUnitOfWork(database, tenant) as session:
        queue_counts = (
            session.execute(
                text("""SELECT count(*) FILTER (WHERE state='queued') AS queued,
              count(*) FILTER (WHERE state='leased') AS leased,
              count(*) FILTER (WHERE state='leased' AND lease_until<=clock_timestamp()) AS overdue_leases,
              count(*) FILTER (WHERE state='completed') AS completed,
              count(*) FILTER (WHERE state='cancelled') AS cancelled
              FROM research.job_queue WHERE tenant_id=:tenant"""),
                {"tenant": tenant},
            )
            .mappings()
            .one()
        )
        attempt_counts = (
            session.execute(
                text("""SELECT count(*) FILTER (WHERE state='expired') AS expired_attempts_since,
              count(*) FILTER (WHERE generation>1) AS retry_attempts_since
              FROM research.job_attempts WHERE tenant_id=:tenant AND claimed_at>=:since"""),
                {"tenant": tenant, "since": lower_bound},
            )
            .mappings()
            .one()
        )
    return JobHealthSummary.from_counts(
        organization_id,
        observed_at,
        lower_bound,
        {key: int(value) for key, value in queue_counts.items()},
        {key: int(value) for key, value in attempt_counts.items()},
    )

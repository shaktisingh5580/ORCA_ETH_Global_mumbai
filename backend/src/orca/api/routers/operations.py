"""Purpose: expose P08 organization-scoped job, source, usage and operation reads.
Behavior: intersect token and membership scope, read RLS-protected rows, and redact unknowns.
Refs: main_implementation plan.md P08.1; api design.md §§3.9, 14; Phase: P08.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.workspace_access import authorize_project
from orca.contracts.common import Operation, Page, ResourceRef
from orca.contracts.operations import JobHealthSummary, SourceHealthView, UsageSummary
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/admin", tags=["operations"])
operation_router = APIRouter(prefix="/v1/operations", tags=["operations"])
usage_router = APIRouter(prefix="/v1/organizations", tags=["operations"])
_BUDGET_UNIT = Decimal("0.00000001")

_OPERATION_ROWS = """SELECT q.id,q.project_id,q.run_id,q.state AS queue_state,q.generation,
  q.created_at,q.updated_at,r.investigation_id,r.plan_digest,r.state AS run_state
  FROM research.job_queue q JOIN research.compute_runs r
    ON r.tenant_id=q.tenant_id AND r.project_id=q.project_id AND r.id=q.run_id
  WHERE q.tenant_id=:tenant"""


def _operation(row: dict[str, Any], *, requested_id: UUID | None = None) -> Operation:
    queue_state = row["queue_state"]
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    if queue_state == "queued":
        status = "queued"
    elif queue_state == "leased":
        status = "running"
    elif queue_state == "cancelled":
        status = "cancelled"
    elif row["run_state"] == "FAILED":
        status = "failed"
    else:
        status = "succeeded"
    return Operation(
        id=requested_id or row["id"],
        kind="research_run",
        status=status,
        resource_ref=ResourceRef(
            type="research_run",
            id=str(row["run_id"]),
            version=str(row["generation"]),
            digest=row["plan_digest"],
        ),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        progress=0
        if status == "queued"
        else 1
        if status in {"succeeded", "failed", "cancelled"}
        else None,
        links={
            "run": f"/v1/research/investigations/{row['investigation_id']}/runs/{row['run_id']}"
        },
    )


@operation_router.get("/{operation_id}", response_model=Operation)
def get_operation(
    operation_id: UUID, organization_id: UUID, request: Request, principal: CurrentPrincipal
) -> Operation:
    """Resolve a P05 queue job or run alias without exposing another project."""
    database = engine(request)
    tenant, roles = resolve_tenant(database, principal, organization_id)
    try:
        principal.require("operation:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Operation read scope required") from error
    with TenantUnitOfWork(database, tenant) as session:
        row = (
            session.execute(
                text(
                    _OPERATION_ROWS
                    + " AND (q.id=:operation OR q.run_id=:operation) ORDER BY q.id LIMIT 1"
                ),
                {"tenant": tenant, "operation": operation_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, "Operation not found")
        try:
            authorize_project(session, tenant, row["project_id"], principal, "project:read")
        except (LookupError, PermissionError) as error:
            raise HTTPException(404, "Operation not found") from error
        return _operation(dict(row), requested_id=operation_id)


@operation_router.get("", response_model=Page[Operation])
def list_operations(
    organization_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = None,
    kind: Literal["research_run"] | None = None,
    cursor: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=20, ge=1, le=100),
) -> Page[Operation]:
    """List only the current tenant's research run jobs in stable ID order."""
    database = engine(request)
    tenant, roles = resolve_tenant(database, principal, organization_id)
    try:
        principal.require("ops:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Operations scope required") from error
    try:
        after = UUID(cursor) if cursor else None
    except ValueError as error:
        raise HTTPException(422, "Invalid cursor") from error
    status = """CASE WHEN q.state='queued' THEN 'queued'
      WHEN q.state='leased' THEN 'running'
      WHEN q.state='cancelled' THEN 'cancelled'
      WHEN r.state='FAILED' THEN 'failed' ELSE 'succeeded' END"""
    with TenantUnitOfWork(database, tenant) as session:
        rows = (
            session.execute(
                text(
                    _OPERATION_ROWS
                    + f" AND (CAST(:after AS uuid) IS NULL OR q.id>CAST(:after AS uuid))"
                    f" AND (CAST(:state AS text) IS NULL OR {status}=CAST(:state AS text))"
                    " ORDER BY q.id LIMIT :limit"
                ),
                {"tenant": tenant, "after": after, "state": state, "limit": limit + 1},
            )
            .mappings()
            .all()
        )
    page = rows[:limit]
    return Page[Operation](
        items=[_operation(dict(row)) for row in page],
        next_cursor=str(page[-1]["id"]) if len(rows) > limit else None,
        page_size=limit,
    )


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


@router.get("/source-health", response_model=Page[SourceHealthView])
def source_health(
    organization_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    provider_id: str | None = Query(default=None, min_length=1, max_length=128),
    cursor: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=20, ge=1, le=100),
) -> Page[SourceHealthView]:
    """Show the latest bounded access probe for each tenant distribution."""
    database = engine(request)
    tenant, roles = resolve_tenant(database, principal, organization_id)
    try:
        principal.require("source:manage", roles)
    except PermissionError as error:
        raise HTTPException(403, "Source management scope required") from error
    try:
        after = UUID(cursor) if cursor else None
    except ValueError as error:
        raise HTTPException(422, "Invalid cursor") from error
    with TenantUnitOfWork(database, tenant) as session:
        rows = (
            session.execute(
                text("""SELECT v.id AS distribution_id,p.provider_id,p.source_product_id,
                  p.status AS catalog_status,sp.status AS probe_status,
                  sp.updated_at AS last_probe_at,sp.error_code,
                  CASE WHEN sp.status='succeeded'
                    AND sp.evidence->>'scope'='metadata_access_only'
                    AND sp.evidence->>'metadata_matches_pinned_version'='true'
                    AND sp.evidence->>'scientific_status'='not_validated'
                    THEN true ELSE false END AS metadata_access_passed
                  FROM research.catalog_versions v
                  JOIN research.catalog_products p ON p.tenant_id=v.tenant_id AND p.id=v.product_id
                  LEFT JOIN LATERAL (
                    SELECT status,updated_at,error_code,evidence
                    FROM research.source_probes s
                    WHERE s.tenant_id=v.tenant_id AND s.version_id=v.id
                    ORDER BY s.created_at DESC,s.id DESC LIMIT 1
                  ) sp ON true
                  WHERE v.tenant_id=:tenant
                    AND (CAST(:provider AS text) IS NULL OR p.provider_id=:provider)
                    AND (CAST(:after AS uuid) IS NULL OR v.id>CAST(:after AS uuid))
                  ORDER BY v.id LIMIT :limit"""),
                {"tenant": tenant, "provider": provider_id, "after": after, "limit": limit + 1},
            )
            .mappings()
            .all()
        )
    page = rows[:limit]
    items = []
    for row in page:
        error_code = row["error_code"]
        safe_error: Literal["SOURCE_PROBE_REJECTED", "SOURCE_UNAVAILABLE"] | None
        if error_code == "SOURCE_PROBE_REJECTED":
            safe_error = "SOURCE_PROBE_REJECTED"
        elif error_code:
            safe_error = "SOURCE_UNAVAILABLE"
        else:
            safe_error = None
        items.append(
            SourceHealthView(
                organization_id=organization_id,
                provider_id=row["provider_id"],
                source_product_id=row["source_product_id"],
                distribution_id=row["distribution_id"],
                catalog_status=row["catalog_status"],
                probe_status=row["probe_status"] or "never_checked",
                last_probe_at=row["last_probe_at"],
                error_code=safe_error,
                metadata_access_passed=row["metadata_access_passed"],
            )
        )
    return Page[SourceHealthView](
        items=items,
        next_cursor=str(page[-1]["distribution_id"]) if len(rows) > limit else None,
        page_size=limit,
    )


@usage_router.get("/{org_id}/usage", response_model=UsageSummary)
def organization_usage(
    org_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    window: Literal["24h", "7d", "30d"],
    group_by: Literal["organization"] = "organization",
) -> UsageSummary:
    """Return recorded run-budget ceilings without inventing actual spend."""
    database = engine(request)
    tenant, roles = resolve_tenant(database, principal, org_id)
    try:
        principal.require("org:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Organization read scope required") from error
    observed_at = datetime.now(timezone.utc)
    duration = {"24h": timedelta(days=1), "7d": timedelta(days=7), "30d": timedelta(days=30)}
    window_start = observed_at - duration[window]
    with TenantUnitOfWork(database, tenant) as session:
        row = (
            session.execute(
                text("""SELECT count(*) AS run_reservations_created,
                  count(*) FILTER (WHERE state='reserved') AS active_reservations_created,
                  coalesce(sum(amount) FILTER (WHERE state='reserved'),0) AS reserved_ceiling,
                  coalesce(sum(amount) FILTER (WHERE state='released'),0) AS released_ceiling,
                  coalesce(sum(amount) FILTER (WHERE state='settled'),0) AS settled_ceiling
                  FROM research.run_budget_reservations
                  WHERE tenant_id=:tenant AND created_at>=:start AND created_at<:end"""),
                {"tenant": tenant, "start": window_start, "end": observed_at},
            )
            .mappings()
            .one()
        )
    return UsageSummary(
        organization_id=org_id,
        window=window,
        window_start=window_start,
        observed_at=observed_at,
        run_reservations_created=int(row["run_reservations_created"]),
        active_reservations_created=int(row["active_reservations_created"]),
        reserved_budget_ceiling_usd=format(
            Decimal(row["reserved_ceiling"]).quantize(_BUDGET_UNIT), "f"
        ),
        released_budget_ceiling_usd=format(
            Decimal(row["released_ceiling"]).quantize(_BUDGET_UNIT), "f"
        ),
        settled_budget_ceiling_usd=format(
            Decimal(row["settled_ceiling"]).quantize(_BUDGET_UNIT), "f"
        ),
    )

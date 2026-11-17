"""Purpose: expose a scoped P09 directory backed by durable service registrations.
Behavior: list service claims while withholding research eligibility until later trust gates.
Refs: main_implementation plan.md P09.1; api design.md §3.10; Phase: P09.
"""

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.contracts.common import Page
from orca.contracts.network import FederatedAgentSummary
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/network/agents", tags=["federation"])


@router.get("", response_model=Page[FederatedAgentSummary])
def list_agents(
    organization_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    capability: str | None = Query(default=None, min_length=1, max_length=100),
    provider_mode: str | None = Query(
        default=None, pattern="^(team_controlled|self_declared_external)$"
    ),
    verified_only: bool = False,
    cursor: UUID | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> Page[FederatedAgentSummary]:
    """List local registrations; verified-only is empty until identity checks exist."""
    database = engine(request)
    tenant, roles = resolve_tenant(database, principal, organization_id)
    try:
        principal.require("network:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Network read scope required") from error
    if verified_only:
        return Page[FederatedAgentSummary](items=[], next_cursor=None, page_size=limit)
    with TenantUnitOfWork(database, tenant) as session:
        rows = (
            session.execute(
                text("""SELECT r.id,r.organization_id,r.state,r.created_at,v.registration,
              (SELECT max(q.expires_at) FROM federation.agent_qualifications q
               WHERE q.tenant_id=r.tenant_id AND q.agent_id=r.id
                 AND q.binding_version=r.binding_version AND q.report->>'status'='PASS')
                 AS qualification_expires_at
              FROM federation.registrations r
              JOIN federation.registration_versions v
                ON v.tenant_id=r.tenant_id AND v.id=r.binding_version
              WHERE r.tenant_id=:tenant AND r.organization_id=:organization
                AND (CAST(:after AS uuid) IS NULL OR r.id>:after)
                AND (CAST(:capability AS text) IS NULL OR v.registration->>'capability'=:capability)
                AND (CAST(:mode AS text) IS NULL OR v.registration->>'operator_mode'=:mode)
              ORDER BY r.id LIMIT :limit"""),
                {
                    "tenant": tenant,
                    "organization": organization_id,
                    "after": cursor,
                    "capability": capability,
                    "mode": provider_mode,
                    "limit": limit + 1,
                },
            )
            .mappings()
            .all()
        )
    page = rows[:limit]
    return Page[FederatedAgentSummary](
        items=[
            FederatedAgentSummary(
                id=row["id"],
                organization_id=row["organization_id"],
                name=row["registration"]["name"],
                capability=row["registration"]["capability"],
                provider_mode=row["registration"]["operator_mode"],
                state=row["state"],
                qualification_expires_at=row["qualification_expires_at"],
                service_eligible=row["state"] == "active"
                and row["qualification_expires_at"] is not None
                and row["qualification_expires_at"] > datetime.now(timezone.utc),
            )
            for row in page
        ],
        next_cursor=str(page[-1]["id"]) if len(rows) > limit else None,
        page_size=limit,
    )

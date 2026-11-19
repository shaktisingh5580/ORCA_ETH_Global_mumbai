"""Purpose: expose a scoped P09 directory backed by durable service registrations.
Behavior: list service claims while withholding research eligibility until later trust gates.
Refs: main_implementation plan.md P09.1; api design.md §3.10; Phase: P09.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.agents import service as onboarding_service
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.agent_onboarding import EndpointCheckRateLimited
from orca.contracts.agents import AgentEndpointCheckRequest, AgentRevision
from orca.contracts.common import Page
from orca.contracts.network import (
    AgentCapabilityClaim,
    AgentEndpointCheckJob,
    AgentEndpointClaim,
    FederatedAgentDetail,
    FederatedAgentSummary,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/network/agents", tags=["federation"])


def _summary(row: dict[str, Any]) -> FederatedAgentSummary:
    registration = row["registration"]
    assert isinstance(registration, dict)
    expires = row["qualification_expires_at"]
    return FederatedAgentSummary(
        id=row["id"],
        organization_id=row["organization_id"],
        name=registration["name"],
        capability=registration["capability"],
        provider_mode=registration["operator_mode"],
        state=row["state"],
        qualification_expires_at=expires,
        service_eligible=row["state"] == "active"
        and isinstance(expires, datetime)
        and expires > datetime.now(timezone.utc),
    )


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
                AND (CAST(:capability AS text) IS NULL OR v.registration->>'capability'=:capability
                  OR jsonb_exists(v.registration->'additional_capabilities', :capability)
                  OR EXISTS (SELECT 1 FROM jsonb_array_elements(
                    coalesce(v.registration->'additional_endpoints', '[]'::jsonb)) item
                    WHERE jsonb_exists(item->'capabilities', :capability)))
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
        items=[_summary(dict(row)) for row in page],
        next_cursor=str(page[-1]["id"]) if len(rows) > limit else None,
        page_size=limit,
    )


@router.get("/{agent_id}", response_model=FederatedAgentDetail)
def get_agent(
    agent_id: UUID, organization_id: UUID, request: Request, principal: CurrentPrincipal
) -> FederatedAgentDetail:
    database = engine(request)
    tenant, roles = resolve_tenant(database, principal, organization_id)
    try:
        principal.require("network:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Network read scope required") from error
    with TenantUnitOfWork(database, tenant) as session:
        row = (
            session.execute(
                text("""SELECT r.id,r.organization_id,r.state,r.version,v.registration,
              (SELECT max(q.expires_at) FROM federation.agent_qualifications q
               WHERE q.tenant_id=r.tenant_id AND q.agent_id=r.id
                 AND q.binding_version=r.binding_version AND q.report->>'status'='PASS')
                 AS qualification_expires_at
              FROM federation.registrations r
              JOIN federation.registration_versions v
                ON v.tenant_id=r.tenant_id AND v.id=r.binding_version
              WHERE r.tenant_id=:tenant AND r.organization_id=:organization AND r.id=:agent"""),
                {"tenant": tenant, "organization": organization_id, "agent": agent_id},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(404, "Agent not found")
    registration = row["registration"]
    return FederatedAgentDetail(
        **_summary(dict(row)).model_dump(),
        version=row["version"],
        endpoint=registration["endpoint"],
        key_id=registration["key_id"],
        public_key=registration["public_key"],
        card_digest=registration["card_digest"],
        additional_capabilities=registration.get("additional_capabilities", []),
        additional_endpoints=registration.get("additional_endpoints", []),
    )


@router.patch("/{agent_id}", response_model=FederatedAgentDetail)
def revise_agent(
    agent_id: UUID,
    organization_id: UUID,
    body: AgentRevision,
    request: Request,
    principal: CurrentPrincipal,
) -> FederatedAgentDetail:
    """Replace the public binding; the onboarding service resets proof and qualification."""
    with onboarding_service(request, organization_id, principal) as app:
        app.revise(agent_id, body.expected_version, body.registration)
    return get_agent(agent_id, organization_id, request, principal)


@router.get("/{agent_id}/capabilities", response_model=Page[AgentCapabilityClaim])
def get_capabilities(
    agent_id: UUID, organization_id: UUID, request: Request, principal: CurrentPrincipal
) -> Page[AgentCapabilityClaim]:
    agent = get_agent(agent_id, organization_id, request, principal)
    declared = dict.fromkeys(
        [
            agent.capability,
            *agent.additional_capabilities,
            *(cap for endpoint in agent.additional_endpoints for cap in endpoint.capabilities),
        ]
    )
    return Page[AgentCapabilityClaim](
        items=[
            AgentCapabilityClaim(
                capability_id=capability,
                qualified_for_service=agent.service_eligible and capability == agent.capability,
            )
            for capability in declared
        ],
        next_cursor=None,
        page_size=len(declared),
    )


@router.get("/{agent_id}/endpoints", response_model=Page[AgentEndpointClaim])
def get_endpoints(
    agent_id: UUID, organization_id: UUID, request: Request, principal: CurrentPrincipal
) -> Page[AgentEndpointClaim]:
    agent = get_agent(agent_id, organization_id, request, principal)
    return Page[AgentEndpointClaim](
        items=[
            AgentEndpointClaim(
                uri=agent.endpoint,
                card_url=agent.endpoint.removesuffix("/a2a") + "/.well-known/agent-card.json",
                card_digest=agent.card_digest,
                service_eligible=agent.service_eligible,
                capabilities=[agent.capability, *agent.additional_capabilities],
            ),
            *[
                AgentEndpointClaim(
                    uri=item.endpoint,
                    card_url=item.endpoint.removesuffix("/a2a") + "/.well-known/agent-card.json",
                    card_digest=item.card_digest,
                    service_eligible=False,
                    capabilities=item.capabilities,
                    primary=False,
                )
                for item in agent.additional_endpoints
            ],
        ],
        next_cursor=None,
        page_size=1 + len(agent.additional_endpoints),
    )


@router.post("/{agent_id}/endpoint-checks", response_model=AgentEndpointCheckJob, status_code=202)
def check_endpoint(
    agent_id: UUID,
    organization_id: UUID,
    body: AgentEndpointCheckRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> AgentEndpointCheckJob:
    """Queue an observation of the current registered endpoint."""
    if not getattr(request.app.state, "agent_onboarding_enabled", False):
        raise HTTPException(503, "Agent onboarding worker is not enabled")
    try:
        with onboarding_service(request, organization_id, principal) as app:
            return AgentEndpointCheckJob.model_validate(
                app.queue_endpoint_check(agent_id, body.expected_version, body.endpoint)
            )
    except EndpointCheckRateLimited as error:
        raise HTTPException(
            429,
            "An endpoint check was requested recently or is still running",
            headers={"Retry-After": "60"},
        ) from error


@router.get("/{agent_id}/endpoint-checks/{job_id}", response_model=AgentEndpointCheckJob)
def get_endpoint_check(
    agent_id: UUID,
    job_id: UUID,
    organization_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
) -> AgentEndpointCheckJob:
    with onboarding_service(request, organization_id, principal) as app:
        return AgentEndpointCheckJob.model_validate(
            app.registration_job(agent_id, job_id, "endpoint_check")
        )

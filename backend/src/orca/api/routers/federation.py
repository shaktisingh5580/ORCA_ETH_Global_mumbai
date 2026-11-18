"""Purpose: expose a scoped P09 directory backed by durable service registrations.
Behavior: list service claims while withholding research eligibility until later trust gates.
Refs: main_implementation plan.md P09.1; api design.md §3.10; Phase: P09.
"""

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.agents import service as onboarding_service
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.agent_onboarding import validate_registered_card
from orca.contracts.agents import AgentRevision, AgentVersion
from orca.contracts.common import Page
from orca.contracts.network import (
    AgentCapabilityClaim,
    AgentEndpointClaim,
    AgentEndpointObservation,
    FederatedAgentDetail,
    FederatedAgentSummary,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.https_egress import fetch_card

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
    return Page[AgentCapabilityClaim](
        items=[
            AgentCapabilityClaim(
                capability_id=agent.capability, qualified_for_service=agent.service_eligible
            )
        ],
        next_cursor=None,
        page_size=1,
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
            )
        ],
        next_cursor=None,
        page_size=1,
    )


@router.post("/{agent_id}/endpoint-checks", response_model=AgentEndpointObservation)
def check_endpoint(
    agent_id: UUID,
    organization_id: UUID,
    body: AgentVersion,
    request: Request,
    principal: CurrentPrincipal,
) -> AgentEndpointObservation:
    """Observe the sole registered endpoint without trusting arbitrary caller URLs."""
    with onboarding_service(request, organization_id, principal) as app:
        registration = app.expected(agent_id, body.expected_version)["registration"]
    status: Literal["matches_registration", "card_mismatch", "unavailable"]
    try:
        observed = fetch_card(registration["endpoint"])
    except (OSError, ValueError, httpx.HTTPError):
        status = "unavailable"
    else:
        try:
            validate_registered_card(observed, registration)
        except ValueError:
            status = "card_mismatch"
        else:
            status = "matches_registration"
    observed_at = datetime.now(timezone.utc)
    with onboarding_service(request, organization_id, principal) as app:
        row = app.expected(agent_id, body.expected_version)
        observation = AgentEndpointObservation(
            agent_id=agent_id,
            binding_version=row["binding_version"],
            observed_at=observed_at,
            status=status,
        )
        app.event(agent_id, "endpoint_checked", observation.model_dump(mode="json"))
    return observation

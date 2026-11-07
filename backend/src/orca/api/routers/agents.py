"""Purpose: expose organization-authorized onboarding beside the current research APIs.
Behavior: keep network calls outside tenant transactions and recheck versions before accepting proofs.
Refs: institutional API/data design section 2; Phase: F02.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature
from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.network import runtime
from orca.api.routers.organizations import errors
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.agent_onboarding import Onboarding
from orca.contracts.agents import AgentDraft, AgentProof, AgentRevision, AgentVersion
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.https_egress import fetch_card

router = APIRouter(prefix="/v1/organizations/{org_id}/agents", tags=["institutional agents"])
directory_router = APIRouter(prefix="/v1/network", tags=["institutional directory"])
Idempotency = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


@contextmanager
def service(
    request: Request, organization: UUID, principal: CurrentPrincipal
) -> Iterator[Onboarding]:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, organization)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        try:
            yield Onboarding(session, tenant, organization, principal)
        except (ValueError, InvalidSignature) as error:
            raise HTTPException(
                409,
                "Agent metadata, proof or state is invalid; reload and verify the current binding",
            ) from error


def coordinator(request: Request) -> dict[str, str]:
    binding = runtime(request).configuration.coordinator
    return {"id": str(binding.id), "key_id": binding.key_id, "public_key": binding.public_key}


@directory_router.get("/institutional-agents")
def directory(request: Request, principal: CurrentPrincipal) -> list[dict[str, Any]]:
    with engine(request).connect() as connection:
        return list(
            connection.execute(text("SELECT * FROM iam.discover_institutional_agents()")).scalars()
        )


@router.get("")
def listing(org_id: UUID, request: Request, principal: CurrentPrincipal) -> list[dict[str, Any]]:
    with service(request, org_id, principal) as app:
        return app.listing()


@router.post("", status_code=201)
def create(
    org_id: UUID,
    body: AgentDraft,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Idempotency,
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.create(body, idempotency_key)


@router.get("/{agent_id}")
def read(
    org_id: UUID, agent_id: UUID, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.view(app.row(agent_id))


@router.patch("/{agent_id}")
def revise(
    org_id: UUID, agent_id: UUID, body: AgentRevision, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.revise(agent_id, body.expected_version, body.registration)


@router.post("/{agent_id}/verification-challenges")
def challenge(
    org_id: UUID, agent_id: UUID, body: AgentVersion, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        row = app.expected(agent_id, body.expected_version)
    try:
        card = fetch_card(row["registration"]["endpoint"])
    except (OSError, ValueError, httpx.HTTPError) as error:
        raise HTTPException(422, "Public HTTPS Agent Card could not be safely verified") from error
    with service(request, org_id, principal) as app:
        return app.challenge(agent_id, body.expected_version, card, coordinator(request))


@router.get("/{agent_id}/verification-challenges")
def current_challenge(
    org_id: UUID, agent_id: UUID, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.current_challenge(agent_id)


@router.post("/{agent_id}/verification-proofs")
def prove(
    org_id: UUID, agent_id: UUID, body: AgentProof, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.prove(agent_id, body)


@router.post("/{agent_id}/qualifications", status_code=202)
def qualification(
    org_id: UUID,
    agent_id: UUID,
    body: AgentVersion,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Idempotency,
) -> dict[str, Any]:
    if not getattr(request.app.state, "agent_onboarding_enabled", False):
        raise HTTPException(503, "Agent qualification worker is not enabled on this deployment")
    with service(request, org_id, principal) as app:
        return app.queue_qualification(agent_id, body.expected_version, idempotency_key)


@router.get("/{agent_id}/qualifications/{job_id}")
def job(
    org_id: UUID, agent_id: UUID, job_id: UUID, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.job(agent_id, job_id)


@router.get("/{agent_id}/qualifications")
def jobs(
    org_id: UUID, agent_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[dict[str, Any]]:
    with service(request, org_id, principal) as app:
        return app.jobs(agent_id)


@router.post("/{agent_id}/activate")
def activate(
    org_id: UUID, agent_id: UUID, body: AgentVersion, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        row = app.expected(agent_id, body.expected_version)
    try:
        from orca.infrastructure.federation.signing import digest

        if (
            digest(fetch_card(row["registration"]["endpoint"]))
            != row["registration"]["card_digest"]
        ):
            raise ValueError("Card changed")
    except (OSError, ValueError, httpx.HTTPError) as error:
        raise HTTPException(
            409, "Agent Card changed or unavailable; revise and requalify"
        ) from error
    with service(request, org_id, principal) as app:
        return app.transition(agent_id, body.expected_version, "activate")


@router.post("/{agent_id}/suspend")
def suspend(
    org_id: UUID, agent_id: UUID, body: AgentVersion, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.transition(agent_id, body.expected_version, "suspend")


@router.post("/{agent_id}/revocations")
def revoke(
    org_id: UUID, agent_id: UUID, body: AgentVersion, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, org_id, principal) as app:
        return app.transition(agent_id, body.expected_version, "revoke")


@router.get("/{agent_id}/events")
def events(
    org_id: UUID, agent_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[dict[str, Any]]:
    with service(request, org_id, principal) as app:
        return app.history(agent_id)

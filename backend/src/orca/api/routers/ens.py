"""Purpose: expose P10 ENS name observations to authorized organization operators.
Behavior: call pinned read-only verifier, fail closed on errors, and assert no research authority.
Refs: main_implementation plan.md P10.4; api design.md §3.11; Phase: P10.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.agents import service as onboarding_service
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.identity_bindings import IdentityBindings
from orca.contracts.ens import (
    CompareENSResolversRequest,
    CreateIdentityBinding,
    ENSPermissionRequest,
    ENSPermissionSnapshot,
    ENSResolutionSnapshot,
    ENSResolverBoundary,
    IdentityBindingChallenge,
    IdentityBindingEvidence,
    ResolveENSRequest,
    SubmitIdentityBinding,
)
from orca.domain.identity import Principal
from orca.infrastructure.federation.ens_observation import (
    inspect_endpoint_permissions,
    inspect_name,
    inspect_resolver_boundary,
    verify_binding_signatures,
)

router = APIRouter(prefix="/v1/identity/ens", tags=["ENS identity"])
binding_router = APIRouter(prefix="/v1/identity/agent-bindings", tags=["ENS identity"])
_RECORD_KEYS = frozenset(
    {
        "org.orca.endpoint",
        "org.orca.cardDigest",
        "org.orca.serviceKey",
        "org.orca.arkivWallet",
        "org.orca.manifestDigest",
    }
)


def _authorize(request: Request, principal: Principal, organization: UUID) -> None:
    _, roles = resolve_tenant(engine(request), principal, organization)
    try:
        principal.require("identity:manage", roles)
    except PermissionError as error:
        raise HTTPException(403, "Identity management scope required") from error


def _boundary_fields(observation: dict[str, Any]) -> dict[str, Any]:
    return {
        "ens_chain_id": observation.get("chain_id"),
        "status": observation.get("status"),
        "names": observation.get("names"),
        "observed_block": observation.get("block_number"),
        "block_hash": observation.get("block_hash"),
        "source_revision": observation.get("source_revision"),
    }


@router.post("/resolutions", response_model=ENSResolutionSnapshot)
def resolve_ens(
    body: ResolveENSRequest, request: Request, principal: CurrentPrincipal
) -> ENSResolutionSnapshot:
    """Inspect a finalized Sepolia name without converting text records to authority."""
    _authorize(request, principal, body.organization_id)
    try:
        observation = inspect_name(body.name)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            503,
            {"code": str(error), "message": "Finalized ENS observation unavailable"},
            headers={"Retry-After": "15"},
        ) from error
    records = observation.get("records")
    if not isinstance(records, dict) or not all(
        key in _RECORD_KEYS and isinstance(value, str) for key, value in records.items()
    ):
        raise HTTPException(503, "ENS observation has invalid records")
    hierarchy = observation.get("hierarchy")
    leaf = hierarchy[-1] if isinstance(hierarchy, list) and hierarchy else None
    active = observation.get("status") == "NAME_OBSERVED_NOT_AUTHORIZED" and isinstance(leaf, dict)
    leaf_records = leaf if isinstance(leaf, dict) else {}
    return ENSResolutionSnapshot(
        name=body.name,
        status=observation.get("status", "observed_unverified"),
        records=records,
        name_active=active,
        controller=leaf_records.get("owner") if active else None,
        resolver=leaf_records.get("resolver") if active else None,
        observed_block=observation.get("block_number"),
        block_hash=observation.get("block_hash"),
        source_revision=observation.get("source_revision"),
        finalized=True
        if observation.get("block_number") and observation.get("block_hash")
        else None,
    )


@router.post("/resolver-boundaries", response_model=ENSResolverBoundary)
def compare_ens_resolvers(
    body: CompareENSResolversRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> ENSResolverBoundary:
    """Report resolver addresses observed at the same finalized block."""
    _authorize(request, principal, body.organization_id)
    try:
        observation = inspect_resolver_boundary(body.first_name, body.second_name)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            503, "Finalized resolver comparison unavailable", headers={"Retry-After": "15"}
        ) from error
    try:
        return ENSResolverBoundary.model_validate(_boundary_fields(observation))
    except ValidationError as error:
        raise HTTPException(503, "Resolver comparison returned invalid evidence") from error


@router.post("/endpoint-permissions", response_model=ENSPermissionSnapshot)
def observe_ens_endpoint_permissions(
    body: ENSPermissionRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> ENSPermissionSnapshot:
    _authorize(request, principal, body.organization_id)
    try:
        observation = inspect_endpoint_permissions(body.first_name, body.second_name, body.delegate)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            503, "Finalized ENS permission observation unavailable", headers={"Retry-After": "15"}
        ) from error
    try:
        return ENSPermissionSnapshot.model_validate(
            {
                **_boundary_fields(observation),
                "delegate": observation.get("delegate"),
                "key": observation.get("key"),
                "resource": observation.get("resource"),
            }
        )
    except ValidationError as error:
        raise HTTPException(503, "Permission observation returned invalid evidence") from error


def _binding_observation(name: str) -> dict[str, Any]:
    try:
        return inspect_name(name)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(
            503, "Finalized ENS binding observation unavailable", headers={"Retry-After": "15"}
        ) from error


@binding_router.post("/challenges", response_model=IdentityBindingChallenge, status_code=201)
def create_binding_challenge(
    body: CreateIdentityBinding,
    request: Request,
    principal: CurrentPrincipal,
) -> IdentityBindingChallenge:
    _authorize(request, principal, body.organization_id)
    with onboarding_service(request, body.organization_id, principal) as onboarding:
        onboarding.expected(body.agent_id, body.expected_version)
    observation = _binding_observation(body.name)
    with onboarding_service(request, body.organization_id, principal) as onboarding:
        return IdentityBindings(onboarding).create(body, observation)


@binding_router.post("", response_model=IdentityBindingEvidence, status_code=201)
def submit_binding_challenge(
    body: SubmitIdentityBinding,
    request: Request,
    principal: CurrentPrincipal,
) -> IdentityBindingEvidence:
    _authorize(request, principal, body.organization_id)
    with onboarding_service(request, body.organization_id, principal) as onboarding:
        manifest = IdentityBindings(onboarding).pending(body.challenge_id)["manifest"]
    try:
        evidence = verify_binding_signatures(
            manifest,
            {
                "controller": body.controller,
                "wallet": body.wallet,
                "service": body.service,
            },
        )
    except PermissionError as error:
        raise HTTPException(403, "Identity binding signatures rejected") from error
    except RuntimeError as error:
        raise HTTPException(503, "Identity binding verifier unavailable") from error
    observation = _binding_observation(manifest["ensName"])
    with onboarding_service(request, body.organization_id, principal) as onboarding:
        return IdentityBindings(onboarding).consume(body.challenge_id, observation, evidence)


@binding_router.get("/{challenge_id}", response_model=IdentityBindingEvidence)
def get_binding_evidence(
    challenge_id: UUID,
    organization_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
) -> IdentityBindingEvidence:
    _authorize(request, principal, organization_id)
    with onboarding_service(request, organization_id, principal) as onboarding:
        return IdentityBindings(onboarding).evidence(challenge_id)

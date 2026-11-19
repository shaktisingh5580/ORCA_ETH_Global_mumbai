"""Purpose: expose P10 ENS name observations to authorized organization operators.
Behavior: call pinned read-only verifier, fail closed on errors, and assert no research authority.
Refs: main_implementation plan.md P10.4; api design.md §3.11; Phase: P10.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.contracts.ens import (
    CompareENSResolversRequest,
    ENSResolutionSnapshot,
    ENSResolverBoundary,
    ResolveENSRequest,
)
from orca.domain.identity import Principal
from orca.infrastructure.federation.ens_observation import inspect_name, inspect_resolver_boundary

router = APIRouter(prefix="/v1/identity/ens", tags=["ENS identity"])
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
        return ENSResolverBoundary.model_validate(
            {
                "ens_chain_id": observation.get("chain_id"),
                "status": observation.get("status"),
                "names": observation.get("names"),
                "observed_block": observation.get("block_number"),
                "block_hash": observation.get("block_hash"),
                "source_revision": observation.get("source_revision"),
            }
        )
    except ValidationError as error:
        raise HTTPException(503, "Resolver comparison returned invalid evidence") from error

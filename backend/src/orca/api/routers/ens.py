"""Purpose: expose P10 ENS name observations to authorized organization operators.
Behavior: call pinned read-only verifier, fail closed on errors, and assert no research authority.
Refs: main_implementation plan.md P10.4; api design.md §3.11; Phase: P10.
"""

from fastapi import APIRouter, HTTPException, Request

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.contracts.ens import ENSResolutionSnapshot, ResolveENSRequest
from orca.infrastructure.federation.ens_observation import inspect_name

router = APIRouter(prefix="/v1/identity/ens", tags=["ENS identity"])


@router.post("/resolutions", response_model=ENSResolutionSnapshot)
def resolve_ens(
    body: ResolveENSRequest, request: Request, principal: CurrentPrincipal
) -> ENSResolutionSnapshot:
    """Inspect a finalized Sepolia name without converting text records to authority."""
    _, roles = resolve_tenant(engine(request), principal, body.organization_id)
    try:
        principal.require("identity:manage", roles)
    except PermissionError as error:
        raise HTTPException(403, "Identity management scope required") from error
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
        isinstance(key, str) and isinstance(value, str) for key, value in records.items()
    ):
        raise HTTPException(503, "ENS observation has invalid records")
    return ENSResolutionSnapshot(
        name=body.name,
        status=observation.get("status", "observed_unverified"),
        records=records,
        observed_block=observation.get("block_number"),
        finalized=True
        if observation.get("block_number") and observation.get("block_hash")
        else None,
    )

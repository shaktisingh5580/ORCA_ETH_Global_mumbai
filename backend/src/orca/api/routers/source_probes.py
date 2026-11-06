"""Purpose: expose operator-authorized durable source metadata checks.
Behavior: require source:manage, configured distribution bindings and idempotent submission.
Refs: API 3.4 source-probes; main plan P03.2/P03.4; Phase: P03.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.projects import IdempotencyConflict
from orca.application.source_probes import SourceProbes
from orca.contracts.common import Operation
from orca.contracts.source_probes import ProbeRequest, SourceProbeStatus
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/catalog/source-probes", tags=["operator source probes"])


def authorize(request: Request, principal: CurrentPrincipal, organization_id: UUID) -> UUID:
    tenant, roles = resolve_tenant(engine(request), principal, organization_id)
    try:
        principal.require("source:manage", roles)
    except PermissionError as exc:
        raise HTTPException(403, "Source management scope required") from exc
    return tenant


@router.post("", response_model=Operation, status_code=202)
def create_probe(
    body: ProbeRequest,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> Operation:
    tenant = authorize(request, principal, body.organization_id)
    if not getattr(request.app.state, "source_probes_enabled", False):
        raise HTTPException(503, "Source probe worker is not configured")
    if not any(
        b.tenant_id == tenant and b.version_id == body.distribution_id
        for b in request.app.state.acquisition_sources
    ):
        raise HTTPException(422, "Distribution is not configured")
    try:
        with TenantUnitOfWork(engine(request), tenant) as session:
            return SourceProbes(session, tenant).create(body, idempotency_key).operation
    except LookupError as exc:
        raise HTTPException(404, "Distribution not found") from exc
    except IdempotencyConflict as exc:
        raise HTTPException(409, "Idempotency key already used") from exc


@router.get("/{probe_id}", response_model=SourceProbeStatus)
def get_probe(
    probe_id: UUID, organization_id: UUID, request: Request, principal: CurrentPrincipal
) -> SourceProbeStatus:
    tenant = authorize(request, principal, organization_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        result = SourceProbes(session, tenant).get(probe_id)
        if result is None:
            raise HTTPException(404, "Source probe not found")
        return result

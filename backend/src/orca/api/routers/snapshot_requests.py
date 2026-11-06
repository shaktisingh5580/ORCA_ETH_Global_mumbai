"""Purpose: expose durable snapshot request submission and authorized status reads.
Behavior: reject submissions until an acquisition worker is configured; never fake success.
Refs: API 3.4; main plan P03.5; Phase: P03.5.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.catalog import authorize
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.projects import IdempotencyConflict
from orca.application.snapshot_requests import SnapshotRequests
from orca.contracts.common import Operation
from orca.contracts.snapshot_requests import SnapshotRequest, SnapshotRequestStatus
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/catalog/snapshot-requests", tags=["snapshot requests"])


@router.post("", response_model=Operation, status_code=202)
def create_request(
    body: SnapshotRequest,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> Operation:
    db = engine(request)
    tenant, roles = resolve_tenant(db, principal, body.organization_id)
    try:
        principal.require("research:plan", roles)
    except PermissionError as exc:
        raise HTTPException(403, "Research planning scope required") from exc
    if not getattr(request.app.state, "snapshot_worker_enabled", False):
        raise HTTPException(503, "Snapshot acquisition worker is not configured")
    try:
        with TenantUnitOfWork(db, tenant) as session:
            return SnapshotRequests(session, tenant).create(body, idempotency_key).operation
    except LookupError as exc:
        raise HTTPException(404, "Product version not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, "Source preservation not permitted") from exc
    except IdempotencyConflict as exc:
        raise HTTPException(409, "Idempotency key already used for a different request") from exc
    except ValueError as exc:
        raise HTTPException(422, "Request does not match approved product metadata") from exc


@router.get("/{request_id}", response_model=SnapshotRequestStatus)
def get_request(
    request_id: UUID, request: Request, principal: CurrentPrincipal, organization_id: UUID
) -> SnapshotRequestStatus:
    tenant = authorize(request, principal, organization_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        result = SnapshotRequests(session, tenant).get(request_id)
        if result is None:
            raise HTTPException(404, "Snapshot request not found")
        return result

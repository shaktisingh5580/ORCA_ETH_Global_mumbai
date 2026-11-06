"""Purpose: expose tenant-authorized P03 snapshot metadata and provenance reads.
Behavior: paginate immutable records, conceal inaccessible IDs and never serve raw bytes.
Refs: API 3.4; P03-snapshot-read-contract.json; Phase: P03.5.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.catalog import authorize
from orca.api.routers.projects import engine
from orca.application.snapshots import SnapshotRepository
from orca.contracts.common import Digest, Page
from orca.contracts.snapshots import DatasetSnapshot, DatasetSnapshotManifest
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/catalog/snapshots", tags=["snapshots"])


@router.get("", response_model=Page[DatasetSnapshot])
def list_snapshots(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    product_id: UUID | None = None,
    digest: Digest | None = None,
    project_id: UUID | None = None,
) -> Page[DatasetSnapshot]:
    tenant = authorize(request, principal, organization_id)
    if set(request.query_params) - {
        "organization_id",
        "limit",
        "cursor",
        "product_id",
        "digest",
        "project_id",
    }:
        raise HTTPException(422, "Unsupported snapshot filter")
    with TenantUnitOfWork(engine(request), tenant) as session:
        repository = SnapshotRepository(session, tenant)
        if project_id is not None and not repository.project_member(project_id, principal.subject):
            raise HTTPException(404, "Project not found")
        rows = repository.list(
            after=cursor,
            limit=limit + 1,
            product_id=product_id,
            digest=digest,
            project_id=project_id,
        )
        return Page[DatasetSnapshot](
            items=[DatasetSnapshot.from_manifest(row) for row in rows[:limit]],
            next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
            page_size=limit,
        )


@router.get("/{snapshot_id}/manifest", response_model=DatasetSnapshotManifest)
def get_manifest(
    snapshot_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> DatasetSnapshotManifest:
    tenant = authorize(request, principal, organization_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        manifest = SnapshotRepository(session, tenant).get(snapshot_id)
        if manifest is None:
            raise HTTPException(404, "Snapshot not found")
        return manifest


@router.get("/{snapshot_id}", response_model=DatasetSnapshot)
def get_snapshot(
    snapshot_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> DatasetSnapshot:
    return DatasetSnapshot.from_manifest(
        get_manifest(snapshot_id, request, principal, organization_id)
    )

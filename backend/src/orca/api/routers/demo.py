"""Purpose: connect authorized snapshots to reproducible results and minimal public Arkiv exports.
Behavior: guard demo enablement, verify source bytes and persist immutable private result manifests.
Refs: accelerated-research-arkiv-demo.json; Phase: accelerated P04/P06/P11 demo.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.catalog import authorize
from orca.api.routers.fitness import planning_tenant
from orca.api.routers.projects import engine
from orca.application.demo import analyze_snapshot, result_digest
from orca.application.snapshots import SnapshotRepository
from orca.contracts.demo import DemoResult, DemoRunRequest, PublicDemoResult
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.storage.snapshot_s3 import SnapshotS3

router = APIRouter(prefix="/v1/demo/research-runs", tags=["accelerated research demo"])


def enabled(request: Request) -> SnapshotS3:
    store = getattr(request.app.state, "demo_snapshot_store", None)
    if store is None:
        raise HTTPException(503, "Research demo is not configured")
    return store  # type: ignore[no-any-return]


@router.post("", response_model=DemoResult)
def create_run(body: DemoRunRequest, request: Request, principal: CurrentPrincipal) -> DemoResult:
    store = enabled(request)
    tenant = planning_tenant(request, principal, body.organization_id)
    if authorize(request, principal, body.organization_id) != tenant:
        raise HTTPException(403, "Tenant mismatch")
    db = engine(request)
    with TenantUnitOfWork(db, tenant) as session:
        manifest = SnapshotRepository(session, tenant).get(body.snapshot_id)
        if manifest is None:
            raise HTTPException(404, "Snapshot not found")
    try:
        content = store.read(tenant, manifest.raw_content_digest)
        result = analyze_snapshot(manifest, content)
    except ValueError as exc:
        raise HTTPException(
            422, "Snapshot cannot be analyzed by the descriptive demo recipe"
        ) from exc
    except Exception as exc:
        raise HTTPException(503, "Private snapshot storage unavailable") from exc
    with TenantUnitOfWork(db, tenant) as session:
        session.execute(
            text("""INSERT INTO research.demo_results(id,tenant_id,snapshot_id,result_digest,manifest)
          VALUES(:id,:tenant,:snapshot,:digest,CAST(:manifest AS jsonb)) ON CONFLICT(id) DO NOTHING"""),
            {
                "id": result.id,
                "tenant": tenant,
                "snapshot": manifest.id,
                "digest": result_digest(result),
                "manifest": result.model_dump_json(),
            },
        )
        stored: Any = session.execute(
            text(
                "SELECT result_digest FROM research.demo_results WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": tenant, "id": result.id},
        ).scalar_one()
        if stored != result_digest(result):
            raise HTTPException(409, "Existing immutable result differs from this computation")
    return result


@router.get("/{result_id}", response_model=DemoResult)
def get_run(
    result_id: UUID, organization_id: UUID, request: Request, principal: CurrentPrincipal
) -> DemoResult:
    enabled(request)
    tenant = authorize(request, principal, organization_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        row: Any = (
            session.execute(
                text(
                    "SELECT manifest,result_digest FROM research.demo_results WHERE tenant_id=:tenant AND id=:id"
                ),
                {"tenant": tenant, "id": result_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, "Research result not found")
        result = DemoResult.model_validate(row["manifest"])
        if result.id != result_id or result_digest(result) != row["result_digest"]:
            raise HTTPException(500, "Stored result integrity check failed")
        return result


@router.post("/{result_id}/public-export", response_model=PublicDemoResult)
def public_export(
    result_id: UUID, body: DemoRunRequest, request: Request, principal: CurrentPrincipal
) -> PublicDemoResult:
    """Calling this endpoint explicitly approves disclosure of the returned minimal reference."""
    tenant = planning_tenant(request, principal, body.organization_id)
    result = get_run(result_id, body.organization_id, request, principal)
    if result.snapshot_id != body.snapshot_id:
        raise HTTPException(422, "Snapshot does not match result")
    with TenantUnitOfWork(engine(request), tenant) as session:
        snapshot = SnapshotRepository(session, tenant).get(result.snapshot_id)
        if snapshot is None or not snapshot.product_version.license.can_redistribute:
            raise HTTPException(403, "Stored source rights do not permit public disclosure")
    return PublicDemoResult(result_id=result.id, result_digest=result_digest(result))

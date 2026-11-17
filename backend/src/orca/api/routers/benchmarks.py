"""Purpose: read P07 frozen benchmark definitions under project authority.
Behavior: expose metadata only; held-out reference values remain outside these routes.
Refs: main_implementation plan.md P07.4; api design.md §§3.8, 4.5; Phase: P07.
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_project
from orca.application.workspace_access import authorize_project
from orca.contracts.benchmarks import BenchmarkDefinitionView
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/research/benchmarks", tags=["benchmarks"])


def _project_session(request: Request, principal: CurrentPrincipal, project_id: UUID):
    database = engine(request)
    tenant, _ = resolve_project(database, principal, project_id)
    return database, tenant


@router.get("", response_model=list[BenchmarkDefinitionView])
def list_benchmarks(
    request: Request,
    principal: CurrentPrincipal,
    project_id: UUID,
    domain: str | None = Query(default=None, max_length=80),
    method: str | None = Query(default=None, max_length=128),
    after: UUID | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[BenchmarkDefinitionView]:
    database, tenant = _project_session(request, principal, project_id)
    with TenantUnitOfWork(database, tenant) as session:
        try:
            authorize_project(session, tenant, project_id, principal, "project:read")
        except (LookupError, PermissionError) as error:
            raise HTTPException(404, "Project not found") from error
        rows = (
            session.execute(
                text("""SELECT id,project_id,title,domain,method_id,version,
              frozen_test_set_digest,reference_policy_digest,reference_kind,synthetic,created_at
              FROM research.benchmark_definitions
              WHERE tenant_id=:tenant AND project_id=:project
                AND (:domain IS NULL OR domain=:domain)
                AND (:method IS NULL OR method_id=:method)
                AND (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid))
              ORDER BY id LIMIT :limit"""),
                {
                    "tenant": tenant,
                    "project": project_id,
                    "domain": domain,
                    "method": method,
                    "after": after,
                    "limit": limit,
                },
            )
            .mappings()
            .all()
        )
        return [BenchmarkDefinitionView.model_validate(row) for row in rows]


@router.get("/{benchmark_id}", response_model=BenchmarkDefinitionView)
def get_benchmark(
    benchmark_id: UUID, request: Request, principal: CurrentPrincipal, project_id: UUID
) -> BenchmarkDefinitionView:
    database, tenant = _project_session(request, principal, project_id)
    with TenantUnitOfWork(database, tenant) as session:
        try:
            authorize_project(session, tenant, project_id, principal, "project:read")
        except (LookupError, PermissionError) as error:
            raise HTTPException(404, "Project not found") from error
        row = (
            session.execute(
                text("""SELECT id,project_id,title,domain,method_id,version,
              frozen_test_set_digest,reference_policy_digest,reference_kind,synthetic,created_at
              FROM research.benchmark_definitions
              WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""),
                {"tenant": tenant, "project": project_id, "id": benchmark_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, "Benchmark not found")
        return BenchmarkDefinitionView.model_validate(row)

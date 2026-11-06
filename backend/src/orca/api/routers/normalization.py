"""Purpose: expose project-authorized CSV/JSON normalization alongside existing research APIs.
Behavior: separate previews from explicit immutable approvals and retain existing authentication.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A01; Phase: A01.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_project
from orca.api.routers.prototype import workflow_errors
from orca.application.normalization import TabularEvidence
from orca.contracts.normalization import (
    DatasetInput,
    DatasetView,
    Mapping,
    NormalizationView,
    NormalizeRequest,
)
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/projects/{project_id}/datasets", tags=["tabular evidence"])


@contextmanager
def service(request: Request, project_id: UUID, principal: Principal) -> Iterator[TabularEvidence]:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        try:
            yield TabularEvidence(session, tenant, project_id, principal)
        except LookupError as error:
            raise HTTPException(404, "Dataset not found") from error


@router.post("", response_model=DatasetView, status_code=201)
def create(
    project_id: UUID, body: DatasetInput, request: Request, principal: CurrentPrincipal
) -> DatasetView:
    with service(request, project_id, principal) as app:
        return app.create(body)


@router.get("", response_model=list[DatasetView])
def listing(project_id: UUID, request: Request, principal: CurrentPrincipal) -> list[DatasetView]:
    with service(request, project_id, principal) as app:
        return app.list_datasets()


@router.post("/{dataset_id}/preview")
def preview(
    project_id: UUID, dataset_id: UUID, body: Mapping, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, project_id, principal) as app:
        return app.preview(dataset_id, body)


@router.post("/{dataset_id}/normalizations", response_model=NormalizationView, status_code=201)
def approve(
    project_id: UUID,
    dataset_id: UUID,
    body: NormalizeRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> NormalizationView:
    with service(request, project_id, principal) as app:
        return app.approve(dataset_id, body.mapping)


@router.get("/{dataset_id}/normalizations", response_model=list[NormalizationView])
def normalizations(
    project_id: UUID, dataset_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[NormalizationView]:
    with service(request, project_id, principal) as app:
        return app.normalizations(dataset_id)

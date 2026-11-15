"""Purpose: expose P05 investigation draft, plan and exact-digest approval APIs.
Behavior: resolve the tenant server-side, enforce project scopes and return explicit plan readiness.
Refs: main_implementation plan.md P05.1; api design.md §§3.6, 5.1; Phase: P05.
"""

from decimal import Decimal
from typing import Annotated, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_project
from orca.application.investigations import Investigations
from orca.application.planning import compile_typed_plan
from orca.application.projects import IdempotencyConflict, PreconditionRequired, StaleVersion
from orca.contracts.common import Operation, Page, ResourceRef
from orca.contracts.investigations import (
    ApprovedPlanView,
    ApprovePlan,
    CreateInvestigation,
    InvestigationPlanView,
    InvestigationView,
    PatchInvestigation,
)
from orca.contracts.research_plans import PlanRequest
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/research/investigations", tags=["investigations"])

# P05 has no organization spending policy yet. This deliberately conservative
# application ceiling bounds a plan request; P05.2 replaces it with atomic
# organization budget reservations before any plan can be run.
P05_PLAN_MAX_BUDGET = Decimal("100.00")
InvestigationState = Literal["DRAFT", "PLAN_PENDING", "PLAN_READY", "APPROVED"]


def investigation_context(db: Engine, principal: Principal, identifier: UUID) -> tuple[UUID, UUID]:
    """Resolve a tenant through the SECURITY DEFINER function, never a client header."""
    with db.connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_investigation_tenant(:subject,:investigation)"),
            {"subject": principal.subject, "investigation": identifier},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Investigation not found")
    resolved_tenant = cast(UUID, tenant)
    with TenantUnitOfWork(db, resolved_tenant) as session:
        project = session.execute(
            text(
                "SELECT project_id FROM research.investigations WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": resolved_tenant, "id": identifier},
        ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Investigation not found")
    return resolved_tenant, cast(UUID, project)


def raise_service_error(error: Exception) -> NoReturn:
    if isinstance(error, LookupError):
        raise HTTPException(404, str(error)) from error
    if isinstance(error, PermissionError):
        raise HTTPException(403, "Investigation action not permitted") from error
    if isinstance(error, PreconditionRequired):
        raise HTTPException(428, "If-Match is required") from error
    if isinstance(error, StaleVersion):
        raise HTTPException(412, "Investigation plan version is stale") from error
    if isinstance(error, IdempotencyConflict):
        raise HTTPException(409, "Idempotency key was used with another request") from error
    if isinstance(error, IntegrityError):
        raise HTTPException(409, "Investigation conflicts with an existing resource") from error
    if isinstance(error, ValueError):
        status = 422 if "budget" in str(error).lower() else 409
        raise HTTPException(status, str(error)) from error
    raise error


@router.get("", response_model=Page[InvestigationView])
def list_investigations(
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    state: InvestigationState | None = None,
) -> Page[InvestigationView]:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            return Investigations(session, tenant, project_id, principal).list_investigations(
                limit=limit, cursor=cursor, state=state
            )
    except Exception as error:
        raise_service_error(error)


@router.post("", response_model=InvestigationView, status_code=201)
def create_investigation(
    body: CreateInvestigation,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> InvestigationView:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, body.project_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, body.project_id, principal).create(
                body, idempotency_key
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["Location"] = f"/v1/research/investigations/{result.id}"
    response.headers["ETag"] = result.etag
    return result


@router.get("/{investigation_id}", response_model=InvestigationView)
def get_investigation(
    investigation_id: UUID, request: Request, response: Response, principal: CurrentPrincipal
) -> InvestigationView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).get(investigation_id)
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = result.etag
    return result


@router.patch("/{investigation_id}", response_model=InvestigationView)
def patch_investigation(
    investigation_id: UUID,
    body: PatchInvestigation,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> InvestigationView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).patch(
                investigation_id, body, if_match
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = result.etag
    return result


@router.post("/{investigation_id}/plans", response_model=Operation, status_code=202)
def create_plan(
    investigation_id: UUID,
    body: PlanRequest,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> Operation:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            service = Investigations(session, tenant, project, principal)
            investigation = service.get(investigation_id)
            candidate = compile_typed_plan(body, revision=investigation.current_plan_revision + 1)
            result = service.create_plan(
                investigation_id,
                candidate,
                idempotency_key,
                maximum_budget=P05_PLAN_MAX_BUDGET,
            )
    except Exception as error:
        raise_service_error(error)
    return Operation(
        id=result.id,
        kind="plan",
        status="succeeded",
        resource_ref=ResourceRef(
            type="investigation_plan",
            id=str(result.id),
            version=str(result.revision),
            digest=result.plan_digest,
        ),
        created_at=result.created_at,
        updated_at=result.created_at,
        progress=1.0,
        links={"plan": f"/v1/research/investigations/{investigation_id}/plans/{result.id}"},
    )


@router.get("/{investigation_id}/plans", response_model=Page[InvestigationPlanView])
def list_plans(
    investigation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[int | None, Query(ge=0)] = None,
) -> Page[InvestigationPlanView]:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            plans = Investigations(session, tenant, project, principal).plans(investigation_id)
    except Exception as error:
        raise_service_error(error)
    after = cursor or 0
    candidates = [plan for plan in plans if plan.revision > after]
    items = candidates[:limit]
    return Page(
        items=items,
        page_size=limit,
        next_cursor=str(items[-1].revision) if len(candidates) > limit and items else None,
    )


@router.get("/{investigation_id}/plans/{plan_id}", response_model=InvestigationPlanView)
def get_plan(
    investigation_id: UUID,
    plan_id: UUID,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
) -> InvestigationPlanView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).get_plan(
                investigation_id, plan_id
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = result.etag
    return result


@router.post("/{investigation_id}/plans/{plan_id}/approve", response_model=ApprovedPlanView)
def approve_plan(
    investigation_id: UUID,
    plan_id: UUID,
    body: ApprovePlan,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    if_match: Annotated[str | None, Header()] = None,
) -> ApprovedPlanView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).approve_plan(
                investigation_id, plan_id, body, if_match, idempotency_key
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = if_match or ""
    return result

"""Purpose: expose implemented tenant project operations with OIDC, RLS and ETags.
Behavior: resolve tenant from membership, require scopes, and conceal unauthorized resources.
Refs: API section 3.3; main plan P02.4; Phase: P02.
"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import Engine, select, text

from orca.api.dependencies import CurrentPrincipal
from orca.application.projects import (
    IdempotencyConflict,
    PreconditionRequired,
    ProjectService,
    StaleVersion,
)
from orca.contracts.projects import CreateProject, PatchProject
from orca.domain.identity import Principal
from orca.infrastructure.db.models import Membership, Project
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/research", tags=["projects"])


def engine(request: Request) -> Engine:
    value = getattr(request.app.state, "runtime_engine", None)
    if value is None:
        raise HTTPException(503, "Database is not configured")
    return cast(Engine, value)


def resolve_tenant(
    db: Engine, principal: Principal, organization_id: UUID
) -> tuple[UUID, set[str]]:
    with db.connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_tenant(:subject,:organization)"),
            {"subject": principal.subject, "organization": organization_id},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Organization not found")
    with TenantUnitOfWork(db, tenant) as session:
        roles = set(
            session.scalars(
                select(Membership.role).where(
                    Membership.subject == principal.subject, Membership.status == "active"
                )
            )
        )
    return tenant, roles


def resolve_project(db: Engine, principal: Principal, project_id: UUID) -> tuple[UUID, set[str]]:
    with db.connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_project_tenant(:subject,:project)"),
            {"subject": principal.subject, "project": project_id},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Project not found")
    with TenantUnitOfWork(db, tenant) as session:
        roles = set(
            session.scalars(
                select(Membership.role).where(
                    Membership.subject == principal.subject, Membership.status == "active"
                )
            )
        )
    return tenant, roles


def project_json(project: Project) -> dict[str, object]:
    return {
        "id": str(project.id),
        "organization_id": str(project.tenant_id),
        "name": project.name,
        "description": project.description,
        "access_class": project.access_class,
        "state": project.state,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "etag": f'"{project.version}"',
    }


@router.get("/projects")
def list_projects(
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> dict[str, object]:
    db = engine(request)
    tenant, roles = resolve_tenant(db, principal, organization_id)
    principal.require("project:read", roles)
    with TenantUnitOfWork(db, tenant) as session:
        page = ProjectService().list(session, tenant, limit, cursor)
        return {
            "items": [project_json(item) for item in page.items],
            "next_cursor": page.next_cursor,
            "page_size": limit,
        }


@router.post("/projects", status_code=201)
def create_project(
    body: CreateProject,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, object]:
    db = engine(request)
    tenant, roles = resolve_tenant(db, principal, body.organization_id)
    principal.require("project:write", roles)
    payload: dict[str, object] = {
        "slug": body.slug,
        "name": body.name,
        "description": body.description,
        "access_class": body.visibility,
    }
    try:
        with TenantUnitOfWork(db, tenant) as session:
            project = ProjectService().create(session, tenant, payload, idempotency_key)
            result = project_json(project)
    except IdempotencyConflict as exc:
        raise HTTPException(409, "Idempotency key was used with another request") from exc
    response.headers["Location"] = f"/v1/research/projects/{result['id']}"
    response.headers["ETag"] = str(result["etag"])
    return result


@router.get("/projects/{project_id}")
def get_project(
    project_id: UUID, request: Request, response: Response, principal: CurrentPrincipal
) -> dict[str, object]:
    db = engine(request)
    tenant, roles = resolve_project(db, principal, project_id)
    principal.require("project:read", roles)
    with TenantUnitOfWork(db, tenant) as session:
        project = session.get(Project, project_id)
        if project is None:
            raise HTTPException(404, "Project not found")
        result = project_json(project)
    response.headers["ETag"] = str(result["etag"])
    return result


@router.patch("/projects/{project_id}")
def patch_project(
    project_id: UUID,
    body: PatchProject,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    db = engine(request)
    tenant, roles = resolve_project(db, principal, project_id)
    principal.require("project:write", roles)
    changes = body.model_dump(exclude_none=True)
    if "visibility" in changes:
        changes["access_class"] = changes.pop("visibility")
    try:
        with TenantUnitOfWork(db, tenant) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "Project not found")
            result = project_json(ProjectService().update(session, project, changes, if_match))
    except PreconditionRequired as exc:
        raise HTTPException(428, "If-Match is required") from exc
    except StaleVersion as exc:
        raise HTTPException(412, "Project version is stale") from exc
    response.headers["ETag"] = str(result["etag"])
    return result


@router.post("/projects/{project_id}/archive")
def archive_project(
    project_id: UUID,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> dict[str, object]:
    db = engine(request)
    tenant, roles = resolve_project(db, principal, project_id)
    principal.require("project:write", roles)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            project = session.get(Project, project_id)
            if project is None:
                raise HTTPException(404, "Project not found")
            result = project_json(
                ProjectService().update(session, project, {"state": "archived"}, if_match)
            )
    except PreconditionRequired as exc:
        raise HTTPException(428, "If-Match is required") from exc
    except StaleVersion as exc:
        raise HTTPException(412, "Project version is stale") from exc
    response.headers["ETag"] = str(result["etag"])
    return result

"""Purpose: expose versioned project membership and bounded organization policy APIs.
Behavior: conceal inaccessible projects, require live authority and serialize policy changes.
Refs: API 3.2/3.3; main plan P02; Phase: P02 completion repair.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.organizations import errors
from orca.api.routers.projects import engine, resolve_project, resolve_tenant
from orca.application.organizations import Organizations
from orca.application.workspace_access import (
    ProjectMembers,
    authorize_project,
    get_policy,
    update_policy,
)
from orca.contracts.common import Page
from orca.contracts.workspace_access import (
    AddProjectMember,
    OrgPolicy,
    PatchOrgPolicy,
    PatchProjectMember,
    ProjectMemberDetail,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1", tags=["workspace access"])


@router.get("/organizations/{org_id}/policies", response_model=OrgPolicy)
def read_policy(
    org_id: UUID, request: Request, response: Response, principal: CurrentPrincipal
) -> OrgPolicy:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        Organizations(session, tenant, org_id).authorize(principal, "org:read")
        result = get_policy(session, tenant)
    response.headers["ETag"] = result.etag
    return result


@router.patch("/organizations/{org_id}/policies", response_model=OrgPolicy)
def patch_policy(
    org_id: UUID,
    body: PatchOrgPolicy,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> OrgPolicy:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        Organizations(session, tenant, org_id).authorize(principal, "org:write")
        result = update_policy(session, tenant, body, if_match)
    response.headers["ETag"] = result.etag
    return result


@router.get("/research/projects/{project_id}/members", response_model=Page[ProjectMemberDetail])
def list_project_members(
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> Page[ProjectMemberDetail]:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        authorize_project(session, tenant, project_id, principal, "project:read")
        rows = ProjectMembers(session, tenant, project_id).list(cursor, limit + 1)
        return Page[ProjectMemberDetail](
            items=rows[:limit],
            page_size=limit,
            next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
        )


@router.post(
    "/research/projects/{project_id}/members", response_model=ProjectMemberDetail, status_code=201
)
def add_project_member(
    project_id: UUID,
    body: AddProjectMember,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
) -> ProjectMemberDetail:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        try:
            result = ProjectMembers(session, tenant, project_id).add(body, principal)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    response.headers["ETag"] = result.etag
    return result


@router.patch(
    "/research/projects/{project_id}/members/{member_id}", response_model=ProjectMemberDetail
)
def patch_project_member(
    project_id: UUID,
    member_id: UUID,
    body: PatchProjectMember,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> ProjectMemberDetail:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        try:
            result = ProjectMembers(session, tenant, project_id).change(
                member_id, body, if_match, principal
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    response.headers["ETag"] = result.etag
    return result


@router.delete("/research/projects/{project_id}/members/{member_id}", status_code=204)
def revoke_project_member(
    project_id: UUID,
    member_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> Response:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        ProjectMembers(session, tenant, project_id).change(
            member_id, PatchProjectMember(status="revoked"), if_match, principal
        )
    return Response(status_code=204)

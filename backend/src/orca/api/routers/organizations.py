"""Purpose: expose the real organization lifecycle and controlled membership administration.
Behavior: recheck authorization under organization locks, use ETags and conceal tenant boundaries.
Refs: API 3.2; main plan P02; Phase: P02 repair.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.identity import LastAdministratorError
from orca.application.organizations import Organizations, bootstrap
from orca.application.projects import IdempotencyConflict, PreconditionRequired, StaleVersion
from orca.contracts.common import Page
from orca.contracts.organizations import (
    CreateOrganization,
    Member,
    MemberRole,
    OrganizationDetail,
    PatchMember,
    PatchOrganization,
)
from orca.infrastructure.db.tenant_context import assert_runtime_role
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/organizations", tags=["organization lifecycle"])


@contextmanager
def errors() -> Iterator[None]:
    try:
        yield
    except LookupError as exc:
        raise HTTPException(404, "Organization or membership not found") from exc
    except PermissionError as exc:
        raise HTTPException(403, "Organization action not permitted") from exc
    except PreconditionRequired as exc:
        raise HTTPException(428, "If-Match is required") from exc
    except StaleVersion as exc:
        raise HTTPException(412, "Resource version changed; reload before updating") from exc
    except LastAdministratorError as exc:
        raise HTTPException(
            409, "Cannot remove or demote the last active owner/administrator"
        ) from exc
    except IdempotencyConflict as exc:
        raise HTTPException(409, "Idempotency key was used for another request") from exc
    except IntegrityError as exc:
        raise HTTPException(
            409, "Organization slug or resource conflicts with an existing record"
        ) from exc


@router.post("", response_model=OrganizationDetail, status_code=201)
def create_organization(
    body: CreateOrganization,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> OrganizationDetail:
    if not getattr(request.app.state, "organization_creation_enabled", False):
        raise HTTPException(
            503, "Organization self-service creation is disabled by platform policy"
        )
    db = engine(request)
    with errors():
        with Session(db) as session, session.begin():
            assert_runtime_role(session.connection())
            identifier = bootstrap(session, principal.subject, body, idempotency_key)
        tenant, _ = resolve_tenant(db, principal, identifier)
        with TenantUnitOfWork(db, tenant) as session:
            result = Organizations(session, tenant, identifier).get()
        response.headers["ETag"] = result.etag
        response.headers["Location"] = f"/v1/organizations/{identifier}"
        return result


@router.get("/{org_id}", response_model=OrganizationDetail)
def get_organization(
    org_id: UUID, request: Request, response: Response, principal: CurrentPrincipal
) -> OrganizationDetail:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        service = Organizations(session, tenant, org_id)
        service.authorize(principal, "org:read")
        result = service.get()
        response.headers["ETag"] = result.etag
        return result


@router.patch("/{org_id}", response_model=OrganizationDetail)
def patch_organization(
    org_id: UUID,
    body: PatchOrganization,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> OrganizationDetail:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        service = Organizations(session, tenant, org_id)
        service.authorize(principal, "org:write")
        result = service.update(body, if_match)
        response.headers["ETag"] = result.etag
        return result


@router.get("/{org_id}/members", response_model=Page[Member])
def list_members(
    org_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    role: MemberRole | None = None,
) -> Page[Member]:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        service = Organizations(session, tenant, org_id)
        service.authorize(principal, "org:members")
        rows = service.members(cursor, limit + 1, role)
        return Page[Member](
            items=rows[:limit],
            page_size=limit,
            next_cursor=str(rows[limit - 1].id) if len(rows) > limit else None,
        )


@router.patch("/{org_id}/members/{member_id}", response_model=Member)
def patch_member(
    org_id: UUID,
    member_id: UUID,
    body: PatchMember,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> Member:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        service = Organizations(session, tenant, org_id)
        roles = service.authorize(principal, "org:members")
        result = service.change_member(member_id, body, if_match, principal.subject, roles)
        response.headers["ETag"] = result.etag
        return result


@router.delete("/{org_id}/members/{member_id}", status_code=204)
def delete_member(
    org_id: UUID,
    member_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> Response:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with errors(), TenantUnitOfWork(db, tenant) as session:
        service = Organizations(session, tenant, org_id)
        roles = service.authorize(principal, "org:members")
        service.change_member(
            member_id, PatchMember(status="revoked"), if_match, principal.subject, roles
        )
    return Response(status_code=204)

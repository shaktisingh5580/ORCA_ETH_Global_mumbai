"""Purpose: expose identity-bound invitation acceptance and expiring project-read grants.
Behavior: keep invitation tokens out of creation responses and require current administrator rights.
Refs: API 3.2; main plan P02; Phase: P02 completion repair.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.organizations import errors
from orca.api.routers.projects import engine, resolve_tenant
from orca.application.invitations import (
    accept_invitation,
    audit,
    create_grant,
    create_invitation,
    grant_detail,
    token_hash,
)
from orca.application.organizations import Organizations
from orca.contracts.common import Page
from orca.contracts.invitations import (
    AcceptInvitation,
    AccessGrant,
    CreateInvitation,
    GrantAccess,
    Invitation,
)
from orca.contracts.organizations import Member
from orca.infrastructure.db.tenant_context import assert_runtime_role
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1", tags=["invitations and grants"])


@contextmanager
def access_errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/organizations/{org_id}/invitations", response_model=Invitation, status_code=201)
def invite(
    org_id: UUID,
    body: CreateInvitation,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> Invitation:
    signing_key = getattr(request.app.state, "invitation_signing_key", None)
    if signing_key is None:
        raise HTTPException(503, "Invitation out-of-band delivery signing key is not configured")
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with access_errors(), TenantUnitOfWork(db, tenant) as session:
        return create_invitation(
            Organizations(session, tenant, org_id),
            principal,
            body,
            idempotency_key,
            signing_key.get_secret_value(),
        )


@router.post("/invitations/{token}/accept", response_model=Member)
def accept(
    token: str, body: AcceptInvitation, request: Request, principal: CurrentPrincipal
) -> Member:
    if not principal.verified_email:
        raise HTTPException(403, "Verified identity-provider email is required")
    if not re.fullmatch(r"[0-9a-f]{32}\.[0-9a-f]{64}", token):
        raise HTTPException(404, "Invitation not found")
    db = engine(request)
    with db.begin() as connection:
        assert_runtime_role(connection)
        tenant = connection.execute(
            text("SELECT iam.resolve_invitation_tenant(:hash,:email)"),
            {"hash": token_hash(token), "email": principal.verified_email},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Invitation not found")
    with access_errors(), TenantUnitOfWork(db, tenant) as session:
        return accept_invitation(session, tenant, token, principal)


@router.post("/organizations/{org_id}/access-grants", response_model=AccessGrant, status_code=201)
def grant(
    org_id: UUID, body: GrantAccess, request: Request, principal: CurrentPrincipal
) -> AccessGrant:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with access_errors(), TenantUnitOfWork(db, tenant) as session:
        return create_grant(Organizations(session, tenant, org_id), body, principal)


@router.get("/organizations/{org_id}/access-grants", response_model=Page[AccessGrant])
def grants(
    org_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    resource: UUID | None = None,
    principal_id: Annotated[UUID | None, Query(alias="principal")] = None,
) -> Page[AccessGrant]:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with access_errors(), TenantUnitOfWork(db, tenant) as session:
        Organizations(session, tenant, org_id).authorize(principal, "org:members")
        rows = list(
            session.execute(
                text("""SELECT * FROM iam.access_grants WHERE tenant_id=:t
          AND (CAST(:cursor AS uuid) IS NULL OR id>CAST(:cursor AS uuid))
          AND (CAST(:resource AS uuid) IS NULL OR project_id=:resource)
          AND (CAST(:principal AS uuid) IS NULL OR principal_id=:principal)
          ORDER BY id LIMIT :limit"""),
                {
                    "t": tenant,
                    "cursor": cursor,
                    "resource": resource,
                    "principal": principal_id,
                    "limit": limit + 1,
                },
            ).mappings()
        )
        return Page[AccessGrant](
            items=[grant_detail(row) for row in rows[:limit]],
            page_size=limit,
            next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        )


@router.delete("/organizations/{org_id}/access-grants/{grant_id}", status_code=204)
def revoke_grant(
    org_id: UUID,
    grant_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> Response:
    db = engine(request)
    tenant, _ = resolve_tenant(db, principal, org_id)
    with access_errors(), TenantUnitOfWork(db, tenant) as session:
        Organizations(session, tenant, org_id).authorize(principal, "org:members")
        row = (
            session.execute(
                text("SELECT * FROM iam.access_grants WHERE tenant_id=:t AND id=:id FOR UPDATE"),
                {"t": tenant, "id": grant_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("grant not found")
        Organizations.precondition(f'"{row["version"]}"', if_match)
        # Use the same project lock as project reads to serialize committed revocation.
        session.execute(
            text("SELECT id FROM research.projects WHERE tenant_id=:t AND id=:p FOR UPDATE"),
            {"t": tenant, "p": row["project_id"]},
        )
        session.execute(
            text(
                "UPDATE iam.access_grants SET revoked=true,version=version+1 WHERE tenant_id=:t AND id=:id"
            ),
            {"t": tenant, "id": grant_id},
        )
        audit(session, tenant, principal.subject, grant_id, "project_read_grant_revoked")
    return Response(status_code=204)

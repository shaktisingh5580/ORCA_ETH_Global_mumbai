"""Purpose: create single-use invitations and explicit, revocable resource grants.
Behavior: bind acceptance to verified identity; hash invite tokens; audit bounded project sharing.
Refs: API 2.2/3.2; main plan P02; Phase: P02 completion repair.
"""

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.organizations import Organizations
from orca.application.projects import IdempotencyConflict, request_digest
from orca.application.workspace_access import authorize_project
from orca.contracts.common import ResourceRef
from orca.contracts.invitations import (
    AccessGrant,
    CreateInvitation,
    GrantAccess,
    Invitation,
    PrincipalRef,
    future_bound,
)
from orca.contracts.organizations import Member
from orca.domain.identity import Principal


def invitation_token(key: str, identifier: UUID, email: str, expires: datetime) -> str:
    message = (
        identifier.bytes
        + b":"
        + email.casefold().encode()
        + b":"
        + str(int(expires.timestamp())).encode()
    )
    return identifier.hex + "." + hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def audit(session: Session, tenant: UUID, actor: str, identifier: UUID, kind: str) -> None:
    session.execute(
        text("""INSERT INTO iam.membership_audit(id,tenant_id,actor_subject,member_id,event)
      VALUES(:id,:t,:actor,:member,CAST(:event AS jsonb))"""),
        {
            "id": uuid4(),
            "t": tenant,
            "actor": actor,
            "member": identifier,
            "event": json.dumps({"kind": kind}),
        },
    )


def create_invitation(
    service: Organizations, principal: Principal, body: CreateInvitation, key: str, signing_key: str
) -> Invitation:
    roles = service.authorize(principal, "org:members")
    if body.role in {"org_owner", "source_operator"} and "org_owner" not in roles:
        raise PermissionError("only owners can invite owners or source operators")
    digest = request_digest(body.model_dump(mode="json"))
    existing = (
        service.session.execute(
            text("SELECT * FROM iam.invitations WHERE tenant_id=:t AND idempotency_key=:key"),
            {"t": service.tenant, "key": key},
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["request_hash"] != digest or existing["created_by"] != principal.subject:
            raise IdempotencyConflict()
        return Invitation(
            id=existing["id"],
            organization_id=service.organization,
            email=existing["email"],
            role=existing["role"],
            expires_at=existing["expires_at"],
        )
    future_bound(body.expires_at)
    identifier = uuid4()
    token = invitation_token(signing_key, identifier, body.email, body.expires_at)
    service.session.execute(
        text("""INSERT INTO iam.invitations
      (id,tenant_id,email,role,expires_at,token_hash,created_by,idempotency_key,request_hash)
      VALUES(:id,:t,:email,:role,:expires,:hash,:actor,:key,:digest)"""),
        {
            "id": identifier,
            "t": service.tenant,
            "email": body.email,
            "role": body.role,
            "expires": body.expires_at,
            "hash": token_hash(token),
            "actor": principal.subject,
            "key": key,
            "digest": digest,
        },
    )
    audit(service.session, service.tenant, principal.subject, identifier, "invitation_created")
    return Invitation(id=identifier, organization_id=service.organization, **body.model_dump())


def accept_invitation(session: Session, tenant: UUID, token: str, principal: Principal) -> Member:
    organization: UUID = session.execute(
        text("SELECT id FROM iam.organizations WHERE tenant_id=:t FOR UPDATE"), {"t": tenant}
    ).scalar_one()
    row = (
        session.execute(
            text(
                "SELECT * FROM iam.invitations WHERE tenant_id=:t AND token_hash=:hash FOR UPDATE"
            ),
            {"t": tenant, "hash": token_hash(token)},
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or row["accepted_at"] is not None
        or row["expires_at"] <= datetime.now(timezone.utc)
        or row["email"] != principal.verified_email
    ):
        raise LookupError("invitation not found")
    issuer_role = session.execute(
        text(
            "SELECT role FROM iam.memberships WHERE tenant_id=:t AND subject=:s AND status='active'"
        ),
        {"t": tenant, "s": row["created_by"]},
    ).scalar_one_or_none()
    if issuer_role not in {"org_owner", "org_admin"} or (
        row["role"] in {"org_owner", "source_operator"} and issuer_role != "org_owner"
    ):
        raise PermissionError("invitation issuer no longer has authority")
    existing = session.execute(
        text("SELECT 1 FROM iam.memberships WHERE tenant_id=:t AND subject=:s"),
        {"t": tenant, "s": principal.subject},
    ).scalar_one_or_none()
    if existing:
        raise ValueError("existing membership requires an explicit administrator update")
    identifier = uuid4()
    session.execute(
        text("INSERT INTO iam.memberships(id,tenant_id,subject,role) VALUES(:id,:t,:s,:role)"),
        {"id": identifier, "t": tenant, "s": principal.subject, "role": row["role"]},
    )
    session.execute(
        text(
            "UPDATE iam.invitations SET accepted_by=:s,accepted_at=clock_timestamp() WHERE tenant_id=:t AND id=:id"
        ),
        {"s": principal.subject, "t": tenant, "id": row["id"]},
    )
    audit(session, tenant, principal.subject, identifier, "invitation_accepted")
    return Member(
        id=identifier,
        organization_id=organization,
        subject=principal.subject,
        role=row["role"],
        status="active",
        version=1,
        etag='"1"',
    )


def grant_detail(row: Any) -> AccessGrant:
    return AccessGrant(
        id=row["id"],
        resource_ref=ResourceRef(
            type="research_project", id=str(row["project_id"]), version=str(row["resource_version"])
        ),
        principal_ref=PrincipalRef(id=row["principal_id"]),
        scopes=["project:read"],
        expires_at=row["expires_at"],
        version=row["version"],
        etag=f'"{row["version"]}"',
        revoked=row["revoked"],
    )


def create_grant(service: Organizations, body: GrantAccess, principal: Principal) -> AccessGrant:
    service.authorize(principal, "org:members")
    project = UUID(body.resource_ref.id)
    authorize_project(service.session, service.tenant, project, principal, "project:share")
    future_bound(body.expires_at)
    target = service.session.execute(
        text("SELECT 1 FROM iam.memberships WHERE tenant_id=:t AND id=:id AND status='active'"),
        {"t": service.tenant, "id": body.principal_ref.id},
    ).scalar_one_or_none()
    if target is None:
        raise LookupError("active organization member not found")
    version = service.session.execute(
        text("SELECT version FROM research.projects WHERE tenant_id=:t AND id=:p"),
        {"t": service.tenant, "p": project},
    ).scalar_one()
    if str(version) != body.resource_ref.version:
        raise ValueError("grant resource version is stale")
    row = (
        service.session.execute(
            text("""INSERT INTO iam.access_grants
      (id,tenant_id,project_id,principal_id,resource_version,expires_at,created_by)
      VALUES(:id,:t,:p,:member,:version,:expires,:actor) RETURNING *"""),
            {
                "id": uuid4(),
                "t": service.tenant,
                "p": project,
                "member": body.principal_ref.id,
                "version": version,
                "expires": body.expires_at,
                "actor": principal.subject,
            },
        )
        .mappings()
        .one()
    )
    audit(
        service.session, service.tenant, principal.subject, row["id"], "project_read_grant_created"
    )
    return grant_detail(row)

"""Purpose: manage organizations and versioned memberships without weakening tenant boundaries.
Behavior: atomic idempotent bootstrap, tenant locks, last-admin protection and append-only audit.
Refs: API 3.2; main plan P02; Phase: P02 repair.
"""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.identity import LastAdministratorError
from orca.application.projects import IdempotencyConflict, PreconditionRequired, StaleVersion
from orca.contracts.organizations import (
    CreateOrganization,
    Member,
    OrganizationDetail,
    PatchMember,
    PatchOrganization,
)
from orca.domain.identity import Principal


def bootstrap(session: Session, subject: str, body: CreateOrganization, key: str) -> UUID:
    digest = hashlib.sha256(
        json.dumps(body.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identifier = session.execute(
        text(
            "SELECT iam.bootstrap_organization(:subject,:key,:hash,:id,:tenant,:member,:name,:slug,:visibility)"
        ),
        {
            "subject": subject,
            "key": key,
            "hash": digest,
            "id": uuid4(),
            "tenant": uuid4(),
            "member": uuid4(),
            **body.model_dump(),
        },
    ).scalar_one_or_none()
    if identifier is None:
        raise IdempotencyConflict()
    return UUID(str(identifier))


class Organizations:
    def __init__(self, session: Session, tenant: UUID, organization: UUID):
        self.session, self.tenant, self.organization = session, tenant, organization

    def authorize(self, principal: Principal, scope: str) -> set[str]:
        self.get(lock=True)
        roles: set[str] = set(
            self.session.execute(
                text(
                    "SELECT role FROM iam.memberships WHERE tenant_id=:tenant AND subject=:subject AND status='active'"
                ),
                {"tenant": self.tenant, "subject": principal.subject},
            ).scalars()
        )
        principal.require(scope, roles)
        return roles

    def get(self, *, lock: bool = False) -> OrganizationDetail:
        query = "SELECT * FROM iam.organizations WHERE tenant_id=:tenant AND id=:id" + (
            " FOR UPDATE" if lock else ""
        )
        row = (
            self.session.execute(text(query), {"tenant": self.tenant, "id": self.organization})
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("organization not found")
        return OrganizationDetail(
            id=row["id"],
            name=row["name"],
            slug=row["slug"],
            visibility=row["visibility"],
            settings=row["settings"],
            version=row["version"],
            etag=f'"{row["version"]}"',
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def precondition(actual: str, supplied: str | None) -> None:
        if supplied is None:
            raise PreconditionRequired()
        if supplied != actual:
            raise StaleVersion()

    def update(self, body: PatchOrganization, if_match: str | None) -> OrganizationDetail:
        before = self.get(lock=True)
        self.precondition(before.etag, if_match)
        self.session.execute(
            text("""UPDATE iam.organizations SET name=:name,settings=CAST(:settings AS jsonb),
          version=version+1,updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:id"""),
            {
                "tenant": self.tenant,
                "id": self.organization,
                "name": body.display_name if body.display_name is not None else before.name,
                "settings": (body.settings or before.settings).model_dump_json(),
            },
        )
        return self.get()

    def member(self, row: Any) -> Member:
        return Member(
            id=row["id"],
            organization_id=self.organization,
            subject=row["subject"],
            role=row["role"],
            status=row["status"],
            version=row["version"],
            etag=f'"{row["version"]}"',
        )

    def members(self, after: UUID | None, limit: int, role: str | None = None) -> list[Member]:
        rows = self.session.execute(
            text("""SELECT * FROM iam.memberships WHERE tenant_id=:tenant
          AND (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid))
          AND (CAST(:role AS text) IS NULL OR role=:role) ORDER BY id LIMIT :limit"""),
            {"tenant": self.tenant, "after": after, "limit": limit, "role": role},
        ).mappings()
        return [self.member(row) for row in rows]

    def change_member(
        self,
        identifier: UUID,
        body: PatchMember,
        if_match: str | None,
        actor: str,
        actor_roles: set[str],
    ) -> Member:
        # All member mutations lock the same organization before checking active administrators.
        self.get(lock=True)
        row = (
            self.session.execute(
                text("SELECT * FROM iam.memberships WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                {"tenant": self.tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("member not found")
        before = self.member(row)
        self.precondition(before.etag, if_match)
        role, status = body.role or before.role, body.status or before.status
        if "org_owner" not in actor_roles and (before.role == "org_owner" or role == "org_owner"):
            raise PermissionError("only an owner may manage owner membership")
        if (
            before.role in {"org_owner", "org_admin"}
            and before.status == "active"
            and (role not in {"org_owner", "org_admin"} or status != "active")
        ):
            count: int = self.session.execute(
                text(
                    "SELECT count(*) FROM iam.memberships WHERE tenant_id=:tenant AND status='active' AND role IN ('org_owner','org_admin')"
                ),
                {"tenant": self.tenant},
            ).scalar_one()
            if count <= 1:
                raise LastAdministratorError()
        changed = (
            self.session.execute(
                text("""UPDATE iam.memberships SET role=:role,status=:status,version=version+1,
          updated_at=clock_timestamp() WHERE tenant_id=:tenant AND id=:id RETURNING *"""),
                {"tenant": self.tenant, "id": identifier, "role": role, "status": status},
            )
            .mappings()
            .one()
        )
        event = {
            "previous_role": before.role,
            "previous_status": before.status,
            "role": role,
            "status": status,
            "version": before.version + 1,
        }
        self.session.execute(
            text(
                "INSERT INTO iam.membership_audit(id,tenant_id,actor_subject,member_id,event) VALUES(:id,:tenant,:actor,:member,CAST(:event AS jsonb))"
            ),
            {
                "id": uuid4(),
                "tenant": self.tenant,
                "actor": actor,
                "member": identifier,
                "event": json.dumps(event),
            },
        )
        return self.member(changed)

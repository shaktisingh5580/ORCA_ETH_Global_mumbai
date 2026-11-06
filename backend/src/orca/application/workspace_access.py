"""Purpose: enforce per-project authority and versioned organization limits.
Behavior: intersect token, organization and project capabilities; serialize grants and revocation.
Refs: API 2.2/3.2/3.3; main plan P02; Phase: P02 completion repair.
"""

import json
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.identity import LastAdministratorError
from orca.application.organizations import Organizations
from orca.contracts.workspace_access import (
    AddProjectMember,
    OrgPolicy,
    PatchOrgPolicy,
    PatchProjectMember,
    ProjectMemberDetail,
)
from orca.domain.identity import Principal


def authorize_project(
    session: Session, tenant: UUID, project: UUID, principal: Principal, scope: str
) -> None:
    # Lock the project for a consistent membership check; all grant mutations use
    # this lock, so a request after committed revocation cannot use stale authority.
    row = (
        session.execute(
            text(
                "SELECT access_class,state,version FROM research.projects WHERE tenant_id=:t AND id=:p FOR UPDATE"
            ),
            {"t": tenant, "p": project},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise LookupError("project not found")
    member = (
        session.execute(
            text("""SELECT m.role,pm.role AS project_role,
          EXISTS(SELECT 1 FROM iam.access_grants g WHERE g.tenant_id=m.tenant_id
            AND g.project_id=:p AND g.principal_id=m.id AND NOT g.revoked
            AND g.expires_at>clock_timestamp() AND g.resource_version=:version) AS read_grant
          FROM iam.memberships m
          LEFT JOIN research.project_members pm ON pm.tenant_id=m.tenant_id
            AND pm.principal_id=m.id AND pm.project_id=:p AND pm.status='active'
          WHERE m.tenant_id=:t AND m.subject=:s AND m.status='active'"""),
            {"t": tenant, "p": project, "s": principal.subject, "version": row["version"]},
        )
        .mappings()
        .one_or_none()
    )
    if member is None:
        raise LookupError("project not found")
    privileged = member["role"] in {"org_owner", "org_admin"}
    project_role = member["project_role"]
    if (
        not privileged
        and project_role is None
        and row["access_class"] not in {"public", "organization"}
        and not member["read_grant"]
    ):
        raise LookupError("project not found")
    principal.require(scope, {member["role"]})
    if not privileged:
        if scope == "project:share" and project_role != "admin":
            raise PermissionError("project administration required")
        if scope == "project:write" and project_role not in {"admin", "editor"}:
            raise PermissionError("project editor required")
    if scope != "project:read" and row["state"] != "active":
        raise PermissionError("archived project is read only")


class ProjectMembers:
    def __init__(self, session: Session, tenant: UUID, project: UUID):
        self.session, self.tenant, self.project = session, tenant, project

    def list(self, after: UUID | None = None, limit: int = 100) -> list[ProjectMemberDetail]:
        rows = self.session.execute(
            text("""SELECT id,project_id,principal_id,role,status,version
          FROM research.project_members WHERE tenant_id=:t AND project_id=:p
          AND (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid)) ORDER BY id LIMIT :limit"""),
            {"t": self.tenant, "p": self.project, "after": after, "limit": limit},
        ).mappings()
        return [ProjectMemberDetail(**dict(r), etag=f'"{r["version"]}"') for r in rows]

    def add(self, body: AddProjectMember, actor: Principal) -> ProjectMemberDetail:
        authorize_project(self.session, self.tenant, self.project, actor, "project:share")
        target = self.session.execute(
            text("""SELECT role FROM iam.memberships
          WHERE tenant_id=:t AND id=:id AND status='active' FOR UPDATE"""),
            {"t": self.tenant, "id": body.principal_id},
        ).scalar_one_or_none()
        if target is None:
            raise LookupError("active organization member not found")
        # A project role never increases the target's organization/token capabilities.
        identifier = self.session.execute(
            text("""INSERT INTO research.project_members
          (id,tenant_id,project_id,principal_id,role) VALUES(:id,:t,:p,:principal,:role)
          ON CONFLICT(tenant_id,project_id,principal_id) DO NOTHING RETURNING id"""),
            {
                "id": uuid4(),
                "t": self.tenant,
                "p": self.project,
                "principal": body.principal_id,
                "role": body.role,
            },
        ).scalar_one_or_none()
        if identifier is None:
            raise ValueError("membership exists; update with its current ETag")
        self.audit(actor.subject, identifier, {"role": body.role, "status": "active"})
        return self.get(identifier)

    def get(self, identifier: UUID) -> ProjectMemberDetail:
        row = (
            self.session.execute(
                text("""SELECT id,project_id,principal_id,role,status,version
          FROM research.project_members WHERE tenant_id=:t AND project_id=:p AND id=:id FOR UPDATE"""),
                {"t": self.tenant, "p": self.project, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("project member not found")
        return ProjectMemberDetail(**dict(row), etag=f'"{row["version"]}"')

    def change(
        self, identifier: UUID, body: PatchProjectMember, if_match: str | None, actor: Principal
    ) -> ProjectMemberDetail:
        authorize_project(self.session, self.tenant, self.project, actor, "project:share")
        before = self.get(identifier)
        Organizations.precondition(before.etag, if_match)
        role, status = body.role or before.role, body.status or before.status
        active = self.session.execute(
            text("SELECT 1 FROM iam.memberships WHERE tenant_id=:t AND id=:id AND status='active'"),
            {"t": self.tenant, "id": before.principal_id},
        ).scalar_one_or_none()
        if status == "active" and active is None:
            raise ValueError("organization membership is inactive")
        if (
            before.role == "admin"
            and before.status == "active"
            and active is not None
            and (role != "admin" or status == "revoked")
        ):
            count: int = self.session.execute(
                text("""SELECT count(*) FROM research.project_members pm
              JOIN iam.memberships m ON m.tenant_id=pm.tenant_id AND m.id=pm.principal_id AND m.status='active'
              WHERE pm.tenant_id=:t AND pm.project_id=:p AND pm.role='admin' AND pm.status='active'"""),
                {"t": self.tenant, "p": self.project},
            ).scalar_one()
            if count <= 1:
                raise LastAdministratorError()
        self.session.execute(
            text("""UPDATE research.project_members SET role=:role,status=:status,
          version=version+1,updated_at=clock_timestamp() WHERE tenant_id=:t AND project_id=:p AND id=:id"""),
            {"role": role, "status": status, "t": self.tenant, "p": self.project, "id": identifier},
        )
        self.audit(
            actor.subject,
            identifier,
            {"role": role, "status": status, "version": before.version + 1},
        )
        return self.get(identifier)

    def audit(self, actor: str, identifier: UUID, event: dict[str, object]) -> None:
        self.session.execute(
            text("""INSERT INTO iam.membership_audit
          (id,tenant_id,actor_subject,member_id,event) VALUES(:id,:t,:actor,:member,CAST(:event AS jsonb))"""),
            {
                "id": uuid4(),
                "t": self.tenant,
                "actor": actor,
                "member": identifier,
                "event": json.dumps(
                    {"kind": "project_member", "project_id": str(self.project), **event}
                ),
            },
        )


def get_policy(session: Session, tenant: UUID) -> OrgPolicy:
    row = (
        session.execute(
            text(
                "SELECT retention_days,max_upload_bytes,max_projects,version FROM iam.organization_policies WHERE tenant_id=:t"
            ),
            {"t": tenant},
        )
        .mappings()
        .one_or_none()
    )
    return OrgPolicy(**dict(row), etag=f'"{row["version"]}"') if row else OrgPolicy()


def update_policy(
    session: Session, tenant: UUID, body: PatchOrgPolicy, if_match: str | None
) -> OrgPolicy:
    # Caller holds the organization lock, including when the policy row is absent.
    before = get_policy(session, tenant)
    Organizations.precondition(before.etag, if_match)
    values = {**before.model_dump(), **body.model_dump(exclude_none=True)}
    session.execute(
        text("""INSERT INTO iam.organization_policies
      (tenant_id,retention_days,max_upload_bytes,max_projects,version) VALUES(:t,:retention_days,:max_upload_bytes,:max_projects,:version)
      ON CONFLICT(tenant_id) DO UPDATE SET retention_days=EXCLUDED.retention_days,
      max_upload_bytes=EXCLUDED.max_upload_bytes,max_projects=EXCLUDED.max_projects,
      version=EXCLUDED.version,updated_at=clock_timestamp()"""),
        {**values, "t": tenant, "version": before.version + 1},
    )
    return get_policy(session, tenant)

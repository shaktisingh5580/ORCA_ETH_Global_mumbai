"""Purpose: implement tenant project creation, pagination and optimistic concurrency.
Behavior: bind idempotency keys to request digests and require current entity versions.
Refs: API sections 3.3,11.1; main plan P02.4; Phase: P02.
"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from orca.infrastructure.db.models import Project


class IdempotencyConflict(Exception):
    pass


class PreconditionRequired(Exception):
    pass


class StaleVersion(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProjectPage:
    items: list[Project]
    next_cursor: str | None


def request_digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class ProjectService:
    def create(
        self, session: Session, tenant_id: UUID, payload: dict[str, object], key: str
    ) -> Project:
        digest = request_digest(payload)
        project_id = uuid4()
        inserted = session.execute(
            text("""INSERT INTO iam.idempotency_records
          (tenant_id,operation,idempotency_key,request_hash,resource_id)
          VALUES(:tenant,'create_project',:key,:digest,:resource)
          ON CONFLICT (tenant_id,operation,idempotency_key) DO NOTHING
          RETURNING resource_id"""),
            {"tenant": tenant_id, "key": key, "digest": digest, "resource": project_id},
        ).scalar_one_or_none()
        if inserted is None:
            record = session.execute(
                text("""SELECT request_hash,resource_id
              FROM iam.idempotency_records
              WHERE tenant_id=:tenant AND operation='create_project' AND idempotency_key=:key"""),
                {"tenant": tenant_id, "key": key},
            ).one()
            if record.request_hash != digest:
                raise IdempotencyConflict
            existing = session.get(Project, record.resource_id)
            if existing is None:
                raise RuntimeError("idempotency record references a missing project")
            return existing
        project = Project(
            id=project_id,
            tenant_id=tenant_id,
            slug=str(payload["slug"]),
            name=str(payload["name"]),
            description=payload.get("description"),
            access_class=str(payload.get("access_class", "project")),
            state="active",
        )
        session.add(project)
        session.flush()
        return project

    def update(
        self, session: Session, project: Project, changes: dict[str, object], if_match: str | None
    ) -> Project:
        if if_match is None:
            raise PreconditionRequired
        if if_match != f'"{project.version}"':
            raise StaleVersion
        for field in ("name", "description", "access_class", "state"):
            if field in changes:
                setattr(project, field, changes[field])
        project.version += 1
        session.flush()
        return project

    def list(
        self, session: Session, tenant_id: UUID, limit: int, cursor: UUID | None = None
    ) -> ProjectPage:
        query = select(Project).where(Project.tenant_id == tenant_id)
        if cursor is not None:
            query = query.where(Project.id > cursor)
        rows = list(session.scalars(query.order_by(Project.id).limit(limit + 1)))
        next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
        return ProjectPage(rows[:limit], next_cursor)

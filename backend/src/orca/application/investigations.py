"""Purpose: persist project-scoped investigation drafts and append-only plan revisions.
Behavior: recheck project permission, bind idempotency keys and keep edits ETag-guarded.
Refs: main_implementation plan.md P05.1; api design.md §§3.6, 5.1; Phase: P05.
"""

import hashlib
import json
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import rfc8785
from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.planning import plan_digest, validate_plan
from orca.application.projects import IdempotencyConflict, PreconditionRequired, StaleVersion
from orca.application.workspace_access import authorize_project
from orca.contracts.common import Page
from orca.contracts.investigations import (
    CreateInvestigation,
    InvestigationPlanView,
    InvestigationView,
    PatchInvestigation,
)
from orca.contracts.research_plans import TypedPlan
from orca.domain.identity import Principal


def _request_digest(payload: dict[str, object]) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            b"orca.investigation.create.v1\0" + rfc8785.dumps(cast(Any, payload))
        ).hexdigest()
    )


class Investigations:
    def __init__(self, session: Session, tenant: UUID, project: UUID, principal: Principal):
        self.session = session
        self.tenant = tenant
        self.project = project
        self.principal = principal

    def _authorize(self, scope: str) -> dict[str, UUID]:
        authorize_project(self.session, self.tenant, self.project, self.principal, scope)
        return {"tenant": self.tenant, "project": self.project}

    @staticmethod
    def _investigation(row: Any) -> InvestigationView:
        values = {key: row[key] for key in InvestigationView.model_fields if key != "etag"}
        values["etag"] = f'"{row["version"]}"'
        return InvestigationView.model_validate(values)

    @staticmethod
    def _plan(row: Any) -> InvestigationPlanView:
        values = {key: row[key] for key in InvestigationPlanView.model_fields}
        return InvestigationPlanView.model_validate(values)

    def create(self, body: CreateInvestigation, idempotency_key: str) -> InvestigationView:
        params = self._authorize("research:plan")
        if body.project_id != self.project:
            raise LookupError("Investigation project not found")
        if not 1 <= len(idempotency_key) <= 128:
            raise ValueError("Invalid idempotency key")
        payload = body.model_dump(mode="json")
        digest = _request_digest(payload)
        identifier = uuid4()
        self.session.execute(
            text("""INSERT INTO research.investigations
              (id,tenant_id,project_id,created_by,question,geo_scope,time_window,as_of,risk_tier,
               idempotency_key,request_digest)
              VALUES(:id,:tenant,:project,:actor,:question,CAST(:geo AS jsonb),CAST(:window AS jsonb),
               :as_of,:risk,:key,:digest)
              ON CONFLICT(tenant_id,project_id,idempotency_key) DO NOTHING"""),
            {
                **params,
                "id": identifier,
                "actor": self.principal.subject,
                "question": body.question,
                "geo": json.dumps(payload["geo_scope"]),
                "window": json.dumps(payload["time_window"]),
                "as_of": body.as_of,
                "risk": body.risk_tier,
                "key": idempotency_key,
                "digest": digest,
            },
        )
        row = (
            self.session.execute(
                text("""SELECT * FROM research.investigations
                  WHERE tenant_id=:tenant AND project_id=:project AND idempotency_key=:key"""),
                {**params, "key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise RuntimeError("Investigation insert did not produce a row")
        if row["request_digest"] != digest:
            raise IdempotencyConflict()
        return self._investigation(row)

    def get(self, identifier: UUID) -> InvestigationView:
        params = self._authorize("project:read")
        row = (
            self.session.execute(
                text("""SELECT * FROM research.investigations
                  WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""),
                {**params, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Investigation not found")
        return self._investigation(row)

    def list_investigations(
        self, *, limit: int = 20, cursor: UUID | None = None
    ) -> Page[InvestigationView]:
        if not 1 <= limit <= 100:
            raise ValueError("Page limit must be from 1 to 100")
        params = self._authorize("project:read")
        rows = self.session.execute(
            text("""SELECT * FROM research.investigations WHERE tenant_id=:tenant AND project_id=:project
              AND (CAST(:cursor AS uuid) IS NULL OR id>:cursor) ORDER BY id LIMIT :limit"""),
            {**params, "cursor": cursor, "limit": limit + 1},
        ).mappings()
        fetched = list(rows)
        has_more = len(fetched) > limit
        items = [self._investigation(row) for row in fetched[:limit]]
        return Page(
            items=items,
            next_cursor=str(items[-1].id) if has_more and items else None,
            page_size=limit,
        )

    def patch(
        self, identifier: UUID, body: PatchInvestigation, if_match: str | None
    ) -> InvestigationView:
        params = self._authorize("research:plan")
        if if_match is None:
            raise PreconditionRequired()
        row = (
            self.session.execute(
                text("""SELECT * FROM research.investigations
                  WHERE tenant_id=:tenant AND project_id=:project AND id=:id FOR UPDATE"""),
                {**params, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Investigation not found")
        if if_match != f'"{row["version"]}"':
            raise StaleVersion()
        if row["state"] != "DRAFT" or row["current_plan_revision"]:
            raise ValueError("A planned investigation is immutable; create a new revision")

        changes = body.model_dump(exclude_unset=True)
        question = changes.get("question") or row["question"]
        geo = changes.get("geo_scope") or row["geo_scope"]
        window = changes.get("time_window") or row["time_window"]
        description = changes.get("description", row["description"])
        self.session.execute(
            text("""UPDATE research.investigations SET question=:question,geo_scope=CAST(:geo AS jsonb),
              time_window=CAST(:window AS jsonb),description=:description,version=version+1,
              updated_at=clock_timestamp() WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""),
            {
                **params,
                "id": identifier,
                "question": question,
                "geo": json.dumps(geo),
                "window": json.dumps(window),
                "description": description,
            },
        )
        return self.get(identifier)

    def create_plan(
        self,
        identifier: UUID,
        plan: TypedPlan,
        idempotency_key: str,
        *,
        maximum_budget: Decimal,
    ) -> InvestigationPlanView:
        params = self._authorize("research:plan")
        if not 1 <= len(idempotency_key) <= 128:
            raise ValueError("Invalid idempotency key")
        validate_plan(plan, maximum_budget=maximum_budget)
        digest = plan_digest(plan, maximum_budget=maximum_budget)
        row = (
            self.session.execute(
                text("""SELECT * FROM research.investigations
                  WHERE tenant_id=:tenant AND project_id=:project AND id=:id FOR UPDATE"""),
                {**params, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Investigation not found")
        for source_id in plan.source_ids:
            granted = self.session.execute(
                text("""SELECT 1 FROM research.dataset_snapshots ds
                  JOIN research.snapshot_project_grants grant_row
                    ON grant_row.tenant_id=ds.tenant_id AND grant_row.snapshot_id=ds.id
                  WHERE ds.tenant_id=:tenant AND ds.id=:source
                    AND grant_row.project_id=:project
                    AND (grant_row.expires_at IS NULL OR grant_row.expires_at>clock_timestamp())"""),
                {**params, "source": source_id},
            ).scalar_one_or_none()
            if granted is None:
                raise LookupError("Authorized source snapshot not found")
        existing = (
            self.session.execute(
                text("""SELECT * FROM research.investigation_plans
                  WHERE tenant_id=:tenant AND project_id=:project AND investigation_id=:id
                    AND idempotency_key=:key"""),
                {**params, "id": identifier, "key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if existing["request_digest"] != digest:
                raise IdempotencyConflict()
            return self._plan(existing)
        if plan.revision != row["current_plan_revision"] + 1:
            raise ValueError("Plan revision must be the next investigation revision")
        if row["state"] not in {"DRAFT", "PLAN_READY"}:
            raise ValueError("Investigation is not accepting plan revisions")
        plan_id = uuid4()
        saved = (
            self.session.execute(
                text("""INSERT INTO research.investigation_plans
              (id,tenant_id,project_id,investigation_id,revision,plan,plan_digest,request_digest,
               idempotency_key,created_by)
              VALUES(:plan_id,:tenant,:project,:investigation,:revision,CAST(:plan AS jsonb),
               :digest,:request_digest,:key,:actor) RETURNING *"""),
                {
                    **params,
                    "plan_id": plan_id,
                    "investigation": identifier,
                    "revision": plan.revision,
                    "plan": json.dumps(plan.model_dump(mode="json")),
                    "digest": digest,
                    "request_digest": digest,
                    "key": idempotency_key,
                    "actor": self.principal.subject,
                },
            )
            .mappings()
            .one()
        )
        self.session.execute(
            text("""UPDATE research.investigations SET current_plan_revision=:revision,
              state='PLAN_READY',version=version+1,updated_at=clock_timestamp()
              WHERE tenant_id=:tenant AND project_id=:project AND id=:id"""),
            {**params, "id": identifier, "revision": plan.revision},
        )
        return self._plan(saved)

    def plans(self, identifier: UUID) -> list[InvestigationPlanView]:
        params = self._authorize("project:read")
        self.get(identifier)
        rows = self.session.execute(
            text("""SELECT * FROM research.investigation_plans
              WHERE tenant_id=:tenant AND project_id=:project AND investigation_id=:id
              ORDER BY revision"""),
            {**params, "id": identifier},
        ).mappings()
        return [self._plan(row) for row in rows]

    def get_plan(self, identifier: UUID, plan_id: UUID) -> InvestigationPlanView:
        params = self._authorize("project:read")
        row = (
            self.session.execute(
                text("""SELECT * FROM research.investigation_plans
                  WHERE tenant_id=:tenant AND project_id=:project
                    AND investigation_id=:investigation AND id=:id"""),
                {**params, "investigation": identifier, "id": plan_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Investigation plan not found")
        return self._plan(row)

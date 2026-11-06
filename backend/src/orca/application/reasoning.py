"""Purpose: persist reasoning requests, plan approvals and deterministic results.
Behavior: reuse project authority and immutable dataset evidence; bind retries to request digests.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; Phase: A02.
"""

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from orca.application.normalization import TabularEvidence
from orca.contracts.reasoning import ReasoningRequest, ResearchPlan
from orca.domain.normalization import digest
from orca.domain.tabular_analysis import summarize


class ResearchReasoning(TabularEvidence):
    def event(self, run: UUID, kind: str, data: dict[str, Any]) -> None:
        self.session.execute(
            text("""INSERT INTO research.reasoning_events
          (tenant_id,project_id,run_id,event_type,data) VALUES(:t,:p,:run,:kind,CAST(:data AS jsonb))"""),
            {
                "t": self.tenant,
                "p": self.project,
                "run": run,
                "kind": kind,
                "data": json.dumps(data, allow_nan=False),
            },
        )

    @staticmethod
    def run_view(row: Any) -> dict[str, Any]:
        return {
            k: row[k]
            for k in (
                "id",
                "project_id",
                "request",
                "state",
                "plan",
                "plan_digest",
                "result",
                "error_code",
                "created_at",
                "updated_at",
            )
        }

    def create_run(self, body: ReasoningRequest, key: str) -> dict[str, Any]:
        params = self.authorize("project:write")
        if len(set(body.dataset_ids)) != len(body.dataset_ids):
            raise ValueError("Duplicate dataset IDs")
        for identifier in body.dataset_ids:
            self.row(identifier)
        request = body.model_dump(mode="json")
        params.update(
            id=uuid4(),
            actor=self.principal.subject,
            request=json.dumps(request),
            digest=digest(request),
            key=key,
        )
        self.session.execute(
            text("""INSERT INTO research.reasoning_runs
          (id,tenant_id,project_id,created_by,request,request_digest,idempotency_key)
          VALUES(:id,:t,:p,:actor,CAST(:request AS jsonb),:digest,:key)
          ON CONFLICT(tenant_id,project_id,idempotency_key) DO NOTHING"""),
            params,
        )
        row = (
            self.session.execute(
                text("""SELECT * FROM research.reasoning_runs
          WHERE tenant_id=:t AND project_id=:p AND idempotency_key=:key"""),
                params,
            )
            .mappings()
            .one()
        )
        if row["request_digest"] != params["digest"]:
            raise ValueError("Idempotency key belongs to another request")
        return self.run_view(row)

    def run_row(self, identifier: UUID, lock: bool = False) -> Any:
        row = (
            self.session.execute(
                text(
                    """SELECT * FROM research.reasoning_runs
          WHERE tenant_id=:t AND project_id=:p AND id=:id"""
                    + (" FOR UPDATE" if lock else "")
                ),
                {**self.authorize(), "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Research run not found")
        return row

    def runs(self) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text("""SELECT * FROM research.reasoning_runs
          WHERE tenant_id=:t AND project_id=:p ORDER BY created_at DESC LIMIT 50"""),
            self.authorize(),
        ).mappings()
        return [self.run_view(r) for r in rows]

    def events(self, identifier: UUID, after: int = 0) -> list[dict[str, Any]]:
        self.run_row(identifier)
        rows = self.session.execute(
            text("""SELECT id,event_type,data,created_at FROM research.reasoning_events
          WHERE tenant_id=:t AND project_id=:p AND run_id=:id AND id>:after ORDER BY id LIMIT 200"""),
            {**self.authorize(), "id": identifier, "after": after},
        ).mappings()
        return [dict(r) for r in rows]

    def approve_plan(self, identifier: UUID, expected_digest: str) -> dict[str, Any]:
        self.authorize("project:write")
        row = self.run_row(identifier, lock=True)
        if row["plan_digest"] != expected_digest:
            raise ValueError("Plan changed; reload before approving")
        if row["state"] == "completed":
            return self.run_view(row)
        if row["state"] != "planned":
            raise ValueError("Only a supported proposed plan can be approved")
        plan = ResearchPlan.model_validate(row["plan"])
        artifacts = []
        for item in plan.datasets:
            saved = self.approve(item.dataset_id, item.mapping)
            summary = summarize(saved.result)
            artifacts.append(
                {
                    "normalization_id": str(saved.id),
                    "dataset_id": str(item.dataset_id),
                    "role": item.role,
                    "artifact_digest": saved.artifact_digest,
                    "source_mode": saved.result["source_mode"],
                    "summary": summary,
                }
            )
        result: dict[str, Any] = {
            "recipe": plan.recipe,
            "artifacts": artifacts,
            "scientific_status": "prototype_not_independently_validated",
            "limitations": plan.limitations,
            "review_state": "not_reviewed",
        }
        if plan.recipe == "mean_difference":
            summaries = {a["role"]: a["summary"] for a in artifacts}
            result["difference_celsius"] = (
                summaries["current"]["mean_celsius"] - summaries["reference"]["mean_celsius"]
            )
        result["result_digest"] = digest(result)
        self.event(identifier, "plan_approved", {"plan_digest": expected_digest})
        self.event(identifier, "analysis_completed", result)
        self.session.execute(
            text("""UPDATE research.reasoning_runs SET state='completed',result=CAST(:result AS jsonb),
          approved_by=:actor,updated_at=now() WHERE tenant_id=:t AND project_id=:p AND id=:id"""),
            {
                **self.authorize(),
                "id": identifier,
                "result": json.dumps(result),
                "actor": self.principal.subject,
            },
        )
        return self.run_view(self.run_row(identifier))

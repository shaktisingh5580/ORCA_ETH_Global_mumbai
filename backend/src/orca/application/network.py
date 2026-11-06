"""Purpose: coordinate authorized research delegation with atomic intents and exclusive assignment.
Behavior: retain tenant isolation, idempotency, current bindings and immutable signed evidence.
Refs: canonical database/agent guides; H04-agent-coordination.json; Phase: H04.
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.projects import IdempotencyConflict
from orca.application.prototype import Prototype
from orca.application.workspace_access import authorize_project
from orca.contracts.network import (
    AgentBinding,
    AssignClaim,
    CreateNetworkTask,
    NetworkOperation,
    NetworkTask,
)
from orca.domain.identity import Principal
from orca.infrastructure.federation.signing import digest


def encode(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), allow_nan=False)


def operation_view(row: Any) -> NetworkOperation:
    return NetworkOperation.model_validate(
        {field: row[field] for field in NetworkOperation.model_fields}
    )


class Network:
    def __init__(self, session: Session, tenant: UUID, project: UUID, principal: Principal):
        self.session, self.tenant, self.project, self.principal = (
            session,
            tenant,
            project,
            principal,
        )

    def authorize(self, scope: str = "project:read") -> None:
        authorize_project(self.session, self.tenant, self.project, self.principal, scope)

    def task_row(self, identifier: UUID, *, lock: bool = False) -> Any:
        self.authorize()
        row = (
            self.session.execute(
                text(
                    "SELECT * FROM federation.tasks WHERE tenant_id=:t AND project_id=:p AND id=:id"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"t": self.tenant, "p": self.project, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("network task not found")
        return row

    def agent(self, identifier: UUID) -> AgentBinding:
        row = self.session.execute(
            text(
                "SELECT binding FROM federation.agents WHERE tenant_id=:t AND id=:id AND NOT revoked"
            ),
            {"t": self.tenant, "id": identifier},
        ).scalar_one_or_none()
        if row is None:
            raise LookupError("active agent binding not found")
        return AgentBinding.model_validate(row)

    def existing(self, action: str, key: str, payload: dict[str, Any]) -> NetworkOperation | None:
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:identity,0))"),
            {"identity": f"network:{self.tenant}:{self.principal.subject}:{action}:{key}"},
        )
        row = (
            self.session.execute(
                text(
                    "SELECT * FROM federation.operations WHERE tenant_id=:t AND created_by=:s AND action=:a AND idempotency_key=:k"
                ),
                {"t": self.tenant, "s": self.principal.subject, "a": action, "k": key},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        if row["request_digest"] != digest(payload):
            raise IdempotencyConflict()
        self.task_row(row["task_id"])
        return operation_view(row)

    def queue(
        self,
        task_id: UUID,
        action: str,
        key: str,
        payload: dict[str, Any],
        provider_id: UUID | None = None,
    ) -> NetworkOperation:
        row = (
            self.session.execute(
                text("""INSERT INTO federation.operations(id,tenant_id,task_id,created_by,action,provider_id,payload,request_digest,idempotency_key)
              VALUES(:id,:t,:task,:s,:a,:provider,CAST(:payload AS jsonb),:digest,:key) RETURNING *"""),
                {
                    "id": uuid4(),
                    "t": self.tenant,
                    "task": task_id,
                    "s": self.principal.subject,
                    "a": action,
                    "provider": provider_id,
                    "payload": encode(payload),
                    "digest": digest(payload),
                    "key": key,
                },
            )
            .mappings()
            .one()
        )
        return operation_view(row)

    def create(self, body: CreateNetworkTask, key: str) -> NetworkOperation:
        self.authorize("project:write")
        payload = body.model_dump(mode="json")
        if body.correction_of is None:
            payload.pop("correction_of", None)
        if previous := self.existing("publish_task", key, payload):
            return previous
        run = Prototype(self.session, self.tenant, self.project, self.principal).run_row(
            body.source_run_id
        )
        if run["status"] != "succeeded" or run["result"]["source_mode"] != "real":
            raise ValueError("a successful real-source result is required")
        if body.approve_public_reference and not run["result"].get("redistribution_permitted"):
            raise ValueError("source policy does not permit public coordination")
        review_digest: str | None = None
        review_expected: dict[str, Any] | None = None
        correction_digest: str | None = None
        challenge_digest: str | None = None
        if body.correction_of:
            if body.capability != "sst_reproduction":
                raise ValueError("corrections require a new reproduction task")
            original = self.task_row(body.correction_of)
            if original["state"] != "completed" or original["source_run_id"] != body.source_run_id:
                raise ValueError(
                    "correction target must be a completed task on the same source run"
                )
            correction_digest = self.session.execute(
                text(
                    "SELECT envelope->>'content_digest' FROM federation.artifacts WHERE tenant_id=:t AND task_id=:id"
                ),
                {"t": self.tenant, "id": body.correction_of},
            ).scalar_one()
            challenge_digest = self.session.execute(
                text(
                    "SELECT a.envelope->>'content_digest' FROM federation.artifacts a JOIN federation.tasks t ON t.tenant_id=a.tenant_id AND t.id=a.task_id WHERE t.tenant_id=:tenant AND t.project_id=:project AND t.review_of=:original AND a.envelope->'payload'->'results'->>'verdict'='challenged' ORDER BY a.created_at DESC LIMIT 1"
                ),
                {"tenant": self.tenant, "project": self.project, "original": body.correction_of},
            ).scalar_one_or_none()
            if not challenge_digest:
                raise ValueError(
                    "a signed challenged review is required before creating a correction"
                )
        if body.capability == "sst_review":
            if body.review_of is None:
                raise ValueError("review_of is required for a review task")
            target = self.task_row(body.review_of)
            if (
                target["state"] != "completed"
                or target["source_run_id"] != body.source_run_id
                or target["capability"] != "sst_reproduction"
            ):
                raise ValueError(
                    "review target must be a completed reproduction of the same source run"
                )
            review_digest = self.session.execute(
                text(
                    "SELECT envelope->>'content_digest' FROM federation.artifacts WHERE tenant_id=:t AND task_id=:id"
                ),
                {"t": self.tenant, "id": body.review_of},
            ).scalar_one()
            reviewed_artifact: dict[str, Any] = self.session.execute(
                text(
                    "SELECT envelope FROM federation.artifacts WHERE tenant_id=:t AND task_id=:id"
                ),
                {"t": self.tenant, "id": body.review_of},
            ).scalar_one()
            review_expected = {
                name: reviewed_artifact["payload"]["results"][name]
                for name in ("current_mean", "baseline_mean", "anomaly", "daily")
            }
        elif body.review_of is not None:
            raise ValueError("reproduction tasks cannot name a review target")
        result = run["result"]
        recipe = {
            "schema_version": "orca.seasonal-delegation/1.0",
            "capability": body.capability,
            "method": result["method"],
            "bundle_digest": result["bundle_digest"],
            "metadata_digest": result["metadata_digest"],
            "input_snapshots": result["input_snapshots"],
            "bbox": result["bbox"],
            "source_result_digest": run["result_digest"],
            "expected": review_expected
            or {
                name: result[name] for name in ("current_mean", "baseline_mean", "anomaly", "daily")
            },
            "review_of_digest": review_digest,
            "correction_of": str(body.correction_of) if body.correction_of else None,
            "corrects_digest": correction_digest,
            "challenge_digest": challenge_digest,
            "scientific_status": "prototype_not_independently_validated",
        }
        identifier = uuid4()
        self.session.execute(
            text("""INSERT INTO federation.tasks(id,tenant_id,project_id,source_run_id,capability,review_of,publication_mode,created_by,recipe,recipe_digest,deadline)
              VALUES(:id,:t,:p,:run,:cap,:review,:mode,:s,CAST(:recipe AS jsonb),:digest,:deadline)"""),
            {
                "id": identifier,
                "t": self.tenant,
                "p": self.project,
                "run": body.source_run_id,
                "cap": body.capability,
                "review": body.review_of,
                "mode": "public_reference" if body.approve_public_reference else "private",
                "s": self.principal.subject,
                "recipe": encode(recipe),
                "digest": digest(recipe),
                "deadline": datetime.now(timezone.utc) + timedelta(seconds=body.deadline_seconds),
            },
        )
        return self.queue(identifier, "publish_task", key, payload)

    def request_claim(
        self, identifier: UUID, provider: UUID, key: str, entity_key: str | None = None
    ) -> NetworkOperation:
        self.authorize("project:write")
        payload = {"task_id": str(identifier), "provider_id": str(provider)}
        action = "import_claim" if entity_key else "claim"
        if entity_key:
            payload["entity_key"] = entity_key
        if previous := self.existing(action, key, payload):
            return previous
        task = self.task_row(identifier, lock=True)
        if task["state"] != "open" or task["deadline"] <= datetime.now(timezone.utc):
            raise ValueError("task is not open for claims")
        if not self.session.execute(
            text(
                "SELECT 1 FROM federation.events WHERE tenant_id=:t AND task_id=:id AND kind='task' LIMIT 1"
            ),
            {"t": self.tenant, "id": identifier},
        ).scalar_one_or_none():
            raise ValueError(
                "task advertisement is still pending; wait for its operation to succeed"
            )
        binding = self.agent(provider)
        if binding.capability != task["capability"]:
            raise ValueError("provider does not have the required capability")
        if task["review_of"]:
            author: UUID = self.session.execute(
                text(
                    "SELECT provider_id FROM federation.artifacts WHERE tenant_id=:t AND task_id=:id"
                ),
                {"t": self.tenant, "id": task["review_of"]},
            ).scalar_one()
            if author == provider:
                raise ValueError("the result producer cannot review its own artifact")
            producer = self.agent(author)
            if (
                producer.wallet.lower() == binding.wallet.lower()
                or producer.public_key == binding.public_key
            ):
                raise ValueError("review requires a separately keyed provider")
        if entity_key and task["publication_mode"] != "public_reference":
            raise ValueError("a private task cannot import public claims")
        return self.queue(identifier, action, key, payload, provider)

    def assign(self, identifier: UUID, body: AssignClaim, key: str) -> NetworkOperation:
        self.authorize("project:write")
        payload = {"task_id": str(identifier), **body.model_dump(mode="json")}
        if previous := self.existing("dispatch", key, payload):
            return previous
        task = self.task_row(identifier, lock=True)
        if (
            task["state"] != "open"
            or task["generation"] != body.expected_generation
            or task["deadline"] <= datetime.now(timezone.utc)
        ):
            raise ValueError("task generation or state changed; reload before assigning")
        claim = (
            self.session.execute(
                text(
                    "SELECT * FROM federation.claims WHERE tenant_id=:t AND task_id=:task AND id=:id"
                ),
                {"t": self.tenant, "task": identifier, "id": body.claim_id},
            )
            .mappings()
            .one_or_none()
        )
        if claim is None:
            raise LookupError("claim not found")
        self.agent(claim["provider_id"])
        generation = task["generation"] + 1
        assignment, attempt = uuid4(), uuid4()
        self.session.execute(
            text("""INSERT INTO federation.assignments(id,tenant_id,task_id,generation,claim_id,provider_id,attempt_id,lease_until)
              VALUES(:id,:t,:task,:generation,:claim,:provider,:attempt,:deadline)"""),
            {
                "id": assignment,
                "t": self.tenant,
                "task": identifier,
                "generation": generation,
                "claim": body.claim_id,
                "provider": claim["provider_id"],
                "attempt": attempt,
                "deadline": task["deadline"],
            },
        )
        self.session.execute(
            text(
                "UPDATE federation.tasks SET state='assigned',generation=:g WHERE tenant_id=:t AND id=:id"
            ),
            {"g": generation, "t": self.tenant, "id": identifier},
        )
        return self.queue(identifier, "dispatch", key, payload, claim["provider_id"])

    def cancel(self, identifier: UUID, generation: int, key: str) -> NetworkOperation:
        self.authorize("project:write")
        payload = {"task_id": str(identifier), "expected_generation": generation}
        if previous := self.existing("cancel", key, payload):
            return previous
        task = self.task_row(identifier, lock=True)
        if task["state"] in {"completed", "cancelled"} or task["generation"] != generation:
            raise ValueError("task already terminal or generation changed")
        self.session.execute(
            text("UPDATE federation.tasks SET state='cancelled' WHERE tenant_id=:t AND id=:id"),
            {"t": self.tenant, "id": identifier},
        )
        self.session.execute(
            text(
                "UPDATE federation.assignments SET state='cancelled' WHERE tenant_id=:t AND task_id=:id AND state='active'"
            ),
            {"t": self.tenant, "id": identifier},
        )
        return self.queue(identifier, "cancel", key, payload)

    def detail(self, identifier: UUID) -> NetworkTask:
        task = dict(self.task_row(identifier))
        task["correction_of"] = task["recipe"].get("correction_of")
        for table in ("claims", "assignments", "artifacts", "operations"):
            rows = self.session.execute(
                text(
                    f"SELECT * FROM federation.{table} WHERE tenant_id=:t AND task_id=:id ORDER BY created_at LIMIT 100"
                ),
                {"t": self.tenant, "id": identifier},
            ).mappings()
            if table == "operations":
                task[table] = [operation_view(row) for row in rows]
            else:
                task[table] = [
                    {
                        field: row[field]
                        for field in ("id", "provider_id", "envelope", "created_at")
                        if field in row
                    }
                    | (
                        {
                            "generation": row["generation"],
                            "state": row["state"],
                            "a2a_task_id": row["a2a_task_id"],
                            "a2a_context_id": row["a2a_context_id"],
                        }
                        if table == "assignments"
                        else {}
                    )
                    for row in rows
                ]
        return NetworkTask.model_validate(
            {field: task[field] for field in NetworkTask.model_fields}
        )

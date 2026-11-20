"""Purpose: coordinate authorized research delegation with atomic intents and exclusive assignment.
Behavior: retain tenant isolation, idempotency, current bindings and immutable signed evidence.
Refs: H04-agent-coordination.json; main_implementation plan.md P11.2–P11.4; api design.md §3.12. Phase: H04/P11.
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
    SignedEnvelope,
)
from orca.domain.identity import Principal
from orca.infrastructure.federation.signing import digest, verify


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
        if not body.publish_arkiv_v1:
            payload.pop("publish_arkiv_v1", None)
        elif not body.approve_public_reference:
            raise ValueError("Arkiv v1 publication requires explicit public-reference approval")
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

    def request_arkiv_recovery(self, identifier: UUID) -> NetworkOperation:
        self.authorize("project:write")
        params = {"t": self.tenant, "id": identifier, "s": self.principal.subject}
        row = (
            self.session.execute(
                text("""SELECT * FROM federation.operations
              WHERE tenant_id=:t AND id=:id AND created_by=:s FOR UPDATE"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Operation not found for the requesting operator")
        task = self.task_row(row["task_id"], lock=True)
        if row["payload"].get("arkiv_readback_only") and row["status"] in {
            "queued",
            "running",
            "succeeded",
        }:
            return operation_view(row)
        if (
            row["status"] != "reconciliation_required"
            or row["error_code"] != "ARKIV_V1_RECONCILIATION_REQUIRED"
            or row["action"] not in {"publish_task", "dispatch"}
            or task["publication_mode"] != "public_reference"
            or task["state"] != ("open" if row["action"] == "publish_task" else "assigned")
            or task["deadline"] <= datetime.now(timezone.utc)
            or row["attempts"] >= 3
        ):
            raise ValueError("This operation cannot resume through Arkiv readback")
        updated = (
            self.session.execute(
                text("""UPDATE federation.operations SET status='queued', error_code=NULL,
              payload=jsonb_set(payload,'{arkiv_readback_only}','true'::jsonb),
              updated_at=clock_timestamp()
              WHERE tenant_id=:t AND id=:id AND created_by=:s RETURNING *"""),
                params,
            )
            .mappings()
            .one()
        )
        return operation_view(updated)

    def request_claim(
        self,
        identifier: UUID,
        provider: UUID,
        key: str,
        entity_key: str | None = None,
        arkiv_v1_entity_key: str | None = None,
    ) -> NetworkOperation:
        self.authorize("project:write")
        payload = {"task_id": str(identifier), "provider_id": str(provider)}
        action = "import_claim" if entity_key else "claim"
        if entity_key:
            payload["entity_key"] = entity_key
        if arkiv_v1_entity_key:
            payload["arkiv_v1_entity_key"] = arkiv_v1_entity_key
        if previous := self.existing(action, key, payload):
            return previous
        task = self.task_row(identifier, lock=True)
        if task["state"] != "open" or task["deadline"] <= datetime.now(timezone.utc):
            raise ValueError("task is not open for claims")
        event = (
            self.session.execute(
                text(
                    "SELECT receipt FROM federation.events WHERE tenant_id=:t AND task_id=:id AND kind='task' LIMIT 1"
                ),
                {"t": self.tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if event is None:
            raise ValueError(
                "task advertisement is still pending; wait for its operation to succeed"
            )
        has_v1 = bool((event["receipt"] or {}).get("arkiv_v1"))
        if has_v1 != bool(arkiv_v1_entity_key) or has_v1 and not entity_key:
            raise ValueError("Arkiv v1 tasks require both the signed and v1 claim references")
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

    def arkiv_evidence_intent(self, identifier: UUID, coordinator: AgentBinding) -> dict[str, Any]:
        task = self._public_evidence_task(identifier)
        producer, artifact = self._accepted_artifact(identifier, coordinator)
        if task["capability"] == "sst_review":
            target = self._public_evidence_task(task["review_of"])
            original_producer, result = self._accepted_artifact(target["id"], coordinator)
            if (
                target["capability"] != "sst_reproduction"
                or target["source_run_id"] != task["source_run_id"]
                or producer.id == original_producer.id
                or producer.wallet.lower() == original_producer.wallet.lower()
                or producer.public_key == original_producer.public_key
                or artifact.payload["review_of_digest"] != result.content_digest
            ):
                raise ValueError(
                    "Review requires separate ownership and the accepted result digest"
                )
            verdicts = {"reproduced": "agrees", "challenged": "challenges"}
            verdict = verdicts.get(artifact.payload["results"]["verdict"])
            if verdict is None:
                raise ValueError("Unsupported signed review verdict")
            kind = "review"
            attributes = {
                "resultDigest": result.content_digest,
                "reviewDigest": artifact.content_digest,
                "reviewerId": str(producer.id),
                "verdict": verdict,
            }
        else:
            kind = "result"
            attributes = {
                "taskId": str(identifier),
                "artifactDigest": artifact.content_digest,
                "methodId": artifact.payload["method_ref"],
                "status": "inconclusive",
            }
        return {
            "kind": kind,
            "owner": producer.wallet,
            "lifetimeSeconds": 2419200,
            "attributes": attributes,
        }

    def _public_evidence_task(self, identifier: UUID) -> Any:
        task = self.task_row(identifier)
        if task["state"] != "completed" or task["publication_mode"] != "public_reference":
            raise LookupError("No approved public evidence for this task")
        receipt: dict[str, Any] | None = self.session.execute(
            text(
                "SELECT receipt FROM federation.events WHERE tenant_id=:t AND task_id=:id AND kind='task'"
            ),
            {"t": self.tenant, "id": identifier},
        ).scalar_one_or_none()
        if not (receipt or {}).get("arkiv_v1"):
            raise LookupError("No Arkiv v1 publication approval for this task")
        return task

    def _accepted_artifact(
        self, task_id: UUID, coordinator: AgentBinding
    ) -> tuple[AgentBinding, SignedEnvelope]:
        row = (
            self.session.execute(
                text("""SELECT ar.provider_id,ar.envelope FROM federation.artifacts ar
              JOIN federation.assignments a
              ON a.tenant_id=ar.tenant_id AND a.id=ar.assignment_id AND a.task_id=ar.task_id
              WHERE ar.tenant_id=:t AND ar.task_id=:task AND a.state='completed'
              AND a.provider_id=ar.provider_id"""),
                {"t": self.tenant, "task": task_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("No accepted artifact for this task")
        actor = self.agent(row["provider_id"])
        envelope = SignedEnvelope.model_validate(row["envelope"])
        verify(envelope, actor.public_key, actor.key_id, str(coordinator.id))
        if (
            envelope.payload.get("task_id") != str(task_id)
            or envelope.payload.get("producer_agent_id") != str(actor.id)
            or envelope.payload.get("binding_version") != str(actor.binding_version)
        ):
            raise ValueError("Accepted artifact differs from the current provider binding")
        return actor, envelope

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

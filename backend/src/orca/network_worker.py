"""Purpose: execute durable agent coordination through real A2A and Arkiv adapters.
Behavior: fence workers, recheck authority, verify separate signatures/owners and reject stale results.
Refs: H04 decision; canonical assignment/outbox/artifact guides; Phase: H04.
"""

import argparse
import time
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from a2a.client.errors import A2AClientHTTPError, A2AClientTimeoutError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from orca.application.network import Network, encode
from orca.config import Settings
from orca.contracts.network import SignedEnvelope
from orca.domain.agent_results import validate_comparison
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.tenant_context import assert_runtime_role
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.a2a_client import exchange
from orca.infrastructure.federation.public_events import public_event
from orca.infrastructure.federation.runtime import ChainUnavailable, NetworkRuntime, load_runtime
from orca.infrastructure.federation.signing import CHAIN_ID, DOMAIN, sign, verify


def actor(subject: str) -> Principal:
    return Principal(
        subject, "authorized-background-operation", frozenset().union(*ROLE_SCOPES.values())
    )


class AssignmentConflict(ValueError):
    """Requester-signed incompatible selections require an explicit resolution/new task."""


def save_event(
    session: Session,
    tenant: UUID,
    task_id: UUID,
    envelope: SignedEnvelope,
    mode: str,
    receipt: dict[str, Any] | None,
) -> None:
    previous = (
        session.execute(
            text("SELECT envelope,receipt FROM federation.events WHERE tenant_id=:t AND id=:id"),
            {"t": tenant, "id": envelope.payload["event_id"]},
        )
        .mappings()
        .one_or_none()
    )
    if previous is not None:
        if previous["envelope"] != envelope.model_dump(mode="json"):
            raise ValueError("immutable event replay differs")
        return
    session.execute(
        text("""INSERT INTO federation.events(id,tenant_id,task_id,kind,actor_id,envelope,publication_mode,receipt)
      VALUES(:id,:t,:task,:kind,:actor,CAST(:envelope AS jsonb),:mode,CAST(:receipt AS jsonb))"""),
        {
            "id": envelope.payload["event_id"],
            "t": tenant,
            "task": task_id,
            "kind": envelope.payload["kind"],
            "actor": envelope.payload["actor_id"],
            "envelope": envelope.model_dump_json(),
            "mode": mode,
            "receipt": encode(receipt),
        },
    )


def event_row(session: Session, tenant: UUID, task: UUID, kind: str) -> dict[str, Any]:
    row = (
        session.execute(
            text(
                "SELECT envelope,receipt FROM federation.events WHERE tenant_id=:t AND task_id=:id AND kind=:kind ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tenant, "id": task, "kind": kind},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("required coordination event is not available yet")
    return dict(row)


def reject_conflicting_assignments(
    runtime: NetworkRuntime, task: dict[str, Any], expected: SignedEnvelope
) -> None:
    if task["publication_mode"] != "public_reference":
        return
    board = runtime.gateway.call({"command": "query", "task_id": str(task["id"])})
    if board["truncated"]:
        raise ValueError("assignment board is incomplete")
    coordinator = runtime.configuration.coordinator
    for record in board["records"]:
        envelope = SignedEnvelope.model_validate(record["envelope"])
        payload = envelope.payload
        if payload.get("kind") != "assignment" or payload.get("assignment_generation") != str(
            task["generation"]
        ):
            continue
        if record["owner"].lower() != coordinator.wallet.lower():
            continue
        verify(envelope, coordinator.public_key, coordinator.key_id, "public")
        if (payload.get("reference_digest"), payload.get("subject_id")) != (
            expected.payload["reference_digest"],
            expected.payload["subject_id"],
        ):
            raise AssignmentConflict(
                "conflicting requester assignments require explicit resolution"
            )


def process(db: Engine, runtime: NetworkRuntime, tenant: UUID, operation_id: UUID) -> bool:
    token = uuid4()
    with TenantUnitOfWork(db, tenant) as session:
        row = (
            session.execute(
                text("""SELECT * FROM federation.operations WHERE tenant_id=:t AND id=:id
          AND (status='queued' OR (status='running' AND lease_until<clock_timestamp())) FOR UPDATE SKIP LOCKED"""),
                {"t": tenant, "id": operation_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        if row["attempts"] >= 3:
            session.execute(
                text(
                    "UPDATE federation.operations SET status='failed',error_code='RETRY_LIMIT' WHERE tenant_id=:t AND id=:id"
                ),
                {"t": tenant, "id": operation_id},
            )
            return True
        job = dict(row)
        session.execute(
            text(
                "UPDATE federation.operations SET status='running',worker_token=:token,lease_until=clock_timestamp()+interval '600 seconds',attempts=attempts+1,updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id"
            ),
            {"token": token, "t": tenant, "id": operation_id},
        )
    try:
        with TenantUnitOfWork(db, tenant) as session:
            task = dict(
                session.execute(
                    text("SELECT * FROM federation.tasks WHERE tenant_id=:t AND id=:id"),
                    {"t": tenant, "id": job["task_id"]},
                )
                .mappings()
                .one()
            )
            service = Network(session, tenant, task["project_id"], actor(job["created_by"]))
            service.authorize("project:write")
            if job["action"] != "cancel" and (
                task["state"] == "cancelled" or task["deadline"] <= datetime.now(timezone.utc)
            ):
                raise ValueError("task cancelled or deadline expired")
            provider = service.agent(job["provider_id"]) if job["provider_id"] else None
            if provider and provider not in runtime.configuration.agents:
                raise ValueError("active binding differs from pinned transport configuration")
            assignment = None
            claim = None
            parent = None
            if job["action"] in {"claim", "import_claim"}:
                if task["state"] != "open":
                    raise ValueError("task is no longer open for claims")
                if session.execute(
                    text(
                        "SELECT 1 FROM federation.claims WHERE tenant_id=:t AND task_id=:id AND provider_id=:p"
                    ),
                    {"t": tenant, "id": task["id"], "p": job["provider_id"]},
                ).scalar_one_or_none():
                    finish(session, tenant, operation_id, token)
                    return True
                parent = event_row(session, tenant, task["id"], "task")
            elif job["action"] == "dispatch":
                assignment = dict(
                    session.execute(
                        text(
                            "SELECT * FROM federation.assignments WHERE tenant_id=:t AND task_id=:id AND generation=:g AND state='active'"
                        ),
                        {"t": tenant, "id": task["id"], "g": task["generation"]},
                    )
                    .mappings()
                    .one()
                )
                claim = dict(
                    session.execute(
                        text("SELECT * FROM federation.claims WHERE tenant_id=:t AND id=:id"),
                        {"t": tenant, "id": assignment["claim_id"]},
                    )
                    .mappings()
                    .one()
                )
                parent = event_row(session, tenant, task["id"], "claim")
                # The parent must be this selected provider's claim, not the last bidder's record.
                parent = dict(
                    session.execute(
                        text(
                            "SELECT envelope,receipt FROM federation.events WHERE tenant_id=:t AND task_id=:id AND kind='claim' AND actor_id=:provider ORDER BY created_at DESC LIMIT 1"
                        ),
                        {"t": tenant, "id": task["id"], "provider": job["provider_id"]},
                    )
                    .mappings()
                    .one()
                )
        emitted: list[tuple[SignedEnvelope, dict[str, Any] | None]] = []
        accepted_artifact: SignedEnvelope | None = None
        claim_document: SignedEnvelope | None = None
        signed_assignment: SignedEnvelope | None = None
        remote_task_id: str | None = None
        remote_context_id: str | None = None
        coordinator = runtime.configuration.coordinator
        issued_at = job["created_at"].isoformat()
        display_task = {
            key: str(task[key]) if key in {"id", "deadline"} else task[key]
            for key in (
                "id",
                "deadline",
                "recipe_digest",
                "publication_mode",
                "capability",
                "generation",
            )
        }
        display_task["deadline"] = task["deadline"].isoformat()
        display_task["source_mode"] = "real"
        if job["action"] in {"publish_task", "cancel"}:
            kind = "task" if job["action"] == "publish_task" else "cancellation"
            event = public_event(
                task=task,
                kind=kind,
                actor=str(coordinator.id),
                binding_version=str(coordinator.binding_version),
                subject=str(task["id"]),
                generation=task["generation"],
                parent_key="",
                reference_digest=task["recipe_digest"],
                operation_id=str(operation_id),
                issued_at=issued_at,
                private_key=runtime.private_key,
                key_id=coordinator.key_id,
            )
            receipt = (
                runtime.gateway.publish(event, coordinator)
                if task["publication_mode"] == "public_reference"
                else None
            )
            emitted.append((event, receipt))
        elif job["action"] == "import_claim":
            if provider is None or parent is None or task["publication_mode"] != "public_reference":
                raise ValueError("public claim import context missing")
            observed = runtime.gateway.call(
                {
                    "command": "read",
                    "entity_key": job["payload"]["entity_key"],
                    "owner": provider.wallet,
                }
            )
            public = SignedEnvelope.model_validate(observed["envelope"])
            from orca.infrastructure.federation.runtime import verify_chain_record

            verify_chain_record(observed, provider, public)
            payload = public.payload
            if (
                payload["kind"] != "claim"
                or payload["task_id"] != str(task["id"])
                or payload["recipe_digest"] != task["recipe_digest"]
                or payload["parent_key"] != parent["receipt"]["entity_key"]
                or payload.get("source_mode", "real") != "real"
                or payload["assignment_generation"] != "0"
            ):
                raise ValueError(
                    "discovered public claim is unrelated to the selected open real-source task"
                )
            receipt = {
                "state": "indexed",
                "entity_key": observed["entity_key"],
                "owner": observed["owner"],
                "content_digest": public.content_digest,
                "chain_id": 7794095,
                "read_back": observed,
            }
            claim_document = public
            emitted.append((public, receipt))
        else:
            if provider is None or parent is None:
                raise ValueError("provider task context missing")
            if task["publication_mode"] == "public_reference":
                parent_binding = coordinator if job["action"] == "claim" else provider
                runtime.gateway.read(
                    parent["receipt"]["entity_key"],
                    parent_binding,
                    SignedEnvelope.model_validate(parent["envelope"]),
                )
            data: dict[str, Any] = {
                "operation": "claim" if job["action"] == "claim" else "execute",
                "task": display_task,
                "issued_at": issued_at,
                "requester_event": parent,
            }
            if assignment is not None and claim is not None:
                signed_assignment = sign(
                    {
                        "schema_version": "orca.assignment/1.0",
                        "domain": DOMAIN,
                        "chain_id": CHAIN_ID,
                        "audience": str(provider.id),
                        "task_id": str(task["id"]),
                        "assignment_generation": task["generation"],
                        "binding_version": str(coordinator.binding_version),
                        "provider_binding_version": str(provider.binding_version),
                        "provider_id": str(provider.id),
                        "attempt_id": str(assignment["attempt_id"]),
                        "recipe_digest": task["recipe_digest"],
                        "claim_digest": claim["envelope"]["content_digest"],
                        "deadline": task["deadline"].isoformat(),
                    },
                    runtime.private_key,
                    coordinator.key_id,
                )
                event = public_event(
                    task=task,
                    kind="assignment",
                    actor=str(coordinator.id),
                    binding_version=str(coordinator.binding_version),
                    subject=str(provider.id),
                    generation=task["generation"],
                    parent_key=parent["receipt"]["entity_key"] if parent["receipt"] else "",
                    reference_digest=signed_assignment.content_digest,
                    operation_id=str(operation_id),
                    issued_at=issued_at,
                    private_key=runtime.private_key,
                    key_id=coordinator.key_id,
                )
                receipt = (
                    runtime.gateway.publish(event, coordinator)
                    if task["publication_mode"] == "public_reference"
                    else None
                )
                reject_conflicting_assignments(runtime, task, event)
                emitted.append((event, receipt))
                data.update(
                    {
                        "assignment": signed_assignment.model_dump(mode="json"),
                        "recipe": task["recipe"],
                        "claim": claim["envelope"],
                        "requester_event": {
                            "envelope": event.model_dump(mode="json"),
                            "receipt": receipt,
                        },
                    }
                )
            response = exchange(runtime, provider, str(operation_id), data)
            remote_task_id = response["a2a_task_id"]
            remote_context_id = response["a2a_context_id"]
            envelope = SignedEnvelope.model_validate(response["envelope"])
            verify(envelope, provider.public_key, provider.key_id, str(coordinator.id))
            payload = envelope.payload
            if (
                payload["task_id"] != str(task["id"])
                or payload["recipe_digest"] != task["recipe_digest"]
                or payload["binding_version"] != str(provider.binding_version)
            ):
                raise ValueError("provider artifact binding differs from task")
            public = SignedEnvelope.model_validate(response["public_event"])
            verify(public, provider.public_key, provider.key_id, "public")
            if (
                public.payload["reference_digest"] != envelope.content_digest
                or public.payload["task_id"] != str(task["id"])
                or public.payload["actor_id"] != str(provider.id)
            ):
                raise ValueError("public provider record differs from received artifact")
            expected_kind = (
                "claim"
                if job["action"] == "claim"
                else "review"
                if provider.capability == "sst_review"
                else "result"
            )
            expected_subject = (
                str(provider.id) if job["action"] == "claim" else payload["artifact_id"]
            )
            if (
                public.payload["kind"] != expected_kind
                or public.payload["binding_version"] != str(provider.binding_version)
                or public.payload["recipe_digest"] != task["recipe_digest"]
                or public.payload["assignment_generation"]
                != str(0 if job["action"] == "claim" else task["generation"])
                or public.payload["subject_id"] != expected_subject
                or public.payload["parent_key"]
                != (
                    data["requester_event"]["receipt"]["entity_key"]
                    if data["requester_event"]["receipt"]
                    else ""
                )
            ):
                raise ValueError("provider public linkage differs from selected task/assignment")
            if task["publication_mode"] == "public_reference":
                runtime.gateway.read(response["receipt"]["entity_key"], provider, public)
            emitted.append((public, response["receipt"]))
            if job["action"] == "claim":
                if (
                    payload["provider_id"] != str(provider.id)
                    or payload["assignment_generation"] != 0
                    or public.payload["kind"] != "claim"
                ):
                    raise ValueError("provider claim differs")
                claim_document = envelope
            else:
                if assignment is None or signed_assignment is None:
                    raise ValueError("assignment context missing")
                if (
                    payload["assignment_generation"] != task["generation"]
                    or payload["attempt_id"] != str(assignment["attempt_id"])
                    or payload["producer_agent_id"] != str(provider.id)
                    or payload["review_of_digest"] != task["recipe"]["review_of_digest"]
                    or payload.get("corrects_digest") != task["recipe"].get("corrects_digest")
                ):
                    raise ValueError("stale or unrelated provider artifact")
                if (
                    payload["results"]["verdict"] not in {"reproduced", "challenged"}
                    or payload["results"]["source_mode"] != "real"
                ):
                    raise ValueError("unsupported scientific verdict")
                reject_conflicting_assignments(runtime, task, emitted[0][0])
                accepted_artifact = envelope
                if (
                    payload["results"].get("operator_mode") != "team_controlled"
                    or payload["results"].get("scientific_status")
                    != "prototype_not_independently_validated"
                ):
                    raise ValueError(
                        "provider trust/scientific labels differ from the configured prototype"
                    )
                validate_comparison(payload["results"], task["recipe"]["expected"])
        with TenantUnitOfWork(db, tenant) as session:
            active_job: UUID | None = session.execute(
                text(
                    "SELECT worker_token FROM federation.operations WHERE tenant_id=:t AND id=:id FOR UPDATE"
                ),
                {"t": tenant, "id": operation_id},
            ).scalar_one()
            if active_job != token:
                return False
            service = Network(session, tenant, task["project_id"], actor(job["created_by"]))
            service.authorize("project:write")
            current = service.task_row(task["id"], lock=True)
            if job["action"] != "cancel" and (
                current["state"] == "cancelled"
                or current["generation"] != task["generation"]
                or current["deadline"] <= datetime.now(timezone.utc)
            ):
                raise ValueError("late result rejected after cancellation/reassignment/deadline")
            if provider and service.agent(provider.id) != provider:
                raise ValueError("provider revoked or binding changed during execution")
            for envelope, receipt in emitted:
                save_event(session, tenant, task["id"], envelope, task["publication_mode"], receipt)
            if claim_document is not None and provider is not None:
                session.execute(
                    text(
                        "INSERT INTO federation.claims(id,tenant_id,task_id,provider_id,envelope) VALUES(:id,:t,:task,:provider,CAST(:envelope AS jsonb)) ON CONFLICT(tenant_id,task_id,provider_id) DO NOTHING"
                    ),
                    {
                        "id": uuid4(),
                        "t": tenant,
                        "task": task["id"],
                        "provider": provider.id,
                        "envelope": claim_document.model_dump_json(),
                    },
                )
            if (
                accepted_artifact is not None
                and assignment is not None
                and provider is not None
                and signed_assignment is not None
            ):
                active = session.execute(
                    text(
                        "UPDATE federation.assignments SET state='completed',envelope=CAST(:envelope AS jsonb),a2a_task_id=:remote_task,a2a_context_id=:remote_context WHERE tenant_id=:t AND id=:id AND state='active' RETURNING id"
                    ),
                    {
                        "envelope": signed_assignment.model_dump_json(),
                        "t": tenant,
                        "id": assignment["id"],
                        "remote_task": remote_task_id,
                        "remote_context": remote_context_id,
                    },
                ).scalar_one_or_none()
                if active is None:
                    raise ValueError("assignment is no longer active")
                session.execute(
                    text(
                        "INSERT INTO federation.artifacts(id,tenant_id,task_id,assignment_id,provider_id,envelope) VALUES(:id,:t,:task,:assignment,:provider,CAST(:envelope AS jsonb))"
                    ),
                    {
                        "id": accepted_artifact.payload["artifact_id"],
                        "t": tenant,
                        "task": task["id"],
                        "assignment": assignment["id"],
                        "provider": provider.id,
                        "envelope": accepted_artifact.model_dump_json(),
                    },
                )
                session.execute(
                    text(
                        "UPDATE federation.tasks SET state='completed' WHERE tenant_id=:t AND id=:id"
                    ),
                    {"t": tenant, "id": task["id"]},
                )
            finish(session, tenant, operation_id, token)
        return True
    except Exception as error:
        reconciliation = isinstance(error, ChainUnavailable) and error.reconciliation
        transient = (
            isinstance(error, (httpx.TransportError, A2AClientTimeoutError))
            or isinstance(error, A2AClientHTTPError)
            and error.status_code >= 500
        ) and job["attempts"] < 2
        code = (
            "ARKIV_RECONCILIATION_REQUIRED"
            if reconciliation
            else "COORDINATION_FAILED_OR_AUTHORITY_CHANGED"
        )
        with TenantUnitOfWork(db, tenant) as session:
            if isinstance(error, AssignmentConflict):
                session.execute(
                    text(
                        "UPDATE federation.tasks SET state='conflicted' WHERE tenant_id=:t AND id=:id AND state NOT IN ('completed','cancelled')"
                    ),
                    {"t": tenant, "id": job["task_id"]},
                )
            session.execute(
                text(
                    "UPDATE federation.operations SET status=:status,error_code=:code,worker_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id AND worker_token=:token"
                ),
                {
                    "status": "reconciliation_required"
                    if reconciliation
                    else "queued"
                    if transient
                    else "failed",
                    "code": code,
                    "t": tenant,
                    "id": operation_id,
                    "token": token,
                },
            )
        return True


def finish(session: Session, tenant: UUID, operation: UUID, token: UUID) -> None:
    session.execute(
        text(
            "UPDATE federation.operations SET status='succeeded',error_code=NULL,worker_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id AND worker_token=:token"
        ),
        {"t": tenant, "id": operation, "token": token},
    )


def process_pending(db: Engine, runtime: NetworkRuntime) -> int:
    with db.connect() as connection:
        assert_runtime_role(connection)
        jobs = list(connection.execute(text("SELECT * FROM iam.network_pending_operations()")))
    return sum(
        int(process(db, runtime, UUID(str(tenant)), UUID(str(operation))))
        for tenant, operation in jobs
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url:
        raise SystemExit("runtime database required")
    runtime = load_runtime(settings)
    db = create_runtime_engine(settings.database_url)
    try:
        while True:
            process_pending(db, runtime)
            if args.once:
                break
            time.sleep(2)
    finally:
        db.dispose()


if __name__ == "__main__":
    main()

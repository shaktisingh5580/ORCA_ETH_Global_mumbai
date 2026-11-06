"""Purpose: rehearse the actual authorized research -> A2A agents -> Arkiv coordination journey.
Behavior: resume stable operation keys, poll durable jobs and save only public/read-back evidence.
Refs: H04-agent-coordination.json; ARKIV_A2A_FRONTEND_HANDOFF.md; Phase: H04.
"""

import argparse
import json
import time
from uuid import UUID

from sqlalchemy import text

from orca.api.routers.projects import resolve_project
from orca.application.network import Network, operation_view
from orca.config import Settings
from orca.contracts.network import AssignClaim, CreateNetworkTask, NetworkOperation, SignedEnvelope
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.runtime import ROOT, load_runtime


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.demo")
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--approve-public-reference", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url:
        raise ValueError("private runtime database required")
    runtime = load_runtime(settings)
    db = create_runtime_engine(settings.database_url)
    principal = Principal(
        args.subject, "trusted-operator-rehearsal", frozenset().union(*ROLE_SCOPES.values())
    )
    tenant, _ = resolve_project(db, principal, args.project_id)
    evidence = {
        "profile": "team-controlled-agent-coordination",
        "entrypoint": "authorized operator application service; browser session acceptance remains separate",
        "source_run_id": str(args.run_id),
        "public_reference_approved": args.approve_public_reference,
        "ens_verified": False,
        "institution_verified": False,
        "tasks": [],
    }

    def wait(operation: NetworkOperation) -> UUID:
        until = time.monotonic() + 300
        while time.monotonic() < until:
            with TenantUnitOfWork(db, tenant) as session:
                row = (
                    session.execute(
                        text("SELECT * FROM federation.operations WHERE tenant_id=:t AND id=:id"),
                        {"t": tenant, "id": operation.id},
                    )
                    .mappings()
                    .one()
                )
                current = operation_view(row)
            if current.status == "succeeded":
                print(
                    json.dumps(
                        {
                            "operation": str(current.id),
                            "action": current.action,
                            "status": current.status,
                        }
                    ),
                    flush=True,
                )
                return current.task_id
            if current.status in {"failed", "reconciliation_required"}:
                raise ValueError(
                    f"{current.action}: {current.status}; {current.error_code}; operation {current.id}"
                )
            time.sleep(1)
        raise TimeoutError(
            "operation still pending; rerun this script with the same arguments to resume"
        )

    try:
        parent = None
        for binding in runtime.configuration.agents:
            role = binding.capability
            with TenantUnitOfWork(db, tenant) as session:
                service = Network(session, tenant, args.project_id, principal)
                operation: NetworkOperation | None = service.create(
                    CreateNetworkTask(
                        project_id=args.project_id,
                        source_run_id=args.run_id,
                        capability=role,
                        review_of=parent if role == "sst_review" else None,
                        approve_public_reference=args.approve_public_reference,
                    ),
                    f"h04-live-task-{args.run_id}-{role}",
                )
            assert operation is not None
            task_id = wait(operation)
            with TenantUnitOfWork(db, tenant) as session:
                service = Network(session, tenant, args.project_id, principal)
                detail = service.detail(task_id)
                if detail.state != "completed":
                    operation = service.request_claim(
                        task_id, binding.id, f"h04-live-claim-{task_id}-{binding.id}"
                    )
                else:
                    operation = None
            if operation:
                wait(operation)
            with TenantUnitOfWork(db, tenant) as session:
                service = Network(session, tenant, args.project_id, principal)
                detail = service.detail(task_id)
                if detail.state != "completed":
                    operation = service.assign(
                        task_id,
                        AssignClaim(claim_id=detail.claims[0]["id"], expected_generation=0),
                        f"h04-live-assignment-{task_id}",
                    )
                else:
                    operation = None
            if operation:
                wait(operation)
            with TenantUnitOfWork(db, tenant) as session:
                service = Network(session, tenant, args.project_id, principal)
                detail = service.detail(task_id)
                rows = [
                    dict(row)
                    for row in session.execute(
                        text(
                            "SELECT envelope,receipt FROM federation.events WHERE tenant_id=:t AND task_id=:id ORDER BY created_at"
                        ),
                        {"t": tenant, "id": task_id},
                    ).mappings()
                ]
            verdict = detail.artifacts[0]["envelope"]["payload"]["results"]
            observations = []
            bindings = {
                str(item.id): item
                for item in [runtime.configuration.coordinator, *runtime.configuration.agents]
            }
            for row in rows:
                envelope = SignedEnvelope.model_validate(row["envelope"])
                if args.approve_public_reference:
                    observed = runtime.gateway.read(
                        row["receipt"]["entity_key"],
                        bindings[envelope.payload["actor_id"]],
                        envelope,
                    )
                    observations.append(
                        {
                            "kind": envelope.payload["kind"],
                            "entity_key": observed["entity_key"],
                            "owner": observed["owner"],
                            "tx_hash": row["receipt"].get("tx_hash"),
                            "content_digest": envelope.content_digest,
                            "observed_at": observed["observed_at"],
                            "verification": observed["verification"],
                        }
                    )
            evidence["tasks"].append(
                {
                    "id": str(task_id),
                    "capability": role,
                    "state": detail.state,
                    "provider_id": str(binding.id),
                    "wallet": binding.wallet,
                    "result_digest": detail.artifacts[0]["envelope"]["content_digest"],
                    "verdict": verdict["verdict"],
                    "anomaly": verdict["anomaly"],
                    "maximum_absolute_difference": verdict["maximum_absolute_difference"],
                    "implementation": verdict["implementation"],
                    "a2a_task_id": detail.assignments[0]["a2a_task_id"],
                    "a2a_context_id": detail.assignments[0]["a2a_context_id"],
                    "chain_records": observations,
                }
            )
            parent = task_id
            target = ROOT / ".cache/network/live-rehearsal.json"
            target.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "task_id": str(task_id),
                        "capability": role,
                        "verdict": verdict["verdict"],
                        "chain_records": len(observations),
                    }
                ),
                flush=True,
            )
        evidence["status"] = "LIVE_REPRODUCTION_AND_REVIEW_COMPLETE"
        (ROOT / ".cache/network/live-rehearsal.json").write_text(
            json.dumps(evidence, indent=2), encoding="utf-8"
        )
    finally:
        db.dispose()


if __name__ == "__main__":
    main()

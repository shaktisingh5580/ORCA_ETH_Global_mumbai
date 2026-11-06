"""Purpose: execute actual A2A disagreements caused by deliberately altered synthetic observations.
Behavior: persist immutable candidate/challenge/correction/re-review stages and optional labelled chain records.
Refs: H05 decision; disagreement frontend handoff; Phase: H05; isolated demo tooling only.
"""

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from disagreement_store import DemoStore

from orca.application.workspace_access import authorize_project
from orca.config import Settings
from orca.contracts.network import SignedEnvelope
from orca.domain.agent_results import validate_comparison
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.a2a_client import exchange
from orca.infrastructure.federation.public_events import public_event
from orca.infrastructure.federation.runtime import (
    NetworkConfiguration,
    NetworkRuntime,
    load_runtime,
)
from orca.infrastructure.federation.signing import (
    CHAIN_ID,
    DOMAIN,
    digest,
    sign,
    strict_json,
    verify,
)


def recipe_for(inputs, phase, stages):
    source = inputs["biased"] if phase == 0 else inputs["clean"]
    expected = source if phase in {0, 2} else stages[phase - 1]["artifact"]["payload"]["results"]
    return {
        "schema_version": "orca.seasonal-delegation/1.0",
        "capability": "sst_review" if phase in {1, 3} else "sst_reproduction",
        "method": source["method"],
        "bundle_digest": digest(
            {"metadata": source["metadata_digest"], "members": source["input_snapshots"]}
        ),
        "metadata_digest": source["metadata_digest"],
        "input_snapshots": source["input_snapshots"],
        "bbox": source["bbox"],
        "source_result_digest": digest(
            {name: expected[name] for name in ("current_mean", "baseline_mean", "anomaly", "daily")}
        ),
        "expected": {
            name: expected[name] for name in ("current_mean", "baseline_mean", "anomaly", "daily")
        },
        "review_of_digest": stages[phase - 1]["artifact"]["content_digest"]
        if phase in {1, 3}
        else None,
        "corrects_digest": stages[0]["artifact"]["content_digest"] if phase == 2 else None,
        "challenge_digest": stages[1]["artifact"]["content_digest"] if phase == 2 else None,
        "source_mode": "synthetic_demo",
        "input_variant": "biased" if phase == 0 else "clean",
        "scientific_status": "prototype_not_independently_validated",
    }


def delegate(runtime, run, phase, recipe):
    task_id = uuid5(UUID(run["id"]), f"demo-task-{phase}")
    issued = run["created_at"]
    task = {
        "id": str(task_id),
        "capability": recipe["capability"],
        "recipe_digest": digest(recipe),
        "deadline": (datetime.fromisoformat(issued) + timedelta(hours=24)).isoformat(),
        "generation": 0,
        "publication_mode": "public_reference" if run["public_reference"] else "private",
        "source_mode": "synthetic_demo",
    }
    binding = next(
        agent for agent in runtime.configuration.agents if agent.capability == recipe["capability"]
    )
    coordinator = runtime.configuration.coordinator
    records = []

    def emit(kind, subject, generation, parent, reference, operation):
        envelope = public_event(
            task=task,
            kind=kind,
            actor=str(coordinator.id),
            binding_version=str(coordinator.binding_version),
            subject=str(subject),
            generation=generation,
            parent_key=parent,
            reference_digest=reference,
            operation_id=str(operation),
            issued_at=issued,
            private_key=runtime.private_key,
            key_id=coordinator.key_id,
        )
        receipt = (
            runtime.gateway.publish(envelope, coordinator) if run["public_reference"] else None
        )
        records.append(
            {"kind": kind, "envelope": envelope.model_dump(mode="json"), "receipt": receipt}
        )
        return {"envelope": envelope.model_dump(mode="json"), "receipt": receipt}

    advertised = emit("task", task_id, 0, "", task["recipe_digest"], uuid5(task_id, "advertise"))
    claim = exchange(
        runtime,
        binding,
        str(uuid5(task_id, "claim")),
        {"operation": "claim", "task": task, "issued_at": issued, "requester_event": advertised},
    )
    claim_envelope = SignedEnvelope.model_validate(claim["envelope"])
    verify(claim_envelope, binding.public_key, binding.key_id, str(coordinator.id))
    claim_public = SignedEnvelope.model_validate(claim["public_event"])
    verify(claim_public, binding.public_key, binding.key_id, "public")
    if run["public_reference"]:
        runtime.gateway.read(claim["receipt"]["entity_key"], binding, claim_public)
    records.append(
        {
            "kind": "claim",
            "envelope": claim_public.model_dump(mode="json"),
            "receipt": claim["receipt"],
        }
    )
    task["generation"] = 1
    attempt = uuid5(task_id, "attempt-1")
    assignment = sign(
        {
            "schema_version": "orca.assignment/1.0",
            "domain": DOMAIN,
            "chain_id": CHAIN_ID,
            "audience": str(binding.id),
            "task_id": str(task_id),
            "assignment_generation": 1,
            "binding_version": str(coordinator.binding_version),
            "provider_binding_version": str(binding.binding_version),
            "provider_id": str(binding.id),
            "attempt_id": str(attempt),
            "recipe_digest": task["recipe_digest"],
            "claim_digest": claim_envelope.content_digest,
            "deadline": task["deadline"],
        },
        runtime.private_key,
        coordinator.key_id,
    )
    assigned = emit(
        "assignment",
        binding.id,
        1,
        claim["receipt"]["entity_key"] if claim["receipt"] else "",
        assignment.content_digest,
        uuid5(task_id, "execute"),
    )
    response = exchange(
        runtime,
        binding,
        str(uuid5(task_id, "execute")),
        {
            "operation": "execute",
            "task": task,
            "issued_at": issued,
            "requester_event": assigned,
            "assignment": assignment.model_dump(mode="json"),
            "claim": claim_envelope.model_dump(mode="json"),
            "recipe": recipe,
        },
    )
    artifact = SignedEnvelope.model_validate(response["envelope"])
    verify(artifact, binding.public_key, binding.key_id, str(coordinator.id))
    payload = artifact.payload
    if (
        payload["task_id"] != str(task_id)
        or payload["recipe_digest"] != task["recipe_digest"]
        or payload["attempt_id"] != str(attempt)
        or payload["binding_version"] != str(binding.binding_version)
        or payload["results"]["source_mode"] != "synthetic_demo"
    ):
        raise ValueError("demo artifact context differs from assigned synthetic task")
    validate_comparison(payload["results"], recipe["expected"])
    public = SignedEnvelope.model_validate(response["public_event"])
    verify(public, binding.public_key, binding.key_id, "public")
    if (
        public.payload["source_mode"] != "synthetic_demo"
        or public.payload["reference_digest"] != artifact.content_digest
    ):
        raise ValueError("demo public record lost its synthetic label or evidence binding")
    if run["public_reference"]:
        runtime.gateway.read(response["receipt"]["entity_key"], binding, public)
    records.append(
        {
            "kind": public.payload["kind"],
            "envelope": public.model_dump(mode="json"),
            "receipt": response["receipt"],
        }
    )
    result = payload["results"]
    names = (
        "Biased-input candidate",
        "Clean-input reviewer challenge",
        "Corrected-input rerun",
        "Re-review of corrected result",
    )
    return {
        "sequence": phase + 1,
        "title": names[phase],
        "source_mode": "synthetic_demo",
        "agent_id": str(binding.id),
        "agent_name": binding.name,
        "task_id": str(task_id),
        "artifact": artifact.model_dump(mode="json"),
        "anomaly_celsius": result["anomaly"],
        "compared_with_celsius": recipe["expected"]["anomaly"],
        "difference_celsius": result["maximum_absolute_difference"],
        "verdict": result["verdict"],
        "recipe_digest": task["recipe_digest"],
        "input_variant": recipe["input_variant"],
        "corrects_digest": recipe["corrects_digest"],
        "review_of_digest": recipe["review_of_digest"],
        "a2a_task_id": response["a2a_task_id"],
        "chain_records": records,
    }


def process_one(settings, root):
    store = DemoStore(root)
    token = str(uuid4())
    with store.connect() as local:
        local.execute("BEGIN IMMEDIATE")
        row = local.execute(
            "SELECT * FROM runs WHERE state IN ('queued','candidate_running','reviewing','correcting','reviewing_correction') AND (lease_until IS NULL OR lease_until<?) ORDER BY created_at LIMIT 1",
            (time.time(),),
        ).fetchone()
        if row is None:
            return False
        run = dict(row)
        local.execute(
            "UPDATE runs SET lease_token=?,lease_until=? WHERE id=?",
            (token, time.time() + 600, run["id"]),
        )
    db = create_runtime_engine(settings.database_url)
    try:
        principal = Principal(
            run["subject"], "demo-worker", frozenset().union(*ROLE_SCOPES.values())
        )

        def permitted():
            with TenantUnitOfWork(db, UUID(run["tenant"])) as session:
                authorize_project(
                    session, UUID(run["tenant"]), UUID(run["project"]), principal, "project:write"
                )

        permitted()
        configured = NetworkConfiguration.model_validate(
            strict_json((root / "bindings.json").read_bytes())
        )
        runtime = NetworkRuntime(
            configured,
            settings.network_private_key.get_secret_value(),
            load_runtime(settings).gateway,
        )
        inputs = strict_json((root / "inputs.json").read_bytes())
        previous = store.read(run["id"], run["tenant"], run["project"])["stages"]
        phase = run["phase"]
        recipe = recipe_for(inputs, phase, previous)
        stage = delegate(runtime, run, phase, recipe)
        permitted()
        next_state = ("reviewing", "challenged", "reviewing_correction", "resolved")[phase]
        if phase == 1 and stage["verdict"] != "challenged":
            raise ValueError(
                "the intentionally biased candidate did not produce a numerical challenge"
            )
        if phase == 3 and stage["verdict"] != "reproduced":
            raise ValueError("corrected input did not resolve the numerical disagreement")
        with store.connect() as local:
            local.execute("BEGIN IMMEDIATE")
            owned = local.execute(
                "SELECT lease_token FROM runs WHERE id=?", (run["id"],)
            ).fetchone()
            if owned[0] != token:
                return False
            local.execute(
                "INSERT INTO stages VALUES(?,?,?)",
                (run["id"], phase + 1, json.dumps(stage, allow_nan=False)),
            )
            local.execute(
                "UPDATE runs SET state=?,phase=phase+1,version=version+1,lease_token=NULL,lease_until=NULL,error_code=NULL WHERE id=?",
                (next_state, run["id"]),
            )
    except Exception as error:
        from orca.infrastructure.federation.runtime import ChainUnavailable

        state = (
            "reconciliation_required"
            if isinstance(error, ChainUnavailable) and error.reconciliation
            else "failed"
        )
        with store.connect() as local:
            local.execute(
                "UPDATE runs SET state=?,error_code=?,lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?",
                (state, "DEMO_DEPENDENCY_OR_AUTHORIZATION_FAILURE", run["id"], token),
            )
    finally:
        db.dispose()
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)
    while True:
        process_one(settings, args.state)
        time.sleep(1)


if __name__ == "__main__":
    main()

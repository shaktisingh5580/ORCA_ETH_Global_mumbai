"""Purpose: publish actual signed research evidence receipts through the existing Arkiv adapter.
Behavior: persist the signed envelope before sending; fence workers and surface uncertain sends.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A04; Phase: A04 evidence publication increment.
"""

import argparse
import json
import time
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import Engine, text

from orca.application.anchors import ResearchAnchors
from orca.config import Settings
from orca.contracts.network import SignedEnvelope
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.public_events import public_event
from orca.infrastructure.federation.runtime import ChainUnavailable, NetworkRuntime, load_runtime


def execute(db: Engine, runtime: NetworkRuntime, tenant: UUID, identifier: UUID) -> bool:
    token = uuid4()
    binding = runtime.configuration.coordinator
    with TenantUnitOfWork(db, tenant) as session:
        row = (
            session.execute(
                text("""SELECT * FROM research.evidence_anchors WHERE tenant_id=:t AND id=:id
          AND (state='queued' OR (state='publishing' AND lease_until<clock_timestamp())) FOR UPDATE SKIP LOCKED"""),
                {"t": tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        job = dict(row)
        principal = Principal(
            job["created_by"], "trusted-anchor-worker", frozenset().union(*ROLE_SCOPES.values())
        )
        ResearchAnchors(session, tenant, job["project_id"], principal).authorize("project:write")
        envelope = (
            SignedEnvelope.model_validate(job["envelope"])
            if job["envelope"]
            else public_event(
                task={
                    "id": job["run_id"],
                    "recipe_digest": job["manifest"]["plan_digest"],
                    "deadline": (job["created_at"] + timedelta(days=7)).isoformat(),
                    "capability": "research_evidence",
                    "region": "user_supplied",
                    "source_mode": job["manifest"]["source_mode"],
                },
                kind="evidence",
                actor=str(binding.id),
                binding_version=str(binding.binding_version),
                subject=str(identifier),
                generation=0,
                parent_key=job["parent_key"],
                reference_digest=job["manifest_digest"],
                operation_id=str(identifier),
                issued_at=job["created_at"].isoformat(),
                private_key=runtime.private_key,
                key_id=binding.key_id,
            )
        )
        session.execute(
            text("""UPDATE research.evidence_anchors SET state='publishing',envelope=CAST(:envelope AS jsonb),
          lease_token=:token,lease_until=clock_timestamp()+interval '240 seconds' WHERE tenant_id=:t AND id=:id"""),
            {"t": tenant, "id": identifier, "token": token, "envelope": envelope.model_dump_json()},
        )
    try:
        receipt = runtime.gateway.publish(envelope, binding)
        # The gateway verifies current chain owner and signature before returning.
        with TenantUnitOfWork(db, tenant) as session:
            updated = session.execute(
                text("""UPDATE research.evidence_anchors SET state='indexed',receipt=CAST(:receipt AS jsonb)
              WHERE tenant_id=:t AND id=:id AND state='publishing' AND lease_token=:token RETURNING id"""),
                {"t": tenant, "id": identifier, "token": token, "receipt": json.dumps(receipt)},
            ).scalar_one_or_none()
            if updated:
                ResearchAnchors(session, tenant, job["project_id"], principal).event(
                    job["run_id"],
                    "arkiv_evidence_indexed",
                    {
                        "anchor_id": str(identifier),
                        "entity_key": receipt["entity_key"],
                        "tx_hash": receipt.get("tx_hash"),
                        "manifest_digest": job["manifest_digest"],
                        "publication_kind": "coordinator_attestation_not_a2a_execution",
                    },
                )
    except Exception as error:
        uncertain = isinstance(error, ChainUnavailable) and error.reconciliation
        with TenantUnitOfWork(db, tenant) as session:
            session.execute(
                text("""UPDATE research.evidence_anchors SET state=:state,error_code=:code
              WHERE tenant_id=:t AND id=:id AND state='publishing' AND lease_token=:token"""),
                {
                    "t": tenant,
                    "id": identifier,
                    "token": token,
                    "state": "reconciliation_required" if uncertain else "failed",
                    "code": "ARKIV_RECONCILIATION_REQUIRED"
                    if uncertain
                    else "ARKIV_PUBLICATION_FAILED",
                },
            )
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url:
        raise ValueError("Database required")
    db, runtime = create_runtime_engine(settings.database_url), load_runtime(settings)
    while True:
        with db.connect() as connection:
            jobs = connection.execute(text("SELECT * FROM iam.pending_evidence_anchors()")).all()
        for tenant, identifier in jobs:
            execute(db, runtime, UUID(str(tenant)), UUID(str(identifier)))
        if args.once:
            break
        time.sleep(3)
    db.dispose()


if __name__ == "__main__":
    main()

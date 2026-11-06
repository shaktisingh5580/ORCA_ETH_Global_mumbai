"""Purpose: reconcile uncertain local coordinator/provider sends without blind transaction replay.
Behavior: require live owner/digest/signature proof before unlocking the matching writer/resuming intent.
Refs: canonical durable outbox/recovery rules; H04-agent-coordination.json; Phase: H04.
"""

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid5

from dotenv import dotenv_values
from sqlalchemy import text

from orca.api.routers.projects import resolve_project
from orca.application.network import Network
from orca.config import Settings
from orca.contracts.network import SignedEnvelope
from orca.domain.identity import ROLE_SCOPES, Principal
from orca.infrastructure.db.engine import create_runtime_engine
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.runtime import (
    ROOT,
    ArkivGateway,
    load_runtime,
    verify_chain_record,
)
from orca.infrastructure.federation.signing import strict_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env.demo")
    parser.add_argument("--operation-id", type=UUID, required=True)
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file)  # type: ignore[call-arg]
    if not settings.database_url:
        raise ValueError("runtime database required")
    runtime = load_runtime(settings)
    db = create_runtime_engine(settings.database_url)
    principal = Principal(
        args.subject, "operator-reconciliation", frozenset().union(*ROLE_SCOPES.values())
    )
    try:
        tenant, _ = resolve_project(db, principal, args.project_id)
        with TenantUnitOfWork(db, tenant) as session:
            service = Network(session, tenant, args.project_id, principal)
            service.authorize("project:write")
            operation = dict(
                session.execute(
                    text("SELECT * FROM federation.operations WHERE tenant_id=:t AND id=:id"),
                    {"t": tenant, "id": args.operation_id},
                )
                .mappings()
                .one()
            )
            service.task_row(operation["task_id"])
            if operation["created_by"] != args.subject:
                raise PermissionError("only original requester can resume this intent")
        locations = [(runtime.configuration.coordinator, runtime.gateway)]
        for binding in runtime.configuration.agents:
            role = "ocean" if binding.capability == "sst_reproduction" else "reviewer"
            values = dotenv_values(ROOT / f".env.provider-{role}")
            locations.append(
                (
                    binding,
                    ArkivGateway(
                        Path(str(values["ORCA_PROVIDER_WALLET_ENV_FILE"])),
                        Path(str(values["ORCA_PROVIDER_STATE_DIR"])),
                        binding.wallet,
                    ),
                )
            )
        verified = []
        for binding, gateway in locations:
            for kind in ("task", "claim", "assignment", "result", "review", "cancellation"):
                event_id = str(uuid5(args.operation_id, kind))
                receipt_file = gateway.state_dir / "receipts" / (event_id + ".json")
                if not receipt_file.exists():
                    continue
                reservation = strict_json(receipt_file.read_bytes())
                observed = gateway.call(
                    {
                        "command": "read",
                        "entity_key": reservation["entity_key"],
                        "owner": binding.wallet,
                        "content_digest": reservation["content_digest"],
                    }
                )
                envelope = SignedEnvelope.model_validate(observed["envelope"])
                verify_chain_record(observed, binding, envelope)
                if envelope.payload["event_id"] != event_id or envelope.payload["task_id"] != str(
                    operation["task_id"]
                ):
                    raise ValueError("reservation belongs to a different task/event")
                lock_file = gateway.state_dir / "writer.lock"
                if args.resume and lock_file.exists():
                    lock = strict_json(lock_file.read_bytes())
                    if (
                        lock["event_id"] == event_id
                        and Path(lock["receipt_file"]).resolve() == receipt_file.resolve()
                    ):
                        lock_file.unlink()
                verified.append(
                    {
                        "event_id": event_id,
                        "kind": kind,
                        "owner": binding.wallet,
                        "entity_key": reservation["entity_key"],
                    }
                )
        if operation["status"] == "reconciliation_required" and not verified:
            raise ValueError(
                "no live-confirmed reservation found; retain locks and inspect pending nonce/transaction before any resend"
            )
        if args.resume:
            with TenantUnitOfWork(db, tenant) as session:
                service = Network(session, tenant, args.project_id, principal)
                service.authorize("project:write")
                task = service.task_row(operation["task_id"], lock=True)
                if task["state"] in {"cancelled", "conflicted"}:
                    raise ValueError("cancelled/conflicted task cannot resume")
                if operation["attempts"] >= 3:
                    raise ValueError(
                        "attempt budget exhausted; retain history and create an explicit new task"
                    )
                session.execute(
                    text(
                        "UPDATE federation.operations SET status='queued',error_code=NULL,worker_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id AND status IN ('failed','reconciliation_required')"
                    ),
                    {"t": tenant, "id": args.operation_id},
                )
        print(
            json.dumps(
                {
                    "verified_reservations": verified,
                    "resume_requested": args.resume,
                    "new_chain_write_sent_by_reconciler": False,
                }
            )
        )
    finally:
        db.dispose()


if __name__ == "__main__":
    main()

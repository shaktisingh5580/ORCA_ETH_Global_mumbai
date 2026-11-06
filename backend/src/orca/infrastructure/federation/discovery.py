"""Purpose: discover open signed public tasks by scientific capability and source profile.
Behavior: query Arkiv, pin requester identity and exclude assigned/cancelled tasks.
Refs: H05 discovery gap; public coordination schema v2; Phase: H05.
"""

from datetime import datetime, timezone
from typing import Any

from orca.contracts.network import AgentBinding, SignedEnvelope
from orca.infrastructure.federation.runtime import ArkivGateway, verify_chain_record


def discover_open(
    gateway: ArkivGateway,
    coordinator: AgentBinding,
    capability: str,
    *,
    source_mode: str = "real",
    region: str = "north_arabian_sea",
) -> dict[str, Any]:
    found = gateway.call(
        {
            "command": "discover",
            "capability": capability,
            "source_mode": source_mode,
            "region": region,
            "owner": coordinator.wallet,
        }
    )
    accepted = []
    for record in found["records"][:30]:
        envelope = SignedEnvelope.model_validate(record["envelope"])
        verify_chain_record(record, coordinator, envelope)
        payload = envelope.payload
        if (
            payload.get("kind") != "task"
            or payload.get("region") != region
            or payload.get("source_mode") != source_mode
            or payload.get("capability") != capability
            or datetime.fromisoformat(payload["deadline"]) <= datetime.now(timezone.utc)
        ):
            continue
        history = gateway.call({"command": "query", "task_id": payload["task_id"]})
        if history["truncated"]:
            continue
        closed = False
        for event in history["records"]:
            if event["owner"].lower() != coordinator.wallet.lower():
                continue
            signed = SignedEnvelope.model_validate(event["envelope"])
            try:
                verify_chain_record(event, coordinator, signed)
            except ValueError:
                continue
            if signed.payload.get("task_id") == payload["task_id"] and signed.payload["kind"] in {
                "assignment",
                "cancellation",
            }:
                closed = True
        if not closed:
            accepted.append(record)
    return {
        "records": accepted,
        "truncated": found["truncated"] or len(found["records"]) > 30,
        "observed_at": found["observed_at"],
        "source_mode": source_mode,
        "requires_explicit_requester_assignment": True,
    }

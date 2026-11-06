"""Purpose: construct minimal consented chain records with context-bound service signatures.
Behavior: publish only bounded ASCII metadata and digests, separate from private A2A artifacts.
Refs: Arkiv blueprint 7; canonical signing guide; H04-agent-coordination.json; Phase: H04.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid5

from orca.contracts.network import SignedEnvelope
from orca.infrastructure.federation.signing import CHAIN_ID, DOMAIN, sign


def public_event(
    *,
    task: dict[str, Any],
    kind: str,
    actor: str,
    binding_version: str,
    subject: str,
    generation: int,
    parent_key: str,
    reference_digest: str,
    operation_id: str,
    issued_at: str,
    private_key: str,
    key_id: str,
) -> SignedEnvelope:
    payload = {
        "schema_version": "orca.public-coordination/1.0",
        "domain": DOMAIN,
        "audience": "public",
        "chain_id": CHAIN_ID,
        "kind": kind,
        "event_id": str(uuid5(UUID(operation_id), kind)),
        "task_id": str(task["id"]),
        "assignment_generation": str(generation),
        "actor_id": actor,
        "binding_version": binding_version,
        "subject_id": subject,
        "parent_key": parent_key,
        "recipe_digest": task["recipe_digest"],
        "reference_digest": reference_digest,
        "issued_at": issued_at,
        "deadline": datetime.fromisoformat(str(task["deadline"]))
        .astimezone(timezone.utc)
        .isoformat(),
    }
    if any(not isinstance(value, str) or len(value.encode()) > 128 for value in payload.values()):
        raise ValueError("public coordination fields exceed chain profile")
    return sign(payload, private_key, key_id)

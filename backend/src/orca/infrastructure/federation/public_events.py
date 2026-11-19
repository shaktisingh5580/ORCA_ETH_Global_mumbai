"""Purpose: construct minimal consented chain records with context-bound service signatures.
Behavior: publish only bounded ASCII metadata and digests, separate from private A2A artifacts.
Refs: api design.md §4.7; main_implementation plan.md P11.2; H04-agent-coordination.json. Phase: H04/P11.
"""

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid5

from orca.contracts.network import AgentBinding, SignedEnvelope
from orca.infrastructure.federation.signing import CHAIN_ID, DOMAIN, digest, sign


def public_task_intent(task: dict[str, Any], requester: AgentBinding) -> dict[str, Any]:
    return {
        "kind": "task",
        "owner": requester.wallet,
        "lifetimeSeconds": 86400,
        "attributes": {
            "taskId": str(task["id"]),
            "capability": task["capability"],
            "regionCode": "global",
            "status": "open",
            "deadlineRef": task["deadline"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }


def public_claim_attributes(task: UUID, provider: AgentBinding) -> dict[str, str]:
    return {
        "taskId": str(task),
        "agentId": str(provider.id),
        "status": "proposed",
        "bindingDigest": digest(provider.model_dump(mode="json")),
    }


def public_assignment_intent(
    task: UUID,
    requester: AgentBinding,
    provider: AgentBinding,
    task_key: str,
    claim_key: str,
    assignment_digest: str,
) -> dict[str, Any]:
    return {
        "kind": "assignment",
        "owner": requester.wallet,
        "lifetimeSeconds": 86400,
        "parent": {"owner": requester.wallet, "key": task_key},
        "selectedClaim": {
            "owner": provider.wallet,
            "attributes": public_claim_attributes(task, provider),
        },
        "attributes": {
            "taskId": str(task),
            "claimRef": claim_key.lower(),
            "status": "selected",
            "assignmentDigest": assignment_digest,
        },
    }


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
        "schema_version": "orca.public-coordination/2.0",
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
        "capability": task.get("capability", "sst_reproduction"),
        "region": task.get("region", "north_arabian_sea"),
        "source_mode": task.get("source_mode", "real"),
    }
    if any(not isinstance(value, str) or len(value.encode()) > 128 for value in payload.values()):
        raise ValueError("public coordination fields exceed chain profile")
    return sign(payload, private_key, key_id)

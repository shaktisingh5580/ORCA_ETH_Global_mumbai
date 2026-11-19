"""Purpose: let a provider discover and claim public work without ORCA database access.
Behavior: pin requester signatures and publish provider-owned claims using its own signer.
Refs: H05 direct Arkiv discovery; main_implementation plan.md P11.2; api design.md §4.7. Phase: H05/P11.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid5

from orca.agent_provider import DurableTasks, ProviderSettings, ScientificAgent
from orca.contracts.network import AgentBinding
from orca.infrastructure.federation.discovery import discover_open
from orca.infrastructure.federation.public_events import public_claim_attributes, public_task_intent
from orca.infrastructure.federation.runtime import NetworkConfiguration
from orca.infrastructure.federation.signing import digest, strict_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--claim-task", type=UUID)
    parser.add_argument("--arkiv-v1-task-key")
    args = parser.parse_args()
    if args.arkiv_v1_task_key and not args.claim_task:
        parser.error("--arkiv-v1-task-key requires --claim-task")
    settings = ProviderSettings(_env_file=args.env_file)  # type: ignore[call-arg]
    binding = AgentBinding.model_validate(strict_json(Path(settings.binding_file).read_bytes()))
    config = NetworkConfiguration.model_validate(
        strict_json(Path(settings.coordinator_file).read_bytes())
    )
    config.validate_endpoints()
    if binding not in config.agents:
        raise ValueError("provider identity is not in the pinned configuration")
    agent = ScientificAgent(
        settings, binding, config.coordinator, DurableTasks(Path(settings.state_dir))
    )
    board = discover_open(
        agent.gateway, config.coordinator, binding.capability, source_mode=settings.source_mode
    )
    if args.claim_task:
        chosen = next(
            (
                record
                for record in board["records"]
                if record["envelope"]["payload"]["task_id"] == str(args.claim_task)
            ),
            None,
        )
        if chosen is None:
            raise ValueError("selected task is not currently open on the live filtered board")
        payload = chosen["envelope"]["payload"]
        arkiv_v1_intent = None
        if args.arkiv_v1_task_key:
            task_reference = agent.gateway.public_v1_read(
                "task", args.arkiv_v1_task_key, config.coordinator.wallet
            )
            expected_task = public_task_intent(
                {
                    "id": args.claim_task,
                    "capability": payload["capability"],
                    "deadline": datetime.fromisoformat(payload["deadline"]),
                },
                config.coordinator,
            )
            if task_reference["attributes"] != expected_task["attributes"]:
                raise ValueError("Arkiv v1 task differs from the signed requester record")
            arkiv_v1_intent = {
                "kind": "claim",
                "owner": binding.wallet,
                "lifetimeSeconds": 86400,
                "parent": {"owner": config.coordinator.wallet, "key": args.arkiv_v1_task_key},
                "attributes": public_claim_attributes(args.claim_task, binding),
            }
        operation = str(uuid5(binding.binding_version, "direct-claim-" + payload["task_id"]))
        data = {
            "operation": "claim",
            "task": {
                "id": payload["task_id"],
                "capability": payload["capability"],
                "recipe_digest": payload["recipe_digest"],
                "deadline": payload["deadline"],
                "generation": 0,
                "publication_mode": "public_reference",
                "source_mode": payload["source_mode"],
            },
            "issued_at": payload["issued_at"],
            "requester_event": {
                "envelope": chosen["envelope"],
                "receipt": {"entity_key": chosen["entity_key"]},
            },
        }
        result = agent.process(
            data,
            {
                "jti": operation,
                "request_digest": digest(data),
                "scope": "claim",
                "task_id": payload["task_id"],
            },
        )
        v1_claim = agent.gateway.public_v1_publish(arkiv_v1_intent) if arkiv_v1_intent else None
        print(
            json.dumps(
                {
                    "task_id": payload["task_id"],
                    "provider_id": str(binding.id),
                    "claim_entity_key": result["receipt"]["entity_key"],
                    **({"arkiv_v1_entity_key": v1_claim["entity_key"]} if v1_claim else {}),
                    "source_mode": settings.source_mode,
                    "orca_api_used": False,
                    "database_used": False,
                    "requester_selection_still_required": True,
                }
            )
        )
    else:
        print(json.dumps(board))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(
            "Provider discovery/claim unavailable (" + type(error).__name__ + ")"
        ) from None

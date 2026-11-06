"""Purpose: let a provider discover and claim public work without ORCA database access.
Behavior: pin requester signatures and publish provider-owned claims using its own signer.
Refs: H05 direct Arkiv discovery; public schema v2; Phase: H05.
"""

import argparse
import json
from pathlib import Path
from uuid import UUID, uuid5

from orca.agent_provider import DurableTasks, ProviderSettings, ScientificAgent
from orca.contracts.network import AgentBinding
from orca.infrastructure.federation.discovery import discover_open
from orca.infrastructure.federation.runtime import NetworkConfiguration
from orca.infrastructure.federation.signing import digest, strict_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--claim-task", type=UUID)
    args = parser.parse_args()
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
        print(
            json.dumps(
                {
                    "task_id": payload["task_id"],
                    "provider_id": str(binding.id),
                    "claim_entity_key": result["receipt"]["entity_key"],
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

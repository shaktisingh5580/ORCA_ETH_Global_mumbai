"""Purpose: dispatch scoped signed requests through the pinned official A2A JSON-RPC client.
Behavior: use exact loopback bindings, short-lived audience tokens and bounded nonredirecting HTTP.
Refs: A2A 0.3.0; official a2a-sdk 0.3.22; H04 decision; Phase: H04.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
import jwt
from a2a.client import A2AClient
from a2a.types import (
    AgentCard,
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orca.contracts.network import AgentBinding
from orca.infrastructure.federation.runtime import ChainUnavailable, NetworkRuntime
from orca.infrastructure.federation.signing import digest, unb64


async def exchange_async(
    runtime: NetworkRuntime, agent: AgentBinding, operation_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    # Read-back timestamps are observations, not logical task input. Excluding them
    # makes retrying a confirmed public assignment preserve the provider deduplication key.
    if data.get("requester_event"):
        parent = data["requester_event"]
        data = {
            **data,
            "requester_event": {
                "envelope": parent["envelope"],
                "receipt": {"entity_key": parent["receipt"]["entity_key"]}
                if parent.get("receipt")
                else None,
            },
        }
    now = int(datetime.now(timezone.utc).timestamp())
    token = jwt.encode(
        {
            "iss": str(runtime.configuration.coordinator.id),
            "aud": str(agent.id),
            "iat": now,
            "exp": now + 300,
            "jti": operation_id,
            "task_id": data["task"]["id"],
            "scope": data["operation"],
            "request_digest": digest(data),
        },
        Ed25519PrivateKey.from_private_bytes(unb64(runtime.private_key)),
        algorithm="EdDSA",
        headers={"kid": runtime.configuration.coordinator.key_id},
    )
    async with httpx.AsyncClient(
        timeout=180,
        follow_redirects=False,
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        card_response = await http.get(
            agent.endpoint.removesuffix("/a2a") + "/.well-known/agent-card.json"
        )
        card_response.raise_for_status()
        if len(card_response.content) > 32768:
            raise ValueError("agent card oversized")
        card = AgentCard.model_validate(card_response.json())
        if (
            card.url != agent.endpoint
            or card.protocol_version != "0.3.0"
            or agent.capability not in {skill.id for skill in card.skills}
        ):
            raise ValueError("agent card differs from pinned binding")
        client = A2AClient(http, agent_card=card)
        response = await client.send_message(
            SendMessageRequest(
                id=str(uuid4()),
                params=MessageSendParams(
                    message=Message(
                        message_id=operation_id,
                        role=Role.user,
                        parts=[Part(root=DataPart(data=data))],
                    )
                ),
            )
        )
        output = response.model_dump(mode="json", by_alias=True)
        if len(str(output)) > 256_000 or "error" in output:
            raise ValueError("A2A task failed or exceeded response bound")
        task = output["result"]
        if (task.get("metadata") or {}).get("error_code") == "ARKIV_RECONCILIATION_REQUIRED":
            raise ChainUnavailable(True)
        if task.get("kind") != "task" or task.get("status", {}).get("state") != "completed":
            raise ValueError("A2A task has not completed successfully")
        artifacts = task.get("artifacts", [])
        if len(artifacts) != 1 or len(artifacts[0].get("parts", [])) != 1:
            raise ValueError("A2A task artifact contract differs")
        part = artifacts[0]["parts"][0]
        if part.get("kind") != "data":
            raise ValueError("expected a structured signed A2A artifact")
        return {"a2a_task_id": task["id"], "a2a_context_id": task["contextId"], **part["data"]}


def exchange(
    runtime: NetworkRuntime, agent: AgentBinding, operation_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    return asyncio.run(exchange_async(runtime, agent, operation_id, data))

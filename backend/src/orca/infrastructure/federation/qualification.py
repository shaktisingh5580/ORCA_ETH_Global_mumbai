"""Purpose: qualify an approved service through real official-SDK A2A requests.
Behavior: verify signed known-answer/negative results, durable task reads and operation replay.
Refs: institutional phase F02.4-F02.5; A2A 0.3.0; Phase: F02.
"""

import asyncio
import time
from typing import Any
from uuid import UUID, uuid4, uuid5

import httpx
import jwt
from a2a.client import A2AClient
from a2a.types import (
    AgentCard,
    DataPart,
    GetTaskRequest,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    TaskQueryParams,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from orca.infrastructure.federation.https_egress import PublicProviderTransport
from orca.infrastructure.federation.signing import canonical, digest, unb64


async def qualify_async(
    agent: dict[str, Any],
    coordinator: dict[str, str],
    private_key: str,
    key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    registration = agent["registration"]
    async with httpx.AsyncClient(
        transport=transport or PublicProviderTransport(),
        timeout=25,
        trust_env=False,
        follow_redirects=False,
    ) as http:
        response = await http.get(
            registration["endpoint"].removesuffix("/a2a") + "/.well-known/agent-card.json"
        )
        response.raise_for_status()
        raw = response.json()
        if digest(raw) != registration["card_digest"]:
            raise ValueError("Agent Card changed since proof")
        parsed = AgentCard.model_validate(raw)
        if parsed.url != registration["endpoint"] or parsed.protocol_version != "0.3.0":
            raise ValueError("Card changed")
        client = A2AClient(http, agent_card=parsed)
        evidence: list[dict[str, Any]] = []
        operation_namespace = uuid5(UUID(str(agent["binding_version"])), key)
        for label, values, unit, expected in [
            ("celsius", [26, 28], "C", 27.0),
            ("kelvin", [299.15, 301.15], "K", 27.0),
            ("invalid", [], "C", None),
        ]:
            op = str(uuid5(operation_namespace, label))
            data = {
                "schema_version": "orca.provider-qualification/1.0",
                "operation_id": op,
                "values": values,
                "unit": unit,
            }
            now = int(time.time())
            token = jwt.encode(
                {
                    "iss": coordinator["id"],
                    "aud": str(agent["id"]),
                    "iat": now,
                    "exp": now + 120,
                    "jti": op,
                    "scope": "agent:qualify",
                    "request_digest": digest(data),
                },
                Ed25519PrivateKey.from_private_bytes(unb64(private_key)),
                algorithm="EdDSA",
                headers={"kid": coordinator["key_id"]},
            )
            http.headers["Authorization"] = "Bearer " + token

            async def send() -> dict[str, Any]:
                reply = await client.send_message(
                    SendMessageRequest(
                        id=str(uuid4()),
                        params=MessageSendParams(
                            message=Message(
                                message_id=op,
                                role=Role.user,
                                parts=[Part(root=DataPart(data=data))],
                            )
                        ),
                    )
                )
                result: dict[str, Any] = reply.model_dump(mode="json", by_alias=True)
                return dict(result["result"])

            task = await send()
            if expected is None:
                if task["status"]["state"] != "failed":
                    raise ValueError("Invalid inputs were accepted")
                evidence.append({"case": label, "state": "rejected"})
                continue
            if task["status"]["state"] != "completed":
                raise ValueError("Provider did not complete qualification")
            envelope = task["artifacts"][0]["parts"][0]["data"]
            if envelope["key_id"] != registration["key_id"]:
                raise ValueError("Service key differs")
            Ed25519PublicKey.from_public_bytes(unb64(registration["public_key"])).verify(
                unb64(envelope["signature"]), canonical(envelope["payload"])
            )
            output = envelope["payload"]
            if (
                output.get("input_digest") != digest(data)
                or output.get("method") != "arithmetic_mean/1.0"
                or output.get("count") != 2
                or abs(output["mean_celsius"] - expected) > 1e-9
            ):
                raise ValueError("Scientific qualification answer differs")
            stored = (
                await client.get_task(
                    GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=task["id"]))
                )
            ).model_dump(mode="json", by_alias=True)
            if stored["result"]["artifacts"] != task["artifacts"]:
                raise ValueError("Stored task differs")
            replay = await send()
            if replay.get("artifacts") != task["artifacts"]:
                raise ValueError("Operation replay changed result")
            evidence.append(
                {
                    "case": label,
                    "a2a_task_id": task["id"],
                    "artifact_digest": digest(envelope),
                    "mean_celsius": output["mean_celsius"],
                    "status_read": True,
                    "replay_equal": True,
                }
            )
        return {
            "status": "PASS",
            "capability": "temperature_summary_v1",
            "method": "arithmetic_mean/1.0",
            "scope": "service conformance; not institutional identity or research delegation",
            "a2a_protocol": "0.3.0",
            "checks": evidence,
        }


def qualify(
    agent: dict[str, Any], coordinator: dict[str, str], private_key: str, key: str
) -> dict[str, Any]:
    return asyncio.run(qualify_async(agent, coordinator, private_key, key))

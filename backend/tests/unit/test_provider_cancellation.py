"""Purpose: prove institutional provider cancellation survives restart and blocks late execution.
Behavior: use real official-SDK task cancellation against a local ASGI service; no external network.
Refs: institutional phase F02.4 recovery; Phase: F02.
"""

import asyncio
import time
from uuid import uuid4

import httpx
import jwt
import pytest
from a2a.client import A2AClient
from a2a.server.context import ServerCallContext
from a2a.types import (
    CancelTaskRequest,
    DataPart,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    Task,
    TaskIdParams,
    TaskState,
    TaskStatus,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orca.agent_provider import DurableTasks
from orca.infrastructure.federation.signing import b64, digest
from orca.institution_provider import card, create_provider


@pytest.mark.enable_socket
def test_provider_cancel_persists_and_rejects_late_execution(tmp_path):
    async def run():
        private = Ed25519PrivateKey.generate()
        public = b64(
            private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        )
        coordinator = {"id": str(uuid4()), "public_key": public, "key_id": "test-coordinator"}
        config = {
            "name": "Cancellation test",
            "endpoint": "https://operator.example/a2a",
            "key_id": "test-provider",
            "public_key": public,
            "private_key": b64(
                private.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ),
            "coordinator": coordinator,
            "agent_id": str(uuid4()),
        }
        operation = str(uuid4())
        task = str(uuid4())
        context = str(uuid4())
        data = {
            "schema_version": "orca.provider-qualification/1.0",
            "operation_id": operation,
            "values": [26, 28],
            "unit": "C",
        }
        now = int(time.time())
        claims = {
            "iss": coordinator["id"],
            "aud": config["agent_id"],
            "iat": now,
            "exp": now + 120,
            "jti": operation,
            "scope": "agent:qualify",
            "request_digest": digest(data),
        }
        token = jwt.encode(
            claims, private, algorithm="EdDSA", headers={"kid": coordinator["key_id"]}
        )
        store = DurableTasks(tmp_path)
        await store.save(
            Task(id=task, context_id=context, status=TaskStatus(state=TaskState.working)),
            ServerCallContext(state={"authorization": claims}),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_provider(config, tmp_path)),
            base_url="https://operator.example",
            headers={"Authorization": "Bearer " + token},
        ) as http:
            client = A2AClient(http, agent_card=card(config))
            response = await client.cancel_task(
                CancelTaskRequest(id=str(uuid4()), params=TaskIdParams(id=task))
            )
            assert (
                response.model_dump(mode="json", by_alias=True)["result"]["status"]["state"]
                == "canceled"
            )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_provider(config, tmp_path)),
            base_url="https://operator.example",
            headers={"Authorization": "Bearer " + token},
        ) as http:
            client = A2AClient(http, agent_card=card(config))
            late = await client.send_message(
                SendMessageRequest(
                    id=str(uuid4()),
                    params=MessageSendParams(
                        message=Message(
                            message_id=operation,
                            role=Role.user,
                            parts=[Part(root=DataPart(data=data))],
                        )
                    ),
                )
            )
            assert (
                late.model_dump(mode="json", by_alias=True)["result"]["status"]["state"] == "failed"
            )
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0

    asyncio.run(run())

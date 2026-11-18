"""Purpose: verify the P09 research client uses official A2A wire messages.
Behavior: exchange one scoped task through an in-process transport and reject stale provider bindings.
Refs: main_implementation plan.md P09.2-P09.3; api design.md §17.1; Phase: P09.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import jwt
import pytest
from a2a.types import Artifact, DataPart, Part, Task, TaskState, TaskStatus
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orca.application.federation import ResearchDispatchScope, ResearchGrant, sign_grant
from orca.infrastructure.federation.research_a2a_client import dispatch_research_task
from orca.infrastructure.federation.signing import b64, canonical, digest
from orca.institution_provider import card


@pytest.mark.enable_socket
def test_scoped_research_task_uses_official_a2a_wire() -> None:
    async def run() -> None:
        now = datetime.now(timezone.utc)
        coordinator_key, provider_key = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()

        def private(key: Ed25519PrivateKey) -> str:
            return b64(
                key.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            )

        def public(key: Ed25519PrivateKey) -> str:
            return b64(
                key.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            )

        grant = ResearchGrant(
            audience_agent_id=uuid4(),
            binding_version=uuid4(),
            organization_id=uuid4(),
            project_id=uuid4(),
            run_id=uuid4(),
            task_id=uuid4(),
            capability_id="temperature_summary_v1",
            approved_plan_digest="sha256:" + "a" * 64,
            method_id="arithmetic_mean/1.0",
            dataset_ids=[uuid4()],
            max_cost_usd=Decimal("1.00"),
            expires_at=now + timedelta(minutes=2),
        )
        signed = sign_grant(grant, private(coordinator_key), "coordinator-v1")
        scope = ResearchDispatchScope(
            agent_id=grant.audience_agent_id,
            binding_version=grant.binding_version,
            organization_id=grant.organization_id,
            project_id=grant.project_id,
            run_id=grant.run_id,
            task_id=grant.task_id,
            capability_id=grant.capability_id,
            approved_plan_digest=grant.approved_plan_digest,
            approved_method_id=grant.method_id,
            permitted_dataset_ids=set(grant.dataset_ids),
            available_budget_usd=Decimal("1.00"),
            service_eligible=True,
            research_dispatch_enabled=True,
        )
        endpoint = "https://provider.example/a2a"
        agent_card = card({"name": "Wire test", "endpoint": endpoint})
        card_data = agent_card.model_dump(mode="json", by_alias=True, exclude_none=True)
        agent = {
            "id": grant.audience_agent_id,
            "binding_version": grant.binding_version,
            "organization_id": grant.organization_id,
            "state": "active",
            "qualification_expires_at": now + timedelta(hours=1),
            "research_dispatch_enabled": True,
            "registration": {
                "endpoint": endpoint,
                "capability": grant.capability_id,
                "card_digest": digest(card_data),
                "key_id": "provider-v1",
                "public_key": public(provider_key),
            },
        }
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.method == "GET":
                return httpx.Response(200, json=card_data)
            body = json.loads(request.content)
            assert request.url.path == "/a2a"
            assert body["method"] in {"message/send", "tasks/get"}
            data = (
                body["params"]["message"]["parts"][0]["data"]
                if body["method"] == "message/send"
                else {
                    "schema_version": "orca.scientific_task/1.0",
                    "grant": signed.model_dump(mode="json"),
                }
            )
            claims = jwt.decode(
                request.headers["Authorization"].removeprefix("Bearer "),
                coordinator_key.public_key(),
                algorithms=["EdDSA"],
                audience=str(grant.audience_agent_id),
            )
            assert claims["request_digest"] == digest(data)
            assert data["grant"]["grant_digest"] == signed.grant_digest
            payload = {
                "schema_version": "orca.scientific_artifact/1.0",
                "grant_digest": signed.grant_digest,
                "task_id": str(grant.task_id),
                "run_id": str(grant.run_id),
                "approved_plan_digest": grant.approved_plan_digest,
                "method_id": grant.method_id,
                "dataset_ids": [str(item) for item in grant.dataset_ids],
                "a2a_task_id": "remote-task",
                "a2a_context_id": "remote-context",
                "result": {"status": "insufficient_evidence"},
            }
            envelope = {
                "payload": payload,
                "key_id": "provider-v1",
                "signature": b64(provider_key.sign(canonical(payload))),
            }
            task = Task(
                id="remote-task",
                context_id="remote-context",
                status=TaskStatus(state=TaskState.completed),
                artifacts=[
                    Artifact(
                        artifact_id=digest(payload), parts=[Part(root=DataPart(data=envelope))]
                    )
                ],
            )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": task.model_dump(mode="json", by_alias=True, exclude_none=True),
                },
            )

        result = await dispatch_research_task(
            signed,
            agent,
            scope,
            uuid4(),
            public(coordinator_key),
            private(coordinator_key),
            "coordinator-v1",
            transport=httpx.MockTransport(handler),
        )
        assert result["a2a_task_id"] == "remote-task"
        assert calls == ["/.well-known/agent-card.json", "/a2a", "/a2a"]
        agent["binding_version"] = uuid4()
        with pytest.raises(PermissionError):
            await dispatch_research_task(
                signed,
                agent,
                scope,
                uuid4(),
                public(coordinator_key),
                private(coordinator_key),
                "coordinator-v1",
                transport=httpx.MockTransport(handler),
            )

    asyncio.run(run())

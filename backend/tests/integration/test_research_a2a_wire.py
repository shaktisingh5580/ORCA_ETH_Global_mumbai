"""Purpose: verify the P09 research client uses official A2A wire messages.
Behavior: exchange one scoped task through an in-process transport and reject stale provider bindings.
Refs: main_implementation plan.md P09.2-P09.3; api design.md §17.1; Phase: P09.
"""

import asyncio
import json
import subprocess
import sys
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
from orca.infrastructure.federation.coordinator import FederationCoordinator
from orca.infrastructure.federation.research_a2a_client import (
    cancel_research_task,
    dispatch_research_task,
    read_research_result,
)
from orca.infrastructure.federation.signing import b64, canonical, digest
from orca.institution_provider import card, create_provider
from orca.workers.verify_remote_summary import verify_summary


@pytest.mark.enable_socket
def test_scoped_research_task_uses_official_a2a_wire(tmp_path) -> None:
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
        methods: list[str] = []
        checkpoint: list[tuple[str, str]] = []
        remote_state = TaskState.completed
        remote_id = "remote-task"

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal remote_state
            calls.append(request.url.path)
            if request.method == "GET":
                return httpx.Response(200, json=card_data)
            body = json.loads(request.content)
            assert request.url.path == "/a2a"
            methods.append(body["method"])
            assert body["method"] in {"message/send", "tasks/get", "tasks/cancel"}
            if body["method"] == "tasks/cancel":
                assert body["params"]["id"] == "remote-task"
                remote_state = TaskState.canceled
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
            if claims["scope"] == "research:readback":
                assert body["method"] == "tasks/get"
                assert claims["remote_task_id"] == body["params"]["id"]
                assert claims["grant_digest"] == signed.grant_digest
                assert claims["request_digest"] == digest(
                    {
                        "method": "tasks/get",
                        "id": remote_id,
                        "grant_digest": signed.grant_digest,
                    }
                )
            else:
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
                "a2a_task_id": remote_id,
                "a2a_context_id": "remote-context",
                "result": {"status": "insufficient_evidence"},
            }
            envelope = {
                "payload": payload,
                "key_id": "provider-v1",
                "signature": b64(provider_key.sign(canonical(payload))),
            }
            task = Task(
                id=remote_id,
                context_id="remote-context",
                status=TaskStatus(state=remote_state),
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

        coordinator = FederationCoordinator(
            uuid4(), "coordinator-v1", public(coordinator_key), private(coordinator_key)
        )
        result = await dispatch_research_task(
            signed,
            agent,
            scope,
            coordinator.id,
            public(coordinator_key),
            private(coordinator_key),
            "coordinator-v1",
            transport=httpx.MockTransport(handler),
            checkpoint=lambda task, context: checkpoint.append((task, context)),
        )
        assert result["a2a_task_id"] == "remote-task"
        assert calls == ["/.well-known/agent-card.json", "/a2a", "/a2a"]
        assert checkpoint == [("remote-task", "remote-context")]
        remote_state = TaskState.working
        assert await cancel_research_task(
            signed,
            agent["registration"],
            coordinator,
            checkpoint[0],
            transport=httpx.MockTransport(handler),
        )
        assert methods[-3:] == ["tasks/get", "tasks/cancel", "tasks/get"]
        with pytest.raises(ValueError, match="reference changed"):
            await cancel_research_task(
                signed,
                agent["registration"],
                coordinator,
                ("remote-task", "different-context"),
                transport=httpx.MockTransport(handler),
            )
        assert methods.count("tasks/cancel") == 1
        config = {
            "name": "Wire test",
            "endpoint": endpoint,
            "key_id": "provider-v1",
            "private_key": private(provider_key),
            "agent_id": str(grant.audience_agent_id),
            "research_binding_version": str(grant.binding_version),
            "coordinator": coordinator.public_descriptor(),
            "research_plans": {
                grant.approved_plan_digest: {
                    "organization_id": str(grant.organization_id),
                    "project_id": str(grant.project_id),
                    "dataset_ids": [str(dataset) for dataset in grant.dataset_ids],
                    "values": [26, 28],
                    "unit": "C",
                },
            },
        }
        dispatched = await dispatch_research_task(
            signed,
            agent,
            scope,
            coordinator.id,
            coordinator.public_key,
            coordinator.private_key,
            coordinator.key_id,
            transport=httpx.ASGITransport(app=create_provider(config, tmp_path)),
        )
        assert dispatched["artifact"]["result"]["mean_celsius"] == 27
        supplied = {
            "dataset_ids": [str(item) for item in grant.dataset_ids],
            "values": [26, 28],
            "unit": "C",
        }
        provider_public = agent["registration"]
        outcome = verify_summary(
            signed, coordinator.public_descriptor(), provider_public, dispatched, supplied
        )
        assert outcome["verdict"] == "matched"
        assert (
            verify_summary(
                signed,
                coordinator.public_descriptor(),
                provider_public,
                dispatched,
                {**supplied, "values": [20, 34]},
            )["verdict"]
            == "challenged"
        )  # Same mean, different input bytes.
        bundle = tmp_path / "review.json"
        bundle.write_text(
            json.dumps(
                {
                    "signed_grant": signed.model_dump(mode="json"),
                    "coordinator_public": coordinator.public_descriptor(),
                    "provider_public": provider_public,
                    "verified_response": dispatched,
                    "inputs": supplied,
                }
            ),
            encoding="utf-8",
        )
        process = subprocess.run(
            [sys.executable, "-m", "orca.workers.verify_remote_summary", str(bundle)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert json.loads(process.stdout)["verdict"] == "matched"
        recovered = await read_research_result(
            signed,
            agent["registration"],
            coordinator,
            None,
            transport=httpx.ASGITransport(app=create_provider(config, tmp_path)),
        )
        assert recovered == dispatched
        assert recovered["a2a_task_id"] != str(grant.task_id)
        target = str(grant.task_id)
        read_token = jwt.encode(
            {
                "iss": str(coordinator.id),
                "aud": str(grant.audience_agent_id),
                "iat": int(now.timestamp()),
                "exp": int(now.timestamp()) + 90,
                "jti": target,
                "scope": "research:readback",
                "remote_task_id": target,
                "grant_digest": signed.grant_digest,
                "request_digest": digest(
                    {
                        "method": "tasks/get",
                        "id": target,
                        "grant_digest": signed.grant_digest,
                    }
                ),
            },
            coordinator_key,
            algorithm="EdDSA",
            headers={"kid": coordinator.key_id},
        )
        async with httpx.AsyncClient(
            base_url=endpoint,
            transport=httpx.ASGITransport(app=create_provider(config, tmp_path)),
            headers={"Authorization": "Bearer " + read_token},
        ) as http:
            for method, identifier in (("tasks/cancel", target), ("tasks/get", str(uuid4()))):
                denied = await http.post(
                    "/a2a",
                    json={
                        "jsonrpc": "2.0",
                        "id": "denied",
                        "method": method,
                        "params": {"id": identifier},
                    },
                )
                assert denied.status_code == 401
        grant = grant.model_copy(update={"expires_at": now - timedelta(seconds=1)})
        signed = sign_grant(grant, private(coordinator_key), "coordinator-v1")
        remote_state = TaskState.completed
        recovered = await read_research_result(
            signed,
            agent["registration"],
            coordinator,
            checkpoint[0],
            transport=httpx.MockTransport(handler),
        )
        assert recovered["a2a_task_id"] == "remote-task"
        remote_id = str(grant.task_id)
        recovered = await read_research_result(
            signed,
            agent["registration"],
            coordinator,
            None,
            transport=httpx.MockTransport(handler),
        )
        assert recovered["a2a_task_id"] == remote_id
        assert methods[-2:] == ["tasks/get", "tasks/get"]
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

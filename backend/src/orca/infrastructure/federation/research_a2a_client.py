"""Purpose: exchange a scoped P09 research grant with a pinned external A2A service.
Behavior: use safe HTTPS egress and verify the returned task and service-signed artifact.
Refs: main_implementation plan.md P09.2–P09.3 and P13.2; api design.md §17.1. Phase: P09/P13.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
from a2a.client import A2AClient
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    DataPart,
    GetTaskRequest,
    Message,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    TaskIdParams,
    TaskQueryParams,
)
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from orca.application.federation import (
    ResearchDispatchScope,
    SignedResearchGrant,
    validate_scope,
    verify_grant,
)
from orca.contracts.remote_dispatch import ProviderTemperatureSummary
from orca.infrastructure.federation.coordinator import FederationCoordinator
from orca.infrastructure.federation.https_egress import PublicProviderTransport, endpoint_origin
from orca.infrastructure.federation.signing import canonical, digest, unb64


class ResearchRequestNotSent(ValueError):
    """The client stopped before attempting to submit the A2A task."""


def verified_artifact(
    response: dict[str, Any], signed: SignedResearchGrant, registration: dict[str, Any]
) -> dict[str, Any]:
    """Accept one completed task and one exact, service-signed grant-bound artifact."""
    if "error" in response or not isinstance(response.get("result"), dict):
        raise ValueError("A2A request failed")
    task = response["result"]
    if task.get("kind") != "task" or task.get("status", {}).get("state") != "completed":
        raise ValueError("A2A task did not complete")
    if not isinstance(task.get("id"), str) or not isinstance(task.get("contextId"), str):
        raise ValueError("A2A task identity missing")
    artifacts = task.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise ValueError("Expected one A2A artifact")
    parts = artifacts[0].get("parts")
    if not isinstance(parts, list) or len(parts) != 1 or parts[0].get("kind") != "data":
        raise ValueError("Expected one structured A2A artifact")
    envelope = parts[0].get("data")
    if not isinstance(envelope, dict) or set(envelope) != {"payload", "key_id", "signature"}:
        raise ValueError("Signed artifact envelope is malformed")
    payload = envelope["payload"]
    grant = signed.grant
    if (
        not isinstance(payload, dict)
        or envelope["key_id"] != registration["key_id"]
        or payload.get("schema_version") != "orca.scientific_artifact/1.0"
        or payload.get("grant_digest") != signed.grant_digest
        or payload.get("task_id") != str(grant.task_id)
        or payload.get("run_id") != str(grant.run_id)
        or payload.get("approved_plan_digest") != grant.approved_plan_digest
        or payload.get("method_id") != grant.method_id
        or payload.get("dataset_ids") != [str(item) for item in grant.dataset_ids]
        or payload.get("a2a_task_id") != task["id"]
        or payload.get("a2a_context_id") != task["contextId"]
    ):
        raise ValueError("Artifact is not bound to the approved task")
    try:
        Ed25519PublicKey.from_public_bytes(unb64(registration["public_key"])).verify(
            unb64(envelope["signature"]), canonical(payload)
        )
    except (InvalidSignature, ValueError) as error:
        raise ValueError("Artifact signature differs from pinned service key") from error
    if len(canonical(payload)) > 256_000:
        raise ValueError("Artifact exceeds the response bound")
    if grant.capability_id == "temperature_summary_v1" and grant.method_id == "arithmetic_mean/1.0":
        if set(payload) != {
            "schema_version",
            "grant_digest",
            "task_id",
            "run_id",
            "approved_plan_digest",
            "method_id",
            "dataset_ids",
            "a2a_task_id",
            "a2a_context_id",
            "result",
        }:
            raise ValueError("Temperature summary contains unapproved artifact fields")
        summary = payload["result"]
        if summary != {"status": "insufficient_evidence"}:
            ProviderTemperatureSummary.model_validate(summary)
    return {
        "a2a_task_id": task["id"],
        "a2a_context_id": task["contextId"],
        "artifact_digest": digest(payload),
        "artifact": payload,
        "signed_artifact": envelope,
    }


def _task_ref(response: dict[str, Any]) -> tuple[str, str]:
    task = response.get("result")
    if not isinstance(task, dict) or task.get("kind") != "task":
        raise ValueError("A2A response did not identify a task")
    task_id, context_id = task.get("id"), task.get("contextId")
    if any(
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or any(ord(char) < 32 for char in value)
        for value in (task_id, context_id)
    ):
        raise ValueError("Invalid A2A task reference")
    assert isinstance(task_id, str) and isinstance(context_id, str)
    return task_id, context_id


@asynccontextmanager
async def _research_client(
    signed: SignedResearchGrant,
    registration: dict[str, Any],
    coordinator: FederationCoordinator,
    transport: httpx.AsyncBaseTransport | None,
    *,
    readback_task_id: str | None = None,
    classify_before_send: bool = False,
) -> AsyncIterator[tuple[A2AClient, dict[str, Any]]]:
    grant = verify_grant(signed, coordinator.public_key, coordinator.key_id)
    if grant.endpoint is not None and grant.endpoint != registration["endpoint"]:
        raise PermissionError("Research grant belongs to another endpoint")
    now = datetime.now(timezone.utc)
    if readback_task_id is None and not now < grant.expires_at:
        raise PermissionError("Research grant expired")
    origin = endpoint_origin(registration["endpoint"])
    data = {
        "schema_version": "orca.scientific_task/1.0",
        "grant": signed.model_dump(mode="json", exclude_none=True),
    }
    claims = {
        "iss": str(coordinator.id),
        "aud": str(grant.audience_agent_id),
        "iat": int(now.timestamp()),
        "exp": int(grant.expires_at.timestamp()),
        "jti": str(grant.task_id),
        "scope": "research:dispatch",
        "request_digest": digest(data),
        "grant_digest": signed.grant_digest,
    }
    if readback_task_id is not None:
        claims.update(
            exp=int(now.timestamp()) + 90,
            scope="research:readback",
            request_digest=digest(
                {
                    "method": "tasks/get",
                    "id": readback_task_id,
                    "grant_digest": signed.grant_digest,
                }
            ),
            remote_task_id=readback_task_id,
            grant_digest=signed.grant_digest,
        )
    token = jwt.encode(
        claims,
        Ed25519PrivateKey.from_private_bytes(unb64(coordinator.private_key)),
        algorithm="EdDSA",
        headers={"kid": coordinator.key_id},
    )
    async with httpx.AsyncClient(
        transport=transport or PublicProviderTransport(),
        timeout=25,
        trust_env=False,
        follow_redirects=False,
    ) as http:
        try:
            async with asyncio.timeout(45):
                response = await http.get(origin + "/.well-known/agent-card.json")
            response.raise_for_status()
            if len(response.content) > 32768:
                raise ValueError("Agent Card exceeds the response bound")
            raw = response.json()
            if digest(raw) != registration["card_digest"]:
                raise ValueError("Agent Card changed since registration")
            card = AgentCard.model_validate(raw)
            if (
                card.url != registration["endpoint"]
                or card.protocol_version != "0.3.0"
                or grant.capability_id not in {skill.id for skill in card.skills}
            ):
                raise ValueError("Agent Card does not match the approved binding")
        except (OSError, ValueError, httpx.HTTPError) as error:
            if classify_before_send:
                raise ResearchRequestNotSent(
                    "Provider card could not be verified before send"
                ) from error
            raise
        http.headers["Authorization"] = "Bearer " + token
        yield A2AClient(http, agent_card=card), data


async def dispatch_research_task(
    signed: SignedResearchGrant,
    agent: dict[str, Any],
    scope: ResearchDispatchScope,
    coordinator_id: UUID,
    coordinator_public_key: str,
    coordinator_private_key: str,
    coordinator_key_id: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    checkpoint: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Send one bounded official A2A message using current durable authorization facts."""
    grant = verify_grant(signed, coordinator_public_key, coordinator_key_id)
    now = datetime.now(timezone.utc)
    validate_scope(grant, **scope.model_dump(), now=now)
    registration = agent["registration"]
    if (
        agent["id"] != grant.audience_agent_id
        or agent["binding_version"] != grant.binding_version
        or agent["organization_id"] != grant.organization_id
        or agent["state"] != "active"
        or agent["qualification_expires_at"] <= now
        or not agent["research_dispatch_enabled"]
        or registration["capability"] != grant.capability_id
    ):
        raise PermissionError("Current provider binding is not authorized for this research task")
    coordinator = FederationCoordinator(
        coordinator_id, coordinator_key_id, coordinator_public_key, coordinator_private_key
    )
    async with _research_client(
        signed, registration, coordinator, transport, classify_before_send=True
    ) as (client, data):
        reply = await client.send_message(
            SendMessageRequest(
                id=str(uuid4()),
                params=MessageSendParams(
                    message=Message(
                        message_id=str(grant.task_id),
                        role=Role.user,
                        parts=[Part(root=DataPart(data=data))],
                    )
                ),
            )
        )
        sent = reply.model_dump(mode="json", by_alias=True)
        remote_ref = _task_ref(sent)
        if checkpoint:
            checkpoint(*remote_ref)
        result = verified_artifact(sent, signed, registration)
        readback = await client.get_task(
            GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=result["a2a_task_id"]))
        )
        stored = readback.model_dump(mode="json", by_alias=True)
        recorded = verified_artifact(stored, signed, registration)
        if recorded != result:
            raise ValueError("Stored A2A task differs from the signed response")
        return result


async def cancel_research_task(
    signed: SignedResearchGrant,
    registration: dict[str, Any],
    coordinator: FederationCoordinator,
    remote_ref: tuple[str, str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """Confirm provider cancellation by reading the same task after requesting cancellation."""
    async with _research_client(signed, registration, coordinator, transport) as (client, _):

        async def read() -> dict[str, Any]:
            reply = await client.get_task(
                GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=remote_ref[0]))
            )
            body = reply.model_dump(mode="json", by_alias=True)
            if _task_ref(body) != remote_ref:
                raise ValueError("Remote cancellation task reference changed")
            return dict(body["result"])

        current = (await read())["status"]["state"]
        if current == "canceled":
            return True
        if current in {"completed", "failed", "rejected"}:
            return False
        await client.cancel_task(
            CancelTaskRequest(id=str(uuid4()), params=TaskIdParams(id=remote_ref[0]))
        )
        return bool((await read())["status"]["state"] == "canceled")


async def read_research_result(
    signed: SignedResearchGrant,
    registration: dict[str, Any],
    coordinator: FederationCoordinator,
    remote_ref: tuple[str, str] | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Read one existing task; providers supporting recovery may index it by grant task UUID."""
    task_id = remote_ref[0] if remote_ref else str(signed.grant.task_id)
    async with _research_client(
        signed,
        registration,
        coordinator,
        transport,
        readback_task_id=task_id,
    ) as (client, _):
        reply = await client.get_task(
            GetTaskRequest(id=str(uuid4()), params=TaskQueryParams(id=task_id))
        )
        body = reply.model_dump(mode="json", by_alias=True)
        actual_ref = _task_ref(body)
        if remote_ref is not None and actual_ref != remote_ref:
            raise ValueError("Remote readback task reference changed")
        return verified_artifact(body, signed, registration)

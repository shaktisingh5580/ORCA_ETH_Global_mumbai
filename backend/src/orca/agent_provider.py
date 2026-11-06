"""Purpose: run a separately keyed real A2A reproduction or numerical-review provider.
Behavior: authenticate scoped coordinator requests, persist tasks privately and publish own Arkiv evidence.
Refs: H04 decision; A2A 0.3.0 SDK 0.3.22; canonical artifact contract; Phase: H04.
"""

import argparse
import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid5

import jwt
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.apps.jsonrpc.jsonrpc_app import CallContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    DataPart,
    HTTPAuthSecurityScheme,
    Part,
    SecurityScheme,
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from orca.contracts.network import AgentBinding, SignedEnvelope
from orca.infrastructure.federation.public_events import public_event
from orca.infrastructure.federation.reproduction import reproduce
from orca.infrastructure.federation.runtime import (
    ArkivGateway,
    ChainUnavailable,
    NetworkConfiguration,
)
from orca.infrastructure.federation.signing import (
    CHAIN_ID,
    DOMAIN,
    digest,
    sign,
    strict_json,
    unb64,
    verify,
)


class ProviderSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCA_PROVIDER_", extra="forbid")
    binding_file: str
    coordinator_file: str
    private_key: SecretStr
    wallet_env_file: str
    capture_folder: str
    state_dir: str
    source_mode: Literal["real", "synthetic_demo"] = "real"
    capture_variants: dict[str, str] = {}
    wallet_state_dir: str | None = None


class DurableTasks(TaskStore):
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "tasks.sqlite3"
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,owner TEXT NOT NULL,data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,digest TEXT NOT NULL,data TEXT NOT NULL)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS cancelled(id TEXT PRIMARY KEY)")
        self.execution_lock = threading.Lock()

    def connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=10)

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        owner = str(context.state["authorization"]["jti"]) if context else "unowned"
        with self.connection() as db:
            previous = db.execute("SELECT owner FROM tasks WHERE id=?", (task.id,)).fetchone()
            if previous is not None and previous[0] != owner:
                raise PermissionError("A2A task owner differs")
            db.execute(
                "INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                (task.id, owner, task.model_dump_json(by_alias=True)),
            )

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        if context is None:
            return None
        with self.connection() as db:
            row = db.execute(
                "SELECT data FROM tasks WHERE id=? AND owner=?",
                (task_id, str(context.state["authorization"]["jti"])),
            ).fetchone()
        return Task.model_validate_json(row[0]) if row else None

    async def delete(self, task_id: str, context: ServerCallContext | None = None) -> None:
        raise PermissionError("provider task retention is operator controlled")


class Context(CallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        return ServerCallContext(state={"authorization": request.scope["state"]["authorization"]})


class ServiceAuthentication:
    def __init__(self, app: ASGIApp, binding: AgentBinding, coordinator: AgentBinding):
        self.app, self.binding, self.coordinator = app, binding, coordinator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/.well-known/agent-card.json":
            await self.app(scope, receive, send)
            return
        try:
            authorization = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not authorization.startswith("Bearer ") or len(authorization) > 4096:
                raise ValueError("missing service bearer token")
            token = authorization[7:]
            if jwt.get_unverified_header(token).get("kid") != self.coordinator.key_id:
                raise ValueError("unregistered service key")
            claims = jwt.decode(
                token,
                Ed25519PublicKey.from_public_bytes(unb64(self.coordinator.public_key)),
                algorithms=["EdDSA"],
                audience=str(self.binding.id),
                issuer=str(self.coordinator.id),
                options={
                    "require": [
                        "iss",
                        "aud",
                        "iat",
                        "exp",
                        "jti",
                        "task_id",
                        "scope",
                        "request_digest",
                    ]
                },
                leeway=5,
            )
            if claims["exp"] - claims["iat"] > 300 or claims["scope"] not in {"claim", "execute"}:
                raise ValueError("invalid service scope/lifetime")
            UUID(claims["jti"])
            UUID(claims["task_id"])
            scope.setdefault("state", {})["authorization"] = claims
        except (ValueError, jwt.InvalidTokenError, KeyError):
            await JSONResponse(
                {"detail": "Valid scoped agent bearer token required"}, status_code=401
            )(scope, receive, send)
            return
        # Bound and validate JSON before the SDK parses it, rejecting duplicate keys/NaN.
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > 131072:
                await JSONResponse({"detail": "Agent request too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            chunks.append(chunk)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        try:
            if body:
                strict_json(body)
        except ValueError:
            await JSONResponse({"detail": "Strict JSON required"}, status_code=400)(
                scope, receive, send
            )
            return
        sent = False

        async def replay() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return dict(await receive())

        await self.app(scope, replay, send)


class ScientificAgent(AgentExecutor):
    def __init__(
        self,
        settings: ProviderSettings,
        binding: AgentBinding,
        coordinator: AgentBinding,
        store: DurableTasks,
    ):
        self.settings, self.binding, self.coordinator, self.store = (
            settings,
            binding,
            coordinator,
            store,
        )
        self.gateway = ArkivGateway(
            Path(settings.wallet_env_file),
            Path(settings.wallet_state_dir or settings.state_dir),
            binding.wallet,
        )

    def process(self, data: dict[str, Any], authorization: dict[str, Any]) -> dict[str, Any]:
        operation_id = authorization["jti"]
        if (
            digest(data) != authorization["request_digest"]
            or data["operation"] != authorization["scope"]
            or data["task"]["id"] != authorization["task_id"]
        ):
            raise ValueError("delegation request differs from signed scope")
        task = data["task"]
        if task.get("source_mode", "real") != self.settings.source_mode:
            raise ValueError("delegation source profile differs from configured provider")
        if task["capability"] != self.binding.capability or datetime.fromisoformat(
            task["deadline"]
        ) <= datetime.now(timezone.utc):
            raise ValueError("unsupported or expired task")
        with self.store.execution_lock, self.store.connection() as db:
            previous = db.execute(
                "SELECT digest,data FROM operations WHERE id=?", (operation_id,)
            ).fetchone()
            if previous:
                if previous[0] != digest(data):
                    raise ValueError("agent operation replay differs")
                return dict(strict_json(previous[1]))
            if db.execute("SELECT 1 FROM cancelled WHERE id=?", (operation_id,)).fetchone():
                raise ValueError("agent task cancelled")
            parent_key = ""
            if task["publication_mode"] == "public_reference":
                event = SignedEnvelope.model_validate(data["requester_event"]["envelope"])
                expected_kind = "task" if data["operation"] == "claim" else "assignment"
                if (
                    event.payload["kind"] != expected_kind
                    or event.payload["task_id"] != task["id"]
                    or event.payload["recipe_digest"] != task["recipe_digest"]
                ):
                    raise ValueError("public requester record differs from delegation")
                parent_key = data["requester_event"]["receipt"]["entity_key"]
                self.gateway.read(parent_key, self.coordinator, event)
                # Discovery is a live shared-board query, not an ORCA database lookup.
                board = self.gateway.call({"command": "query", "task_id": task["id"]})
                if board["truncated"] or not any(
                    record["entity_key"] == parent_key for record in board["records"]
                ):
                    raise ValueError("requester record is not currently discoverable on Arkiv")
            if data["operation"] == "claim":
                payload = {
                    "schema_version": "orca.agent-claim/1.0",
                    "domain": DOMAIN,
                    "chain_id": CHAIN_ID,
                    "audience": str(self.coordinator.id),
                    "task_id": task["id"],
                    "assignment_generation": 0,
                    "binding_version": str(self.binding.binding_version),
                    "provider_id": str(self.binding.id),
                    "recipe_digest": task["recipe_digest"],
                    "deadline": task["deadline"],
                    "issued_at": data["issued_at"],
                    "operator_mode": "team_controlled",
                    "institution_verified": False,
                }
                kind, generation, subject = "claim", 0, str(self.binding.id)
            else:
                assignment = SignedEnvelope.model_validate(data["assignment"])
                verify(
                    assignment,
                    self.coordinator.public_key,
                    self.coordinator.key_id,
                    str(self.binding.id),
                )
                selected = assignment.payload
                claim = SignedEnvelope.model_validate(data["claim"])
                verify(
                    claim,
                    self.binding.public_key,
                    self.binding.key_id,
                    "public" if claim.payload.get("kind") == "claim" else str(self.coordinator.id),
                )
                claimed_provider = claim.payload.get("provider_id", claim.payload.get("actor_id"))
                if (
                    claim.content_digest != selected["claim_digest"]
                    or claim.payload["task_id"] != task["id"]
                    or claimed_provider != str(self.binding.id)
                ):
                    raise ValueError("assignment does not select this provider's signed claim")
                if (
                    selected["task_id"] != task["id"]
                    or selected["provider_id"] != str(self.binding.id)
                    or selected["provider_binding_version"] != str(self.binding.binding_version)
                    or selected["assignment_generation"] != task["generation"]
                    or selected["recipe_digest"] != task["recipe_digest"]
                    or digest(data["recipe"]) != task["recipe_digest"]
                ):
                    raise ValueError("assignment identity or recipe differs")
                if (
                    task["publication_mode"] == "public_reference"
                    and data["requester_event"]["envelope"]["payload"]["reference_digest"]
                    != assignment.content_digest
                ):
                    raise ValueError("public assignment reference differs from signed delegation")
                variant = data["recipe"].get("input_variant", "primary")
                if variant != "primary" and (
                    self.settings.source_mode != "synthetic_demo"
                    or variant not in self.settings.capture_variants
                ):
                    raise ValueError("unregistered provider input variant")
                folder = (
                    self.settings.capture_folder
                    if variant == "primary"
                    else self.settings.capture_variants[variant]
                )
                computed = reproduce(
                    Path(folder),
                    data["recipe"],
                    review=self.binding.capability == "sst_review",
                    source_mode=self.settings.source_mode,
                )
                artifact_id = str(uuid5(UUID(operation_id), "artifact"))
                payload = {
                    "schema_version": "orca.scientific_artifact/1.0",
                    "domain": DOMAIN,
                    "chain_id": CHAIN_ID,
                    "audience": str(self.coordinator.id),
                    "task_id": task["id"],
                    "assignment_generation": task["generation"],
                    "binding_version": str(self.binding.binding_version),
                    "producer_agent_id": str(self.binding.id),
                    "artifact_id": artifact_id,
                    "artifact_version": 1,
                    "attempt_id": selected["attempt_id"],
                    "recipe_digest": task["recipe_digest"],
                    "method_ref": data["recipe"]["method"],
                    "input_manifest_ref": computed["input_manifest_digest"],
                    "results": computed,
                    "review_of_digest": data["recipe"]["review_of_digest"],
                    "corrects_digest": data["recipe"].get("corrects_digest"),
                    "issued_at": data["issued_at"],
                }
                kind, generation, subject = (
                    ("review" if self.binding.capability == "sst_review" else "result"),
                    task["generation"],
                    artifact_id,
                )
            envelope = sign(
                payload, self.settings.private_key.get_secret_value(), self.binding.key_id
            )
            event = public_event(
                task=task,
                kind=kind,
                actor=str(self.binding.id),
                binding_version=str(self.binding.binding_version),
                subject=subject,
                generation=generation,
                parent_key=parent_key,
                reference_digest=envelope.content_digest,
                operation_id=operation_id,
                issued_at=data["issued_at"],
                private_key=self.settings.private_key.get_secret_value(),
                key_id=self.binding.key_id,
            )
            receipt = (
                self.gateway.publish(event, self.binding)
                if task["publication_mode"] == "public_reference"
                else None
            )
            result = {
                "envelope": envelope.model_dump(mode="json"),
                "public_event": event.model_dump(mode="json"),
                "receipt": receipt,
            }
            db.execute(
                "INSERT INTO operations VALUES(?,?,?)",
                (operation_id, digest(data), json.dumps(result, allow_nan=False)),
            )
            return result

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A task/context identity required")
        try:
            if (
                context.message is None
                or context.call_context is None
                or len(context.message.parts) != 1
            ):
                raise ValueError("one scoped data message required")
            part = context.message.parts[0].root
            if not isinstance(part, DataPart):
                raise ValueError("structured task data required")
            result = await asyncio.to_thread(
                self.process, part.data, context.call_context.state["authorization"]
            )
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.completed),
                    artifacts=[
                        Artifact(
                            artifact_id=result["envelope"]["content_digest"],
                            parts=[Part(root=DataPart(data=result))],
                        )
                    ],
                )
            )
        except Exception as error:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.failed),
                    metadata={
                        "error_code": "ARKIV_RECONCILIATION_REQUIRED"
                        if isinstance(error, ChainUnavailable) and error.reconciliation
                        else "PROVIDER_TASK_REJECTED_OR_DEPENDENCY_UNAVAILABLE"
                    },
                )
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.call_context is None or not context.task_id or not context.context_id:
            raise PermissionError("service authentication required")
        with self.store.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO cancelled VALUES(?)",
                (context.call_context.state["authorization"]["jti"],),
            )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.canceled),
                final=True,
            )
        )


def create_provider(settings: ProviderSettings) -> ASGIApp:
    binding = AgentBinding.model_validate(strict_json(Path(settings.binding_file).read_bytes()))
    configured = NetworkConfiguration.model_validate(
        strict_json(Path(settings.coordinator_file).read_bytes())
    )
    configured.validate_endpoints()
    if binding not in configured.agents:
        raise ValueError("provider is not in the pinned operator configuration")
    store = DurableTasks(Path(settings.state_dir))
    card = AgentCard(
        name=binding.name,
        description="Team-operated numerical research prototype; no ENS/institutional verification",
        url=binding.endpoint,
        version="1.0.0",
        protocol_version="0.3.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=binding.capability,
                name=binding.capability,
                description="Recompute preserved public January SST inputs",
                tags=["marine", "research", "prototype"],
            )
        ],
        security_schemes={
            "scopedBearer": SecurityScheme(
                root=HTTPAuthSecurityScheme(scheme="bearer", bearer_format="JWT")
            )
        },
        security=[{"scopedBearer": []}],
    )
    handler = DefaultRequestHandler(
        agent_executor=ScientificAgent(settings, binding, configured.coordinator, store),
        task_store=store,
    )
    app = A2AStarletteApplication(
        agent_card=card, http_handler=handler, context_builder=Context(), max_content_length=131072
    ).build(rpc_url="/a2a")
    return ServiceAuthentication(app, binding, configured.coordinator)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()
    settings = ProviderSettings(_env_file=args.env_file)  # type: ignore[call-arg]
    binding = AgentBinding.model_validate(strict_json(Path(settings.binding_file).read_bytes()))
    port = int(binding.endpoint.split(":")[-1].split("/")[0])
    uvicorn.run(create_provider(settings), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()

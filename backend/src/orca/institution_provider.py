"""Purpose: provide an independently deployable, institution-controlled A2A onboarding service.
Behavior: keep keys/state local, accept operator-approved coordinator tokens and compute bounded summaries.
Refs: institutional implementation plan F02.4; A2A 0.3.0; Phase: F02.
"""

import argparse
import asyncio
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jwt
import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from orca.agent_provider import Context
from orca.infrastructure.federation.provider_research import ResearchTasks, approved_inputs
from orca.infrastructure.federation.signing import b64, canonical, digest, strict_json, unb64


def card(config: dict[str, Any]) -> AgentCard:
    return AgentCard(
        name=config["name"],
        description="Institution-operated bounded temperature summary service; affiliation self-declared",
        url=config["endpoint"],
        version="1.0.0",
        protocol_version="0.3.0",
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id="temperature_summary_v1",
                name="Temperature summary",
                description="Arithmetic mean of explicitly supplied Celsius or Kelvin values; no causal inference",
                tags=["research", "temperature"],
            )
        ],
        security_schemes={
            "scopedBearer": SecurityScheme(
                root=HTTPAuthSecurityScheme(scheme="bearer", bearer_format="JWT")
            )
        },
        security=[{"scopedBearer": []}],
    )


def compute(data: dict[str, Any]) -> dict[str, Any]:
    if (
        set(data) != {"schema_version", "operation_id", "values", "unit"}
        or data["schema_version"] != "orca.provider-qualification/1.0"
    ):
        raise ValueError("Unsupported qualification request")
    values = data["values"]
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= 1000
        or data["unit"] not in {"C", "K"}
        or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
            for x in values
        )
    ):
        raise ValueError("Finite temperature values and explicit unit required")
    normalized = [x - 273.15 if data["unit"] == "K" else x for x in values]
    mean = math.fsum(normalized) / len(normalized)
    if not math.isfinite(mean):
        raise ValueError("Non-finite result")
    return {
        "method": "arithmetic_mean/1.0",
        "count": len(normalized),
        "mean_celsius": mean,
        "input_digest": digest(data),
        "source_mode": "qualification_inputs",
        "scientific_scope": "supplied_values_only",
    }


class QualificationAgent(AgentExecutor):
    def __init__(self, config: dict[str, Any], store: ResearchTasks):
        self.config, self.store = config, store

    def process(self, data: dict[str, Any], auth: dict[str, Any]) -> dict[str, Any]:
        if data.get("operation_id") != auth["jti"] or digest(data) != auth["request_digest"]:
            raise ValueError("Request differs from signed scope")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT digest,data FROM operations WHERE id=?", (auth["jti"],)
            ).fetchone()
            if previous:
                if previous[0] != digest(data):
                    raise ValueError("Operation payload changed")
                return dict(json.loads(previous[1]))
            if db.execute("SELECT 1 FROM cancelled WHERE id=?", (auth["jti"],)).fetchone():
                raise ValueError("Operation cancelled")
            result = compute(data)
            envelope = {
                "payload": result,
                "key_id": self.config["key_id"],
                "signature": b64(
                    Ed25519PrivateKey.from_private_bytes(unb64(self.config["private_key"])).sign(
                        canonical(result)
                    )
                ),
            }
            db.execute(
                "INSERT INTO operations VALUES(?,?,?)",
                (auth["jti"], digest(data), json.dumps(envelope)),
            )
            return envelope

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if (
            not context.task_id
            or not context.context_id
            or not context.call_context
            or not context.message
        ):
            raise ValueError("Authenticated task context required")
        try:
            if len(context.message.parts) != 1 or not isinstance(
                context.message.parts[0].root, DataPart
            ):
                raise ValueError("One structured message required")
            data = context.message.parts[0].root.data
            auth = context.call_context.state["authorization"]
            signed = None
            if auth["scope"] == "research:dispatch":
                signed, inputs = approved_inputs(data, auth, self.config)
                grant = signed.grant
                summary = compute(inputs)
                summary["source_mode"] = "operator_approved_values"
                payload = {
                    "schema_version": "orca.scientific_artifact/1.0",
                    "grant_digest": signed.grant_digest,
                    "task_id": str(grant.task_id),
                    "run_id": str(grant.run_id),
                    "approved_plan_digest": grant.approved_plan_digest,
                    "method_id": grant.method_id,
                    "dataset_ids": [str(item) for item in grant.dataset_ids],
                    "a2a_task_id": context.task_id,
                    "a2a_context_id": context.context_id,
                    "result": summary,
                }
                result = {
                    "payload": payload,
                    "key_id": self.config["key_id"],
                    "signature": b64(
                        Ed25519PrivateKey.from_private_bytes(
                            unb64(self.config["private_key"])
                        ).sign(canonical(payload))
                    ),
                }
            else:
                result = await asyncio.to_thread(self.process, data, auth)
            task = Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.completed),
                artifacts=[
                    Artifact(artifact_id=digest(result), parts=[Part(root=DataPart(data=result))])
                ],
            )
            if signed is not None:
                await asyncio.to_thread(self.store.complete, signed, task)
        except (KeyError, TypeError, ValueError, OverflowError):
            task = Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.failed),
                metadata={"error_code": "QUALIFICATION_INPUT_REJECTED"},
            )
        await event_queue.enqueue_event(task)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id or context.current_task is None:
            raise PermissionError("Authenticated task required")
        with self.store.connection() as db:
            # SDK 0.3.22 authenticates TaskStore.get before invoking cancellation,
            # but omits call_context in this callback. Read that task's durable
            # owner instead; the HTTP middleware and task-store check remain mandatory.
            stored = db.execute(
                "SELECT owner,data FROM tasks WHERE id=?", (context.task_id,)
            ).fetchone()
            if stored is None or json.loads(stored[1])["contextId"] != context.context_id:
                raise PermissionError("Stored task context required")
            db.execute(
                "INSERT OR IGNORE INTO cancelled VALUES(?)",
                (stored[0],),
            )
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.canceled),
                final=True,
            )
        )


class Authentication:
    def __init__(self, app: ASGIApp, config: dict[str, Any]):
        self.app, self.config = app, config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/.well-known/agent-card.json":
            await self.app(scope, receive, send)
            return
        try:
            coordinator = self.config["coordinator"]
            token = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not token.startswith("Bearer ") or len(token) > 4096:
                raise ValueError()
            token = token[7:]
            if jwt.get_unverified_header(token).get("kid") != coordinator["key_id"]:
                raise ValueError()
            auth = jwt.decode(
                token,
                Ed25519PublicKey.from_public_bytes(unb64(coordinator["public_key"])),
                algorithms=["EdDSA"],
                audience=self.config["agent_id"],
                issuer=coordinator["id"],
                options={"require": ["iss", "aud", "iat", "exp", "jti", "scope", "request_digest"]},
            )
            window = auth["exp"] - auth["iat"]
            lifetime = {"agent:qualify": 120, "research:dispatch": 300, "research:readback": 90}
            if not 0 < window <= lifetime[auth["scope"]]:
                raise ValueError()
            if auth["scope"] != "agent:qualify" and not self.config.get("research_plans"):
                raise ValueError()
            if auth["scope"] == "research:readback":
                chunks = bytearray()
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return
                    chunks.extend(message.get("body", b""))
                    if len(chunks) > 4096:
                        raise ValueError()
                    if not message.get("more_body", False):
                        break
                rpc = strict_json(bytes(chunks))
                target = auth["remote_task_id"]
                if (
                    scope.get("path") != "/a2a"
                    or not isinstance(rpc, dict)
                    or rpc.get("method") != "tasks/get"
                    or not isinstance(target, str)
                    or not 1 <= len(target) <= 256
                    or rpc.get("params") != {"id": target}
                    or digest(
                        {"method": "tasks/get", "id": target, "grant_digest": auth["grant_digest"]}
                    )
                    != auth["request_digest"]
                ):
                    raise ValueError()
                original_receive = receive
                pending = True

                async def replay() -> Any:
                    nonlocal pending
                    if pending:
                        pending = False
                        return {"type": "http.request", "body": bytes(chunks), "more_body": False}
                    return await original_receive()

                receive = replay
            scope.setdefault("state", {})["authorization"] = auth
        except (KeyError, TypeError, ValueError, jwt.InvalidTokenError):
            await JSONResponse(
                {"detail": "Operator-approved scoped token required"}, status_code=401
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_provider(config: dict[str, Any], directory: Path) -> ASGIApp:
    store = ResearchTasks(directory)
    os.chmod(directory, 0o700)
    os.chmod(store.path, 0o600)
    handler = DefaultRequestHandler(
        agent_executor=QualificationAgent(config, store), task_store=store
    )
    app = A2AStarletteApplication(
        agent_card=card(config),
        http_handler=handler,
        context_builder=Context(),
        max_content_length=65536,
    ).build(rpc_url="/a2a")
    return Authentication(app, config)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["init", "describe", "accept-challenge", "serve"])
    parser.add_argument("--directory", required=True)
    parser.add_argument("--endpoint")
    parser.add_argument("--name", default="Institution temperature service")
    parser.add_argument("--challenge")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    folder = Path(args.directory)
    file = folder / "provider.json"
    if args.action == "init":
        if file.exists():
            raise SystemExit("Existing provider keys preserved; choose another directory")
        from orca.infrastructure.federation.https_egress import endpoint_origin

        endpoint_origin(args.endpoint or "")
        folder.mkdir(parents=True, exist_ok=True)
        private = Ed25519PrivateKey.generate()
        config = {
            "name": args.name,
            "endpoint": args.endpoint,
            "key_id": "institution-service-v1",
            "private_key": b64(
                private.private_bytes(
                    serialization.Encoding.Raw,
                    serialization.PrivateFormat.Raw,
                    serialization.NoEncryption(),
                )
            ),
            "public_key": b64(
                private.public_key().public_bytes(
                    serialization.Encoding.Raw, serialization.PublicFormat.Raw
                )
            ),
        }
        file.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(file, 0o600)
    config = strict_json(file.read_bytes())
    if args.action in {"init", "describe"}:
        print(
            json.dumps(
                {
                    "name": config["name"],
                    "endpoint": config["endpoint"],
                    "public_key": config["public_key"],
                    "key_id": config["key_id"],
                    "card_digest": digest(
                        card(config).model_dump(mode="json", by_alias=True, exclude_none=True)
                    ),
                    "operator_mode": "self_declared_external",
                    "capability": "temperature_summary_v1",
                    "disclosure_policy": "task_inputs_only",
                },
                indent=2,
            )
        )
    elif args.action == "accept-challenge":
        payload = strict_json(Path(args.challenge).read_bytes())
        if "payload" in payload:
            payload = payload["payload"]
        if (
            payload["endpoint"] != config["endpoint"]
            or payload["key_id"] != config["key_id"]
            or payload["card_digest"]
            != digest(card(config).model_dump(mode="json", by_alias=True, exclude_none=True))
            or payload["schema_version"] != "orca.registration-proof/1.0"
            or payload.get("audience") != "orca-agent-onboarding"
            or datetime.fromisoformat(payload["expires_at"]) <= datetime.now(timezone.utc)
        ):
            raise SystemExit("Challenge does not match this provider")
        config.update(coordinator=payload["coordinator"], agent_id=payload["agent_id"])
        file.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(file, 0o600)
        print(
            json.dumps(
                {
                    "challenge_id": payload["challenge_id"],
                    "signature": b64(
                        Ed25519PrivateKey.from_private_bytes(unb64(config["private_key"])).sign(
                            canonical(payload)
                        )
                    ),
                }
            )
        )
    else:
        uvicorn.run(
            create_provider(config, folder), host=args.host, port=args.port, log_level="warning"
        )


if __name__ == "__main__":
    main()

"""Purpose: expose the existing dashboard's agent task/claim/assignment/timeline workflow.
Behavior: require verified project authority and queue durable actions rather than fake completion.
Refs: api design.md network guidance; H04-agent-coordination.json; Phase: H04.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.organizations import errors
from orca.api.routers.projects import engine, resolve_project, resolve_tenant
from orca.application.network import Network, operation_view
from orca.contracts.network import (
    AgentBinding,
    AssignClaim,
    CancelNetworkTask,
    CreateNetworkTask,
    NetworkOperation,
    NetworkTask,
    RequestClaim,
    SignedEnvelope,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.runtime import (
    ChainUnavailable,
    NetworkRuntime,
    verify_chain_record,
)

router = APIRouter(prefix="/v1/network", tags=["agent coordination"])
Idempotency = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def runtime(request: Request) -> NetworkRuntime:
    configured = getattr(request.app.state, "network_runtime", None)
    if configured is None:
        raise HTTPException(503, "Agent coordination is not configured")
    return cast(NetworkRuntime, configured)


@contextmanager
def network_errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except ChainUnavailable as error:
            raise HTTPException(
                503,
                "Live Arkiv read unavailable; saved observations are not fresh chain verification",
            ) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error


@router.get("/config")
def capabilities(request: Request) -> dict[str, Any]:
    configured = runtime(request)
    return {
        "profile": "hackathon-agent-coordination",
        "a2a_protocol": "0.3.0",
        "a2a_sdk": "0.3.22",
        "chain": "Rabdi",
        "chain_id": 7794095,
        "operator_mode": "team_controlled",
        "ens_verified": False,
        "institution_verified": False,
        "capabilities": sorted({agent.capability for agent in configured.configuration.agents}),
        "public_reference_consent_required": True,
    }


@router.get("/agents", response_model=list[AgentBinding])
def agents(
    organization_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[AgentBinding]:
    runtime(request)
    db = engine(request)
    tenant, roles = resolve_tenant(db, principal, organization_id)
    try:
        principal.require("project:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Project read permission required") from error
    with TenantUnitOfWork(db, tenant) as session:
        bindings: list[Any] = list(
            session.execute(
                text(
                    "SELECT binding FROM federation.agents WHERE tenant_id=:t AND NOT revoked ORDER BY id LIMIT 100"
                ),
                {"t": tenant},
            ).scalars()
        )
        return [AgentBinding.model_validate(row) for row in bindings]


@router.post("/tasks", response_model=NetworkOperation, status_code=202)
def create_task(
    body: CreateNetworkTask,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Idempotency,
) -> NetworkOperation:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, body.project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        return Network(session, tenant, body.project_id, principal).create(body, idempotency_key)


@router.get("/tasks", response_model=list[NetworkTask])
def tasks(project_id: UUID, request: Request, principal: CurrentPrincipal) -> list[NetworkTask]:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        service = Network(session, tenant, project_id, principal)
        service.authorize()
        identifiers: list[UUID] = list(
            session.execute(
                text(
                    "SELECT id FROM federation.tasks WHERE tenant_id=:t AND project_id=:p ORDER BY created_at DESC LIMIT 30"
                ),
                {"t": tenant, "p": project_id},
            ).scalars()
        )
        return [service.detail(identifier) for identifier in identifiers]


@router.get("/tasks/{task_id}", response_model=NetworkTask)
def task(
    task_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal
) -> NetworkTask:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        return Network(session, tenant, project_id, principal).detail(task_id)


@router.post("/tasks/{task_id}/claims", response_model=NetworkOperation, status_code=202)
def claim(
    task_id: UUID,
    body: RequestClaim,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Idempotency,
) -> NetworkOperation:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        return Network(session, tenant, project_id, principal).request_claim(
            task_id, body.provider_id, idempotency_key
        )


@router.post("/tasks/{task_id}/assignments", response_model=NetworkOperation, status_code=202)
def assign(
    task_id: UUID,
    body: AssignClaim,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Idempotency,
) -> NetworkOperation:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        return Network(session, tenant, project_id, principal).assign(
            task_id, body, idempotency_key
        )


@router.post("/tasks/{task_id}/cancel", response_model=NetworkOperation, status_code=202)
def cancel(
    task_id: UUID,
    body: CancelNetworkTask,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Idempotency,
) -> NetworkOperation:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        return Network(session, tenant, project_id, principal).cancel(
            task_id, body.expected_generation, idempotency_key
        )


@router.get("/operations/{operation_id}", response_model=NetworkOperation)
def operation(
    operation_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal
) -> NetworkOperation:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        service = Network(session, tenant, project_id, principal)
        row = (
            session.execute(
                text("SELECT * FROM federation.operations WHERE tenant_id=:t AND id=:id"),
                {"t": tenant, "id": operation_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("operation not found")
        service.task_row(row["task_id"])
        return operation_view(row)


@router.get("/tasks/{task_id}/timeline")
def timeline(
    task_id: UUID,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    refresh_chain: bool = False,
) -> dict[str, Any]:
    configured = runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        service = Network(session, tenant, project_id, principal)
        task_row = service.task_row(task_id)
        rows = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT id,kind,actor_id,envelope,publication_mode,receipt,created_at FROM federation.events WHERE tenant_id=:t AND task_id=:id ORDER BY created_at LIMIT 100"
                ),
                {"t": tenant, "id": task_id},
            ).mappings()
        ]
        bindings = {
            str(agent.id): agent
            for agent in [configured.configuration.coordinator, *configured.configuration.agents]
        }
    observed: dict[str, Any] | None = None
    if refresh_chain and task_row["publication_mode"] == "public_reference":
        with network_errors():
            response = configured.gateway.call({"command": "query", "task_id": str(task_id)})
            accepted = []
            for row in rows:
                expected = SignedEnvelope.model_validate(row["envelope"])
                binding = bindings.get(str(row["actor_id"]))
                if binding is None:
                    continue
                for record in response["records"]:
                    if record["envelope"]["content_digest"] == expected.content_digest:
                        verify_chain_record(record, binding, expected)
                        accepted.append(record)
            observed = {
                "records": accepted,
                "truncated": response["truncated"],
                "observed_at": response["observed_at"],
            }
    # Revalidate authorization after network I/O before releasing private saved context.
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        Network(session, tenant, project_id, principal).task_row(task_id)
    return {
        "task_id": task_id,
        "events": rows,
        "live_chain": observed,
        "saved_receipts_are_historical_observations": True,
        "institution_verified": False,
    }

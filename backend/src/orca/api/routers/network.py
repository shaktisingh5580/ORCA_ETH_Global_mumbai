"""Purpose: expose the existing dashboard's agent task/claim/assignment/timeline workflow.
Behavior: require verified project authority and queue durable actions rather than fake completion.
Refs: api design.md §3.12; main_implementation plan.md P11.2–P11.4; H04-agent-coordination.json. Phase: H04/P11.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.organizations import errors
from orca.api.routers.projects import engine, resolve_project, resolve_tenant
from orca.application.network import Network, operation_view
from orca.contracts.network import (
    ApproveArkivEvidence,
    AssignClaim,
    CancelNetworkTask,
    CreateNetworkTask,
    ImportClaim,
    NetworkOperation,
    NetworkTask,
    RequestClaim,
    SignedEnvelope,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.federation.discovery import discover_open
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


@router.get("/board")
def board(
    organization_id: UUID,
    capability: Literal["sst_reproduction", "sst_review"],
    request: Request,
    principal: CurrentPrincipal,
    source_mode: Literal["real", "synthetic_demo"] = "real",
) -> dict[str, Any]:
    configured = runtime(request)
    db = engine(request)
    _, roles = resolve_tenant(db, principal, organization_id)
    try:
        principal.require("project:read", roles)
    except PermissionError as error:
        raise HTTPException(403, "Project read permission required") from error
    with network_errors():
        return discover_open(
            configured.gateway,
            configured.configuration.coordinator,
            capability,
            source_mode=source_mode,
        )


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


@router.post("/tasks/{task_id}/claims/import", response_model=NetworkOperation, status_code=202)
def import_claim(
    task_id: UUID,
    body: ImportClaim,
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
            task_id, body.provider_id, idempotency_key, body.entity_key, body.arkiv_v1_entity_key
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


@router.post(
    "/operations/{operation_id}/arkiv-recovery", response_model=NetworkOperation, status_code=202
)
def recover_arkiv_operation(
    operation_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal
) -> NetworkOperation:
    runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        return Network(session, tenant, project_id, principal).request_arkiv_recovery(operation_id)


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


@router.post("/tasks/{task_id}/arkiv-evidence/intent")
def prepare_arkiv_evidence(
    task_id: UUID,
    approval: ApproveArkivEvidence,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    configured = runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        service = Network(session, tenant, project_id, principal)
        service.authorize("project:write")
        return service.arkiv_evidence_intent(task_id, configured.configuration.coordinator)


@router.get("/tasks/{task_id}/arkiv-evidence")
def read_arkiv_evidence(
    task_id: UUID,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    cursor: Annotated[str | None, Query(pattern=r"^0x[0-9a-fA-F]{64}$")] = None,
) -> dict[str, Any]:
    configured = runtime(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    coordinator = configured.configuration.coordinator
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        expected = Network(session, tenant, project_id, principal).arkiv_evidence_intent(
            task_id, coordinator
        )
    kind = expected["kind"]
    attributes = expected["attributes"]
    reference = attributes["artifactDigest" if kind == "result" else "resultDigest"]
    with network_errors():
        page = configured.gateway.public_v1_evidence(kind, reference, expected["owner"], cursor)
    with network_errors(), TenantUnitOfWork(db, tenant) as session:
        current = Network(session, tenant, project_id, principal).arkiv_evidence_intent(
            task_id, coordinator
        )
        if current != expected:
            raise ValueError("Accepted evidence changed during the Arkiv lookup")
    matching = [
        row
        for row in page["records"]
        if row["status"] == "observed"
        and row["attributes"] == attributes
        and row["owner"].lower() == expected["owner"].lower()
    ]
    return {
        "kind": kind,
        "expected_digest": attributes["artifactDigest" if kind == "result" else "reviewDigest"],
        "index_block": page["index_block"],
        "observed_block": page["observed_block"],
        "index_lag_blocks": page["index_lag_blocks"],
        "next_cursor": page["next_cursor"],
        "records": matching,
        "institution_verified": False,
        "scientific_result_verified": False,
    }

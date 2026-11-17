"""Purpose: expose P05 investigation draft, plan and exact-digest approval APIs.
Behavior: resolve the tenant server-side, enforce project scopes and return explicit plan readiness.
Refs: main_implementation plan.md P05.1/P08.1; api design.md §§3.6, 5.1; Phase: P05/P08.
"""

import hashlib
import json
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Annotated, Literal, NoReturn, cast
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from jwt import InvalidTokenError
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.projects import engine, resolve_project
from orca.application.investigations import Investigations
from orca.application.planning import compile_typed_plan
from orca.application.projects import IdempotencyConflict, PreconditionRequired, StaleVersion
from orca.contracts.common import Operation, Page, ResourceRef
from orca.contracts.investigations import (
    ApprovedPlanView,
    ApprovePlan,
    CreateInvestigation,
    InvestigationPlanView,
    InvestigationView,
    PatchInvestigation,
    RunOutcome,
    RunView,
    StartRun,
)
from orca.contracts.research_plans import PlanRequest, TypedPlan
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.events.postgres_outbox import PostgresRunEvents
from orca.infrastructure.jobs.postgres_queue import EnqueuedRun, PostgresQueue, RunRateLimitExceeded
from orca.science.manifests import verify_manifest

router = APIRouter(prefix="/v1/research/investigations", tags=["investigations"])

# P05 has no organization spending policy yet. This deliberately conservative
# application ceiling bounds a plan request; P05.2 replaces it with atomic
# organization budget reservations before any plan can be run.
P05_PLAN_MAX_BUDGET = Decimal("100.00")
InvestigationState = Literal["DRAFT", "PLAN_PENDING", "PLAN_READY", "APPROVED"]


def investigation_context(db: Engine, principal: Principal, identifier: UUID) -> tuple[UUID, UUID]:
    """Resolve a tenant through the SECURITY DEFINER function, never a client header."""
    with db.connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_investigation_tenant(:subject,:investigation)"),
            {"subject": principal.subject, "investigation": identifier},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Investigation not found")
    resolved_tenant = cast(UUID, tenant)
    with TenantUnitOfWork(db, resolved_tenant) as session:
        project = session.execute(
            text(
                "SELECT project_id FROM research.investigations WHERE tenant_id=:tenant AND id=:id"
            ),
            {"tenant": resolved_tenant, "id": identifier},
        ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Investigation not found")
    return resolved_tenant, cast(UUID, project)


def raise_service_error(error: Exception) -> NoReturn:
    if isinstance(error, RunRateLimitExceeded):
        raise HTTPException(
            429, "Organization research run rate limit reached", headers={"Retry-After": "60"}
        ) from error
    if isinstance(error, LookupError):
        raise HTTPException(404, str(error)) from error
    if isinstance(error, PermissionError):
        raise HTTPException(403, "Investigation action not permitted") from error
    if isinstance(error, PreconditionRequired):
        raise HTTPException(428, "If-Match is required") from error
    if isinstance(error, StaleVersion):
        raise HTTPException(412, "Investigation plan version is stale") from error
    if isinstance(error, IdempotencyConflict):
        raise HTTPException(409, "Idempotency key was used with another request") from error
    if isinstance(error, IntegrityError):
        raise HTTPException(409, "Investigation conflicts with an existing resource") from error
    if isinstance(error, ValueError):
        status = 422 if "budget" in str(error).lower() else 409
        raise HTTPException(status, str(error)) from error
    raise error


@router.get("", response_model=Page[InvestigationView])
def list_investigations(
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    state: InvestigationState | None = None,
) -> Page[InvestigationView]:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            return Investigations(session, tenant, project_id, principal).list_investigations(
                limit=limit, cursor=cursor, state=state
            )
    except Exception as error:
        raise_service_error(error)


@router.post("", response_model=InvestigationView, status_code=201)
def create_investigation(
    body: CreateInvestigation,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> InvestigationView:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, body.project_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, body.project_id, principal).create(
                body, idempotency_key
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["Location"] = f"/v1/research/investigations/{result.id}"
    response.headers["ETag"] = result.etag
    return result


@router.get("/{investigation_id}", response_model=InvestigationView)
def get_investigation(
    investigation_id: UUID, request: Request, response: Response, principal: CurrentPrincipal
) -> InvestigationView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).get(investigation_id)
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = result.etag
    return result


@router.patch("/{investigation_id}", response_model=InvestigationView)
def patch_investigation(
    investigation_id: UUID,
    body: PatchInvestigation,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    if_match: Annotated[str | None, Header()] = None,
) -> InvestigationView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).patch(
                investigation_id, body, if_match
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = result.etag
    return result


@router.post("/{investigation_id}/plans", response_model=Operation, status_code=202)
def create_plan(
    investigation_id: UUID,
    body: PlanRequest,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> Operation:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            service = Investigations(session, tenant, project, principal)
            investigation = service.get(investigation_id)
            candidate = compile_typed_plan(body, revision=investigation.current_plan_revision + 1)
            result = service.create_plan(
                investigation_id,
                candidate,
                idempotency_key,
                maximum_budget=P05_PLAN_MAX_BUDGET,
            )
    except Exception as error:
        raise_service_error(error)
    return Operation(
        id=result.id,
        kind="plan",
        status="succeeded",
        resource_ref=ResourceRef(
            type="investigation_plan",
            id=str(result.id),
            version=str(result.revision),
            digest=result.plan_digest,
        ),
        created_at=result.created_at,
        updated_at=result.created_at,
        progress=1.0,
        links={"plan": f"/v1/research/investigations/{investigation_id}/plans/{result.id}"},
    )


@router.get("/{investigation_id}/plans", response_model=Page[InvestigationPlanView])
def list_plans(
    investigation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[int | None, Query(ge=0)] = None,
) -> Page[InvestigationPlanView]:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            plans = Investigations(session, tenant, project, principal).plans(investigation_id)
    except Exception as error:
        raise_service_error(error)
    after = cursor or 0
    candidates = [plan for plan in plans if plan.revision > after]
    items = candidates[:limit]
    return Page(
        items=items,
        page_size=limit,
        next_cursor=str(items[-1].revision) if len(candidates) > limit and items else None,
    )


@router.get("/{investigation_id}/plans/{plan_id}", response_model=InvestigationPlanView)
def get_plan(
    investigation_id: UUID,
    plan_id: UUID,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
) -> InvestigationPlanView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).get_plan(
                investigation_id, plan_id
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = result.etag
    return result


@router.post("/{investigation_id}/plans/{plan_id}/approve", response_model=ApprovedPlanView)
def approve_plan(
    investigation_id: UUID,
    plan_id: UUID,
    body: ApprovePlan,
    request: Request,
    response: Response,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
    if_match: Annotated[str | None, Header()] = None,
) -> ApprovedPlanView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            result = Investigations(session, tenant, project, principal).approve_plan(
                investigation_id, plan_id, body, if_match, idempotency_key
            )
    except Exception as error:
        raise_service_error(error)
    response.headers["ETag"] = if_match or ""
    return result


@router.post("/{investigation_id}/runs", response_model=Operation, status_code=202)
def start_run(
    investigation_id: UUID,
    body: StartRun,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> Operation:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            Investigations(session, tenant, project, principal)._authorize("research:run")
            row = (
                session.execute(
                    text("""SELECT p.plan_digest,(p.plan->'budget'->>'max_amount') AS limit_amount
              FROM research.investigation_plans p JOIN research.plan_approvals a ON a.tenant_id=p.tenant_id AND a.project_id=p.project_id AND a.plan_id=p.id
              WHERE p.tenant_id=:tenant AND p.project_id=:project AND p.investigation_id=:investigation AND p.id=:plan"""),
                    {
                        "tenant": tenant,
                        "project": project,
                        "investigation": investigation_id,
                        "plan": body.plan_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if row is None or row["plan_digest"] != body.digest:
                raise ValueError("exact approved plan digest is required before enqueue")
            receipt = PostgresQueue(session, tenant, project).enqueue_run(
                EnqueuedRun(
                    investigation_id,
                    body.plan_id,
                    body.digest,
                    Decimal(str(row["limit_amount"])),
                    Decimal(body.execution_budget),
                    "sha256:" + hashlib.sha256(body.model_dump_json().encode("utf-8")).hexdigest(),
                    idempotency_key,
                    principal.subject,
                    {"kind": "investigation_run", "node_count": 4, "tool_budget": 0},
                )
            )
            created = (
                session.execute(
                    text(
                        "SELECT created_at,updated_at FROM research.compute_runs WHERE tenant_id=:tenant AND id=:id"
                    ),
                    {"tenant": tenant, "id": receipt.run_id},
                )
                .mappings()
                .one()
            )
    except Exception as error:
        raise_service_error(error)
    return Operation(
        id=receipt.job_id,
        kind="research_run",
        status="queued",
        resource_ref=ResourceRef(
            type="research_run", id=str(receipt.run_id), version="1", digest=body.digest
        ),
        created_at=created["created_at"],
        updated_at=created["updated_at"],
        progress=0,
        links={"run": f"/v1/research/investigations/{investigation_id}/runs/{receipt.run_id}"},
    )


@router.get("/{investigation_id}/runs", response_model=Page[RunView])
def list_runs(
    investigation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> Page[RunView]:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        Investigations(session, tenant, project, principal)._authorize("project:read")
        rows = session.execute(
            text(
                "SELECT id,investigation_id,plan_id,state,stage,execution_budget::text AS execution_budget,created_at,updated_at FROM research.compute_runs WHERE tenant_id=:tenant AND project_id=:project AND investigation_id=:investigation ORDER BY created_at DESC LIMIT :limit"
            ),
            {
                "tenant": tenant,
                "project": project,
                "investigation": investigation_id,
                "limit": limit,
            },
        ).mappings()
        items = [RunView.model_validate(dict(row)) for row in rows]
    return Page(items=items, page_size=limit, next_cursor=None)


@router.post("/{investigation_id}/runs/{run_id}/cancel", response_model=Operation, status_code=202)
def cancel_run(
    investigation_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> Operation:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        Investigations(session, tenant, project, principal)._authorize("research:run")
        row = (
            session.execute(
                text(
                    "SELECT q.id,r.created_at,r.updated_at FROM research.compute_runs r JOIN research.job_queue q ON q.tenant_id=r.tenant_id AND q.run_id=r.id WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.investigation_id=:investigation AND r.id=:run"
                ),
                {
                    "tenant": tenant,
                    "project": project,
                    "investigation": investigation_id,
                    "run": run_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, "Run not found")
        changed = PostgresQueue(session, tenant, project).cancel(row["id"])
    return Operation(
        id=row["id"],
        kind="research_run",
        status="cancelled" if changed else "failed",
        resource_ref=ResourceRef(type="research_run", id=str(run_id), version="1"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        progress=None,
        links={},
    )


@router.post("/{investigation_id}/runs/{run_id}/retry", response_model=Operation, status_code=202)
def retry_run(
    investigation_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> Operation:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        Investigations(session, tenant, project, principal)._authorize("research:run")
        row = (
            session.execute(
                text(
                    "SELECT q.id,r.created_at,r.updated_at FROM research.compute_runs r JOIN research.job_queue q ON q.tenant_id=r.tenant_id AND q.run_id=r.id WHERE r.tenant_id=:tenant AND r.project_id=:project AND r.investigation_id=:investigation AND r.id=:run"
                ),
                {
                    "tenant": tenant,
                    "project": project,
                    "investigation": investigation_id,
                    "run": run_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(404, "Run not found")
        if not PostgresQueue(session, tenant, project).retry(row["id"]):
            raise HTTPException(409, "Only completed inconclusive or failed runs can be retried")
    return Operation(
        id=row["id"],
        kind="research_run",
        status="queued",
        resource_ref=ResourceRef(type="research_run", id=str(run_id), version="1"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        progress=0,
        links={},
    )


@router.get("/{investigation_id}/runs/{run_id}", response_model=RunView)
def get_run(
    investigation_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> RunView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        Investigations(session, tenant, project, principal)._authorize("project:read")
        row = (
            session.execute(
                text(
                    "SELECT id,investigation_id,plan_id,state,stage,execution_budget::text AS execution_budget,created_at,updated_at FROM research.compute_runs WHERE tenant_id=:tenant AND project_id=:project AND investigation_id=:investigation AND id=:run"
                ),
                {
                    "tenant": tenant,
                    "project": project,
                    "investigation": investigation_id,
                    "run": run_id,
                },
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise HTTPException(404, "Run not found")
    return RunView.model_validate(dict(row))


@router.get("/{investigation_id}/runs/{run_id}/events")
def run_events(
    investigation_id: UUID,
    run_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    after: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        Investigations(session, tenant, project, principal)._authorize("project:read")
        exists = session.execute(
            text("""SELECT 1 FROM research.compute_runs WHERE tenant_id=:tenant
              AND project_id=:project AND investigation_id=:investigation AND id=:run"""),
            {
                "tenant": tenant,
                "project": project,
                "investigation": investigation_id,
                "run": run_id,
            },
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(404, "Run not found")
        try:
            events = PostgresRunEvents(session, tenant, project).replay(run_id, after)
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
    return {
        "events": [
            {
                "id": str(event.id),
                "sequence": event.sequence,
                "type": event.event_type,
                "data": event.payload,
            }
            for event in events
        ]
    }


@router.get("/{investigation_id}/runs/{run_id}/events/stream")
def stream_run_events(
    investigation_id: UUID,
    run_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    last_event_id: Annotated[int | None, Header(ge=0)] = None,
) -> StreamingResponse:
    # Authenticate and authorize before sending response headers. Rechecking
    # inside the stream closes it if the token expires or membership is revoked.
    run_events(investigation_id, run_id, request, principal, last_event_id or 0)
    bearer = request.headers.get("authorization", "")
    token = bearer[7:] if bearer.lower().startswith("bearer ") else ""

    def body() -> Iterator[str]:
        cursor = last_event_id or 0
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            verifier = getattr(request.app.state, "oidc_verifier", None)
            try:
                if verifier is None or not token:
                    return
                refreshed = verifier.verify(token)
                if refreshed.subject != principal.subject:
                    return
                payload = run_events(investigation_id, run_id, request, refreshed, cursor)
            except (HTTPException, InvalidTokenError, PermissionError, ValueError):
                return
            for event in cast(list[dict[str, object]], payload["events"]):
                cursor = int(cast(int, event["sequence"]))
                yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event['data'], sort_keys=True)}\n\n"
            yield ": heartbeat\n\n"
            time.sleep(2)

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{investigation_id}/runs/{run_id}/outcome", response_model=RunOutcome)
def run_outcome(
    investigation_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> RunOutcome:
    run = get_run(investigation_id, run_id, request, principal)
    if run.state in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}:
        raise HTTPException(409, "Run has not completed")
    if run.state in {"SUCCEEDED_SUPPORTED", "SUCCEEDED_INSUFFICIENT"}:
        db = engine(request)
        tenant, project = investigation_context(db, principal, investigation_id)
        with TenantUnitOfWork(db, tenant) as session:
            Investigations(session, tenant, project, principal)._authorize("evidence:read")
            row = (
                session.execute(
                    text("""SELECT a.id,a.evidence_manifest_id,a.scientific_status,
                  m.manifest,m.manifest_digest FROM research.run_artifacts a
                  JOIN research.science_evidence_manifests m ON m.tenant_id=a.tenant_id
                    AND m.project_id=a.project_id AND m.id=a.evidence_manifest_id
                  WHERE a.tenant_id=:tenant AND a.project_id=:project AND a.run_id=:run"""),
                    {"tenant": tenant, "project": project, "run": run_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise HTTPException(409, "Completed run has no immutable evidence artifact")
        manifest = ScienceEvidenceManifest.model_validate(row["manifest"])
        verify_manifest(manifest)
        if (
            manifest.id != row["evidence_manifest_id"]
            or manifest.manifest_digest != row["manifest_digest"]
        ):
            raise HTTPException(409, "Run evidence manifest is inconsistent")
        expected = "supported" if run.state == "SUCCEEDED_SUPPORTED" else "insufficient_evidence"
        if manifest.result_status != expected or row["scientific_status"] != expected:
            raise HTTPException(409, "Run outcome differs from immutable evidence")
        return RunOutcome(
            status=expected,
            artifact_ids=[row["id"]],
            limitations=[]
            if expected == "supported"
            else ["The immutable evidence manifest records insufficient scientific support."],
            evidence_manifest_id=manifest.id,
        )
    return RunOutcome(
        status="insufficient_evidence" if run.state == "SUCCEEDED_INSUFFICIENT" else "inconclusive",
        artifact_ids=[],
        limitations=[
            f"Run ended at durable stage: {run.stage}. No immutable evidence artifact is attached."
        ],
        evidence_manifest_id=None,
    )


@router.get("/{investigation_id}/runs/{run_id}/task-graph")
def run_task_graph(
    investigation_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> dict[str, object]:
    run = get_run(investigation_id, run_id, request, principal)
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        Investigations(session, tenant, project, principal)._authorize("project:read")
        plan_row = session.execute(
            text("""SELECT p.plan FROM research.investigation_plans p
              WHERE p.tenant_id=:tenant AND p.project_id=:project AND p.id=:plan"""),
            {"tenant": tenant, "project": project, "plan": run.plan_id},
        ).scalar_one_or_none()
        if plan_row is None:
            raise HTTPException(409, "Approved plan is missing from run history")
        completed = {
            row["stage"]: row["output_digest"]
            for row in session.execute(
                text("""SELECT stage,output_digest FROM research.run_stage_checkpoints
                  WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run"""),
                {"tenant": tenant, "project": project, "run": run_id},
            ).mappings()
        }
    plan = TypedPlan.model_validate(plan_row)
    terminal = run.state not in {"QUEUED", "RUNNING", "CANCEL_REQUESTED"}
    return {
        "run_id": str(run.id),
        "nodes": [
            {
                "id": node.node_id,
                "operation": node.operation,
                "depends_on": node.depends_on,
                "state": "completed"
                if node.node_id in completed
                else "blocked"
                if terminal
                else "pending",
                "output_digest": completed.get(node.node_id),
            }
            for node in plan.nodes
        ],
    }


@router.get("/{investigation_id}/runs/{run_id}/costs")
def run_costs(
    investigation_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> dict[str, object]:
    run = get_run(investigation_id, run_id, request, principal)
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        Investigations(session, tenant, project, principal)._authorize("project:read")
        reservation = (
            session.execute(
                text("""SELECT state,amount FROM research.run_budget_reservations
              WHERE tenant_id=:tenant AND project_id=:project AND run_id=:run"""),
                {"tenant": tenant, "project": project, "run": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if reservation is None:
            raise HTTPException(409, "Run budget reservation is missing")
    return {
        "run_id": str(run.id),
        "budget_cap_usd": run.execution_budget,
        "reserved_usd": str(reservation["amount"]) if reservation["state"] == "reserved" else "0",
        "measured_usd": None,
        "status": "unknown",
        "reason": "P05 does not fabricate compute/source costs before metering is committed.",
    }

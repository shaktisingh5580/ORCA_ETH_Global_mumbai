"""Purpose: expose the hackathon's real question, plan, approval, run and report flow.
Behavior: retain verified project authority and serve only configured, clearly bounded capabilities.
Refs: HACKATHON_IMPLEMENTATION_PLAN.md H02/H03; FRONTEND_BACKEND_HANDOFF.md; Phase: H02/H03.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, cast
from uuid import UUID

import jwt
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.organizations import errors
from orca.api.routers.projects import engine, resolve_project
from orca.application.prototype import Prototype, guided_plan
from orca.application.workspace_access import authorize_project
from orca.config import Settings
from orca.contracts.prototype import (
    ApprovePlan,
    CreateInvestigation,
    CreateRun,
    Investigation,
    PrototypeConfig,
    PrototypeRun,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.infrastructure.gemini import explain_plan

router = APIRouter(prefix="/v1/prototype", tags=["hackathon research workflow"])


def settings(request: Request) -> Settings:
    configured = getattr(request.app.state, "prototype_settings", None)
    if configured is None:
        raise HTTPException(503, "Hackathon prototype is not configured")
    return cast(Settings, configured)


@contextmanager
def workflow_errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except ValueError as error:
            raise HTTPException(409, str(error)) from error


@router.get("/config", response_model=PrototypeConfig)
def public_config(request: Request) -> dict[str, Any]:
    configured = settings(request)
    anon = None
    if configured.auth_mode == "supabase" and configured.supabase_anon_key:
        value = configured.supabase_anon_key.get_secret_value()
        try:
            claims = jwt.decode(value, options={"verify_signature": False})
            if claims.get("role") == "anon":
                anon = value
        except jwt.InvalidTokenError:
            pass
    return {
        "profile": "hackathon-prototype",
        "source_mode": "real",
        "supabase_url": configured.oidc_issuer.removesuffix("/auth/v1")
        if configured.oidc_issuer
        else None,
        "supabase_anon_key": anon,
        "gemini_configured": bool(configured.gemini_api_key and configured.gemini_model),
        "study": "January 2025 · North Arabian Sea · 68.45–68.55°E, 20.45–20.55°N",
        "baseline": "January 2003–2022 · 20 years",
        "showcase_available": bool(configured.prototype_showcase_run),
    }


@router.post("/investigations", response_model=Investigation, status_code=201)
def create_investigation(
    body: CreateInvestigation, request: Request, principal: CurrentPrincipal
) -> Investigation:
    configured = settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, body.project_id)
    if tenant != configured.prototype_source_tenant:
        raise HTTPException(422, "No live input bundle is configured for this workspace")
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        authorize_project(session, tenant, body.project_id, principal, "project:write")
    summary = None
    if configured.gemini_api_key and configured.gemini_model:
        summary = explain_plan(
            configured.gemini_api_key.get_secret_value(), configured.gemini_model, body.question
        )
    plan = guided_plan(cast(str, configured.prototype_bundle_digest), summary)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        return Prototype(session, tenant, body.project_id, principal).create(body, plan)


@router.get("/investigations", response_model=list[Investigation])
def list_investigations(
    project_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[Investigation]:
    settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        return Prototype(session, tenant, project_id, principal).investigations()


@router.post("/investigations/{investigation_id}/approve", response_model=Investigation)
def approve(
    investigation_id: UUID,
    body: ApprovePlan,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
) -> Investigation:
    settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        return Prototype(session, tenant, project_id, principal).approve(investigation_id)


@router.get("/investigations/{investigation_id}", response_model=Investigation)
def read_investigation(
    investigation_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal
) -> Investigation:
    settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        return Prototype(session, tenant, project_id, principal).read_investigation(
            investigation_id
        )


@router.post("/runs", response_model=PrototypeRun, status_code=202)
def submit_run(
    body: CreateRun,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> PrototypeRun:
    settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, body.project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        return Prototype(session, tenant, body.project_id, principal).submit(
            body.investigation_id, idempotency_key
        )


@router.get("/runs/{run_id}", response_model=PrototypeRun)
def read_run(
    run_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal
) -> PrototypeRun:
    settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        return Prototype(session, tenant, project_id, principal).run(run_id)


@router.get("/runs", response_model=list[PrototypeRun])
def list_runs(
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    investigation_id: UUID | None = None,
) -> list[PrototypeRun]:
    settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        service = Prototype(session, tenant, project_id, principal)
        service.authorize()
        identifiers: list[UUID] = list(
            session.execute(
                text(
                    "SELECT id FROM research.prototype_runs WHERE tenant_id=:t AND project_id=:p "
                    "AND (CAST(:inv AS uuid) IS NULL OR investigation_id=CAST(:inv AS uuid)) "
                    "ORDER BY created_at DESC LIMIT 30"
                ),
                {"t": tenant, "p": project_id, "inv": investigation_id},
            ).scalars()
        )
        return [service.run(identifier) for identifier in identifiers]


@router.get("/runs/{run_id}/report", response_class=PlainTextResponse)
def download_report(
    run_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal
) -> PlainTextResponse:
    settings(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with workflow_errors(), TenantUnitOfWork(db, tenant) as session:
        row = Prototype(session, tenant, project_id, principal).run_row(run_id)
        if row["status"] != "succeeded":
            raise HTTPException(409, "Report is available after successful execution")
        return PlainTextResponse(
            row["report"],
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="orca-report-{run_id}.md"'},
        )

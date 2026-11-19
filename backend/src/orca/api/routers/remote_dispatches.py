"""Purpose: expose P09 scoped remote research intents beneath approved runs.
Behavior: authorize each read/write, fence cancellation and label remote outcomes accurately.
Refs: main_implementation plan.md P09.3; api design.md §§3.6,17.1; Phase: P09.
"""

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.investigations import investigation_context, raise_service_error
from orca.api.routers.projects import engine
from orca.application.remote_dispatches import RemoteDispatches
from orca.contracts.remote_dispatch import (
    CreateRemoteDispatch,
    RemoteDispatchHistory,
    RemoteDispatchView,
)
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/research/investigations", tags=["remote-dispatches"])


def _run_in_investigation(
    session: Session, tenant: UUID, project: UUID, investigation: UUID, run: UUID
) -> None:
    exists = session.execute(
        text("""SELECT 1 FROM research.compute_runs WHERE tenant_id=:tenant AND project_id=:project
          AND investigation_id=:investigation AND id=:run"""),
        {"tenant": tenant, "project": project, "investigation": investigation, "run": run},
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(404, "Run not found")


def _view(row: dict[str, object]) -> RemoteDispatchView:
    return RemoteDispatchView.model_validate(row)


@router.post(
    "/{investigation_id}/runs/{run_id}/remote-dispatches",
    response_model=RemoteDispatchView,
    status_code=202,
)
def create_remote_dispatch(
    investigation_id: UUID,
    run_id: UUID,
    body: CreateRemoteDispatch,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> RemoteDispatchView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            _run_in_investigation(session, tenant, project, investigation_id, run_id)
            service = RemoteDispatches(session, tenant, project, principal)
            receipt = service.enqueue(
                run_id,
                body.agent_id,
                body.capability_id,
                body.method_id,
                Decimal(body.budget_usd),
                idempotency_key,
                endpoint=body.endpoint,
                fallback_from=body.fallback_from,
            )
            row = service.get(run_id, receipt.id)
    except HTTPException:
        raise
    except Exception as error:
        raise_service_error(error)
    return _view(row)


@router.get(
    "/{investigation_id}/runs/{run_id}/remote-dispatches/{dispatch_id}",
    response_model=RemoteDispatchView,
)
def get_remote_dispatch(
    investigation_id: UUID,
    run_id: UUID,
    dispatch_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
) -> RemoteDispatchView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            _run_in_investigation(session, tenant, project, investigation_id, run_id)
            row = RemoteDispatches(session, tenant, project, principal).get(run_id, dispatch_id)
    except HTTPException:
        raise
    except Exception as error:
        raise_service_error(error)
    return _view(row)


@router.post(
    "/{investigation_id}/runs/{run_id}/remote-dispatches/{dispatch_id}/cancel",
    response_model=RemoteDispatchView,
)
def cancel_remote_dispatch(
    investigation_id: UUID,
    run_id: UUID,
    dispatch_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
) -> RemoteDispatchView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            _run_in_investigation(session, tenant, project, investigation_id, run_id)
            service = RemoteDispatches(session, tenant, project, principal)
            service.cancel(run_id, dispatch_id)
            row = service.get(run_id, dispatch_id)
    except HTTPException:
        raise
    except Exception as error:
        raise_service_error(error)
    return _view(row)


@router.get(
    "/{investigation_id}/runs/{run_id}/remote-dispatches/{dispatch_id}/events",
    response_model=RemoteDispatchHistory,
)
def get_remote_dispatch_history(
    investigation_id: UUID,
    run_id: UUID,
    dispatch_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    after: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RemoteDispatchHistory:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    try:
        with TenantUnitOfWork(db, tenant) as session:
            _run_in_investigation(session, tenant, project, investigation_id, run_id)
            return RemoteDispatches(session, tenant, project, principal).history(
                run_id, dispatch_id, after, limit
            )
    except HTTPException:
        raise
    except Exception as error:
        raise_service_error(error)

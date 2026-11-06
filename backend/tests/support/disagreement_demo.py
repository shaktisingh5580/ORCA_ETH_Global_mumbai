"""Purpose: expose authenticated intentional-data-bias demonstration controls to the existing UI.
Behavior: keep synthetic stages separate from real Supabase research and require explicit correction.
Refs: H05 demonstration decision; DISAGREEMENT_FRONTEND_HANDOFF.md; Phase: H05; tooling only.
"""

from pathlib import Path
from typing import Annotated
from uuid import UUID

from disagreement_store import DemoStore
from fastapi import APIRouter, Header, Request
from pydantic import Field

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.network import network_errors
from orca.api.routers.projects import engine, resolve_project
from orca.application.workspace_access import authorize_project
from orca.contracts.common import Contract
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(
    prefix="/v1/demo/disagreements", tags=["intentional synthetic-data disagreement demonstration"]
)
Idempotency = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


class StartDemo(Contract):
    project_id: UUID
    approve_public_reference: bool = False


class CorrectDemo(Contract):
    expected_version: int = Field(ge=1)


def store(request: Request) -> DemoStore:
    return DemoStore(Path(request.app.state.disagreement_state))


def permission(
    request: Request, principal: CurrentPrincipal, project: UUID, write: bool = False
) -> UUID:
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project)
    with TenantUnitOfWork(db, tenant) as session:
        authorize_project(
            session, tenant, project, principal, "project:write" if write else "project:read"
        )
    return tenant


@router.post("", status_code=202)
def create(
    body: StartDemo, request: Request, principal: CurrentPrincipal, idempotency_key: Idempotency
):
    with network_errors():
        tenant = permission(request, principal, body.project_id, True)
        return store(request).create(
            str(tenant),
            str(body.project_id),
            principal.subject,
            idempotency_key,
            body.approve_public_reference,
        )


@router.get("")
def listing(project_id: UUID, request: Request, principal: CurrentPrincipal):
    with network_errors():
        tenant = permission(request, principal, project_id)
        local = store(request)
        with local.connect() as db:
            identifiers = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM runs WHERE tenant=? AND project=? ORDER BY created_at DESC LIMIT 30",
                    (str(tenant), str(project_id)),
                )
            ]
        return [local.read(identifier, str(tenant), str(project_id)) for identifier in identifiers]


@router.get("/{run_id}")
def read(run_id: UUID, project_id: UUID, request: Request, principal: CurrentPrincipal):
    with network_errors():
        tenant = permission(request, principal, project_id)
        return store(request).read(str(run_id), str(tenant), str(project_id))


@router.post("/{run_id}/correct", status_code=202)
def correct(
    run_id: UUID,
    body: CorrectDemo,
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Idempotency,
):
    with network_errors():
        tenant = permission(request, principal, project_id, True)
        return store(request).correct(
            str(run_id), str(tenant), str(project_id), body.expected_version, idempotency_key
        )

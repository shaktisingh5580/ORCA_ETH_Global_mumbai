"""Purpose: expose durable AI research planning, timeline and explicit approval.
Behavior: use existing project authentication and return real persisted workflow state.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02/A05; Phase: A02.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.normalization import service
from orca.application.reasoning import ResearchReasoning
from orca.contracts.reasoning import ReasoningApproval, ReasoningRequest

router = APIRouter(prefix="/v1/projects/{project_id}/reasoning", tags=["AI research"])


@router.post("", status_code=202)
def create(
    project_id: UUID,
    body: ReasoningRequest,
    request: Request,
    principal: CurrentPrincipal,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> dict[str, Any]:
    with service(request, project_id, principal) as base:
        return ResearchReasoning(base.session, base.tenant, base.project, principal).create_run(
            body, idempotency_key
        )


@router.get("")
def listing(
    project_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[dict[str, Any]]:
    with service(request, project_id, principal) as base:
        return ResearchReasoning(base.session, base.tenant, base.project, principal).runs()


@router.get("/{run_id}")
def read(
    project_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> dict[str, Any]:
    with service(request, project_id, principal) as base:
        app = ResearchReasoning(base.session, base.tenant, base.project, principal)
        return app.run_view(app.run_row(run_id))


@router.get("/{run_id}/events")
def events(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    after: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    with service(request, project_id, principal) as base:
        return {
            "events": ResearchReasoning(base.session, base.tenant, base.project, principal).events(
                run_id, after
            )
        }


@router.post("/{run_id}/approve")
def approve(
    project_id: UUID,
    run_id: UUID,
    body: ReasoningApproval,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    with service(request, project_id, principal) as base:
        return ResearchReasoning(base.session, base.tenant, base.project, principal).approve_plan(
            run_id, body.plan_digest
        )

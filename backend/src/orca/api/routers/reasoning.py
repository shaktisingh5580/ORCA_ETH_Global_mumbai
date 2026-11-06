"""Purpose: expose durable AI research planning, timeline and explicit approval.
Behavior: use existing project authentication and return real persisted workflow state.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02/A05; Phase: A02.
"""

from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.normalization import service
from orca.application.anchors import ResearchAnchors
from orca.application.reasoning import ResearchReasoning
from orca.application.review import EvidenceReview
from orca.contracts.reasoning import ReasoningApproval, ReasoningRequest
from orca.contracts.review import CandidateResult, CorrectionApproval

router = APIRouter(prefix="/v1/projects/{project_id}/reasoning", tags=["AI research"])


class PublicationConsent(BaseModel):
    approve_public_reference: Literal[True]


@router.post("/{run_id}/anchors", status_code=202)
def publish_evidence(
    project_id: UUID,
    run_id: UUID,
    body: PublicationConsent,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    if not hasattr(request.app.state, "network_runtime"):
        raise HTTPException(503, "Arkiv publication is not configured")
    with service(request, project_id, principal) as base:
        return ResearchAnchors(base.session, base.tenant, base.project, principal).publish(run_id)


@router.get("/{run_id}/anchors")
def evidence_anchors(
    project_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[dict[str, Any]]:
    with service(request, project_id, principal) as base:
        return ResearchAnchors(base.session, base.tenant, base.project, principal).anchors(run_id)


@router.post("/{run_id}/reviews", status_code=202)
def submit_review(
    project_id: UUID,
    run_id: UUID,
    body: CandidateResult,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    with service(request, project_id, principal) as base:
        return EvidenceReview(base.session, base.tenant, base.project, principal).submit_review(
            run_id, body
        )


@router.get("/{run_id}/reviews")
def list_reviews(
    project_id: UUID, run_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[dict[str, Any]]:
    with service(request, project_id, principal) as base:
        return EvidenceReview(base.session, base.tenant, base.project, principal).reviews(run_id)


@router.post("/{run_id}/reviews/{review_id}/correct", status_code=202)
def correct_review(
    project_id: UUID,
    run_id: UUID,
    review_id: UUID,
    body: CorrectionApproval,
    request: Request,
    principal: CurrentPrincipal,
) -> dict[str, Any]:
    with service(request, project_id, principal) as base:
        app = EvidenceReview(base.session, base.tenant, base.project, principal)
        if app.review_row(review_id)["run_id"] != run_id:
            raise ValueError("Review belongs to another investigation")
        return app.correct(review_id, body.candidate_digest)


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

"""Purpose: expose a private, evidence-grounded companion reply.
Behavior: authorize within the conversation's tenant before and after the one optional model call.
Refs: main implementation plan P12.2; A-API §§3.13,17.
"""

from functools import partial
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from pydantic import SecretStr

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.companion import service
from orca.api.routers.evidence import _artifact_row, _authorize, _verified
from orca.application import companion_reply_history as history
from orca.application.companion import Conversations
from orca.application.companion_model_limits import ModelRateLimited, reserve
from orca.application.companion_reply import (
    Citation,
    Reply,
    ReplyRequest,
    answer,
    unavailable_evidence,
)
from orca.application.companion_reply_jobs import Job, JobConflict, Jobs
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.domain.identity import Principal
from orca.infrastructure.gemini_companion import classify

router = APIRouter(prefix="/v1/conversations", tags=["companion"])


def authorized_manifest(
    request: Request,
    principal: Principal,
    organization_id: UUID,
    conversation_id: UUID,
    artifact_id: UUID | None,
    *,
    limits: tuple[int, int] | None = None,
) -> ScienceEvidenceManifest | None:
    with service(request, principal, organization_id) as conversations:
        if conversations.get(conversation_id).archived:
            raise HTTPException(409, "Conversation is archived")
        manifest = accessible_manifest(conversations, artifact_id)
        if limits is not None and manifest is not None and manifest.result_status == "supported":
            reserve(conversations, user_limit=limits[0], organization_limit=limits[1])
        return manifest


def accessible_manifest(
    conversations: Conversations,
    artifact_id: UUID | None,
) -> ScienceEvidenceManifest | None:
    if artifact_id is None:
        return None
    try:
        row = _artifact_row(conversations.session, conversations.tenant, artifact_id)
        _authorize(
            conversations.session, conversations.tenant, row["project_id"], conversations.principal
        )
        manifest, _ = _verified(row)
    except HTTPException as exc:
        if exc.status_code in (403, 404, 409):
            return None
        raise
    except ValueError:
        return None
    if manifest.tenant_id != conversations.tenant or manifest.project_id != row["project_id"]:
        return None
    return manifest


def save_if_authorized(
    request: Request,
    principal: Principal,
    organization_id: UUID,
    conversation_id: UUID,
    result: Reply,
) -> Reply:
    citation = result.citation
    if citation is None:
        return unavailable_evidence()
    with service(request, principal, organization_id) as conversations:
        if conversations.lock(conversation_id).archived:
            raise HTTPException(409, "Conversation is archived")
        current = accessible_manifest(conversations, citation.artifact_id)
        if current is None or current.manifest_digest != citation.manifest_digest:
            return unavailable_evidence()
        identifier = history.save(conversations.session, conversation_id, result)
    return result.model_copy(update={"id": identifier})


@router.post("/{conversation_id}/reply")
def reply(
    conversation_id: UUID,
    body: ReplyRequest,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> Reply:
    """Classify only the explicitly shared question; leave private research data on ORCA."""
    key = getattr(request.app.state, "companion_model_key", None)
    model = getattr(request.app.state, "companion_model_name", None)
    select = (
        partial(classify, key.get_secret_value(), model)
        if (isinstance(key, SecretStr) and isinstance(model, str))
        else None
    )
    try:
        manifest = authorized_manifest(
            request,
            principal,
            organization_id,
            conversation_id,
            body.artifact_id,
            limits=request.app.state.companion_model_limits
            if select is not None and body.share_question_with_model
            else None,
        )
    except ModelRateLimited as exc:
        raise HTTPException(
            429, "Companion model rate limit reached", headers={"Retry-After": "60"}
        ) from exc
    result = answer(body, manifest, select)
    if result.status == "answered":
        return save_if_authorized(request, principal, organization_id, conversation_id, result)
    return result


@router.get("/{conversation_id}/replies")
def list_replies(
    conversation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> history.ReplyHistory:
    with service(request, principal, organization_id) as conversations:
        conversations.get(conversation_id)
        rows = history.page(conversations.session, conversation_id, cursor, limit)
        items: list[Reply] = []
        accessible: dict[UUID, ScienceEvidenceManifest | None] = {}
        # Check each distinct artifact once per page. Advance the cursor even
        # when revocation makes the current page empty.
        for artifact_id in sorted({row.artifact_id for row in rows[:limit]}):
            accessible[artifact_id] = accessible_manifest(conversations, artifact_id)
        for row in rows[:limit]:
            verified = accessible[row.artifact_id]
            if verified is None or verified.manifest_digest != row.manifest_digest:
                continue
            items.append(
                Reply(
                    id=row.id,
                    status="answered",
                    text=row.body,
                    citation=Citation(
                        artifact_id=row.artifact_id,
                        manifest_digest=row.manifest_digest,
                        href=f"/v1/research/artifacts/{row.artifact_id}/manifest",
                    ),
                )
            )
        return history.ReplyHistory(
            items=items, next_cursor=rows[limit - 1].id if len(rows) > limit else None
        )


class LeaseLost(Exception):
    pass


def process_job(
    request: Request,
    principal: Principal,
    organization_id: UUID,
    conversation_id: UUID,
    job_id: UUID,
) -> None:
    with service(request, principal, organization_id) as conversations:
        claim = Jobs(conversations, conversation_id, job_id).claim()
    if claim is None:
        return
    key = getattr(request.app.state, "companion_model_key", None)
    model = getattr(request.app.state, "companion_model_name", None)
    select = (
        partial(classify, key.get_secret_value(), model)
        if (isinstance(key, SecretStr) and isinstance(model, str))
        else None
    )
    try:
        manifest = authorized_manifest(
            request,
            principal,
            organization_id,
            conversation_id,
            claim.body.artifact_id,
            limits=request.app.state.companion_model_limits
            if select is not None and claim.body.share_question_with_model
            else None,
        )
        result: Reply | None = answer(claim.body, manifest, select)
    except ModelRateLimited:
        result = None
    except HTTPException as exc:
        if exc.status_code not in (403, 404, 409):
            raise
        result = unavailable_evidence()
    try:
        with service(request, principal, organization_id) as conversations:
            conversation = conversations.lock(conversation_id)
            if result is not None and result.citation is not None:
                citation = result.citation
                current = (
                    None
                    if conversation.archived
                    else accessible_manifest(
                        conversations,
                        citation.artifact_id,
                    )
                )
                if current is None or current.manifest_digest != citation.manifest_digest:
                    result = unavailable_evidence()
                else:
                    identifier = history.save(conversations.session, conversation_id, result)
                    result = result.model_copy(update={"id": identifier})
            if not Jobs(conversations, conversation_id, job_id).finish(claim.lease, result):
                raise LeaseLost
    except LeaseLost:
        pass


def run_job(
    request: Request,
    principal: Principal,
    organization_id: UUID,
    conversation_id: UUID,
    job_id: UUID,
) -> None:
    # Revocation after enqueue is normal. The durable lease expires if a request
    # loses membership or the process exits before it can publish a result.
    try:
        process_job(request, principal, organization_id, conversation_id, job_id)
    except HTTPException as exc:
        if exc.status_code not in (403, 404):
            raise


@router.post("/{conversation_id}/reply-jobs", status_code=202)
def enqueue_reply(
    conversation_id: UUID,
    body: ReplyRequest,
    request: Request,
    background: BackgroundTasks,
    principal: CurrentPrincipal,
    organization_id: UUID,
    idempotency_key: Annotated[UUID, Header()],
) -> Job:
    with service(request, principal, organization_id) as conversations:
        if conversations.lock(conversation_id).archived:
            raise HTTPException(409, "Conversation is archived")
        try:
            job = Jobs(conversations, conversation_id, idempotency_key).enqueue(body)
        except JobConflict as exc:
            raise HTTPException(409, "Idempotency key belongs to another reply request") from exc
    if job.state == "queued":
        background.add_task(run_job, request, principal, organization_id, conversation_id, job.id)
    return Job(id=job.id, state=job.state)


@router.get("/{conversation_id}/reply-jobs/{job_id}")
def get_reply_job(
    conversation_id: UUID,
    job_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> Job:
    with service(request, principal, organization_id) as conversations:
        conversations.get(conversation_id)
        job = Jobs(conversations, conversation_id, job_id).get()
        if job.result is not None and job.result.citation is not None:
            citation = job.result.citation
            manifest = accessible_manifest(conversations, citation.artifact_id)
            if manifest is None or manifest.manifest_digest != citation.manifest_digest:
                return Job(id=job_id, state="completed", result=unavailable_evidence())
        return job


@router.post("/{conversation_id}/reply-jobs/{job_id}/resume", status_code=202)
def resume_reply_job(
    conversation_id: UUID,
    job_id: UUID,
    request: Request,
    background: BackgroundTasks,
    principal: CurrentPrincipal,
    organization_id: UUID,
) -> Job:
    with service(request, principal, organization_id) as conversations:
        if conversations.get(conversation_id).archived:
            raise HTTPException(409, "Conversation is archived")
        job = Jobs(conversations, conversation_id, job_id).get()
    if job.state == "queued":
        background.add_task(run_job, request, principal, organization_id, conversation_id, job.id)
    return Job(id=job.id, state=job.state)

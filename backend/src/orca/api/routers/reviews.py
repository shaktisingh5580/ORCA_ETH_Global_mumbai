"""Purpose: expose P07 scoped reviewer grants and immutable scientific disagreements.
Behavior: recheck artifact authority and grants per request; never equate a vote with verified science.
Refs: main_implementation plan.md P07.2; api design.md §§3.8, 4.5; Phase: P07.
"""

import json
from datetime import datetime, timezone
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.evidence import _artifact_context, _artifact_row, _verified
from orca.api.routers.projects import engine
from orca.application.review_export import assemble_review_export
from orca.application.workspace_access import authorize_project
from orca.contracts.scientific_reviews import (
    ChallengeRequest,
    ChallengeResolutionView,
    ChallengeResponseRequest,
    ChallengeResponseView,
    ChallengeView,
    IndependentReviewRequest,
    IndependentReviewView,
    ResolveChallengeRequest,
    ReviewGrantRequest,
    ReviewGrantView,
)
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/research", tags=["scientific reviews"])


def _review_tenant(request: Request, principal: Principal, artifact_id: UUID) -> UUID:
    with engine(request).connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_review_artifact_tenant(:subject,:artifact)"),
            {"subject": principal.subject, "artifact": artifact_id},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Artifact not found")
    return cast(UUID, tenant)


def _grant_row(session: Any, tenant: UUID, artifact_id: UUID, subject: str) -> Any:
    return (
        session.execute(
            text("""SELECT id,artifact_id,reviewer_subject,granted_by,expires_at,revoked_at,created_at
          FROM research.artifact_review_grants
          WHERE tenant_id=:tenant AND artifact_id=:artifact AND reviewer_subject=:subject
          FOR SHARE"""),
            {"tenant": tenant, "artifact": artifact_id, "subject": subject},
        )
        .mappings()
        .one_or_none()
    )


def _review_access(
    session: Any,
    tenant: UUID,
    project: UUID,
    artifact_id: UUID,
    principal: Principal,
    *,
    write: bool,
) -> None:
    scope = "review:write" if write else "evidence:read"
    try:
        authorize_project(session, tenant, project, principal, scope)
        return
    except (LookupError, PermissionError):
        pass
    if scope not in principal.token_scopes:
        raise HTTPException(403, "Review scope is required")
    grant = _grant_row(session, tenant, artifact_id, principal.subject)
    if (
        grant is None
        or grant["revoked_at"] is not None
        or grant["expires_at"] <= datetime.now(timezone.utc)
    ):
        raise HTTPException(404, "Review grant not found")


def _original(session: Any, tenant: UUID, artifact_id: UUID) -> tuple[Any, Any]:
    row = _artifact_row(session, tenant, artifact_id)
    manifest, _ = _verified(row)
    return row, manifest


def _require_separate_resolver(
    subject: str, origin_author: str, challenger: str, responders: set[str]
) -> None:
    if subject in {origin_author, challenger} or subject in responders:
        raise HTTPException(403, "A separate resolver is required")


@router.post(
    "/artifacts/{artifact_id}/review-grants", response_model=ReviewGrantView, status_code=201
)
def grant_review(
    artifact_id: UUID,
    body: ReviewGrantRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> ReviewGrantView:
    tenant = _artifact_context(request, principal, artifact_id)
    if body.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(422, "Grant expiry must be in the future")
    with TenantUnitOfWork(engine(request), tenant) as session:
        row, manifest = _original(session, tenant, artifact_id)
        try:
            authorize_project(session, tenant, row["project_id"], principal, "project:share")
        except (LookupError, PermissionError) as error:
            raise HTTPException(403, "Project sharing permission required") from error
        if body.reviewer_subject == manifest.created_by:
            raise HTTPException(422, "The result author cannot receive an independent review grant")
        identifier = uuid4()
        try:
            created = (
                session.execute(
                    text("""INSERT INTO research.artifact_review_grants
                  (id,tenant_id,project_id,artifact_id,reviewer_subject,granted_by,expires_at)
                  VALUES(:id,:tenant,:project,:artifact,:reviewer,:actor,:expires)
                  RETURNING id,artifact_id,reviewer_subject,granted_by,expires_at,revoked_at,created_at"""),
                    {
                        "id": identifier,
                        "tenant": tenant,
                        "project": row["project_id"],
                        "artifact": artifact_id,
                        "reviewer": body.reviewer_subject,
                        "actor": principal.subject,
                        "expires": body.expires_at,
                    },
                )
                .mappings()
                .one()
            )
        except IntegrityError as error:
            raise HTTPException(409, "A grant for this reviewer already exists") from error
        return ReviewGrantView.model_validate(created)


@router.post(
    "/artifacts/{artifact_id}/review-grants/{grant_id}/revoke", response_model=ReviewGrantView
)
def revoke_review(
    artifact_id: UUID,
    grant_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
) -> ReviewGrantView:
    tenant = _artifact_context(request, principal, artifact_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        row, _ = _original(session, tenant, artifact_id)
        try:
            authorize_project(session, tenant, row["project_id"], principal, "project:share")
        except (LookupError, PermissionError) as error:
            raise HTTPException(403, "Project sharing permission required") from error
        changed = (
            session.execute(
                text("""UPDATE research.artifact_review_grants SET revoked_at=clock_timestamp()
              WHERE tenant_id=:tenant AND artifact_id=:artifact AND id=:id AND revoked_at IS NULL
              RETURNING id,artifact_id,reviewer_subject,granted_by,expires_at,revoked_at,created_at"""),
                {"tenant": tenant, "artifact": artifact_id, "id": grant_id},
            )
            .mappings()
            .one_or_none()
        )
        if changed is None:
            raise HTTPException(404, "Active review grant not found")
        return ReviewGrantView.model_validate(changed)


@router.post("/artifacts/{artifact_id}/challenges", response_model=ChallengeView, status_code=201)
def create_challenge(
    artifact_id: UUID,
    body: ChallengeRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> ChallengeView:
    tenant = _review_tenant(request, principal, artifact_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        row, manifest = _original(session, tenant, artifact_id)
        _review_access(session, tenant, row["project_id"], artifact_id, principal, write=True)
        if principal.subject == manifest.created_by:
            raise HTTPException(
                403, "The result author cannot independently challenge their own result"
            )
        allowed = {item.snapshot_id for item in manifest.inputs}
        if not set(body.source_refs).issubset(allowed):
            raise HTTPException(422, "Challenge references must identify original input snapshots")
        if body.discrepancy_type == "wrong_baseline" and not any(
            item.role == "baseline" and item.snapshot_id in body.source_refs
            for item in manifest.inputs
        ):
            raise HTTPException(422, "A baseline challenge must reference a baseline input")
        created = (
            session.execute(
                text("""INSERT INTO research.scientific_challenges
              (id,tenant_id,project_id,artifact_id,reviewer_subject,discrepancy_type,source_refs,statement)
              VALUES(:id,:tenant,:project,:artifact,:reviewer,:kind,CAST(:refs AS jsonb),:statement)
              RETURNING id,artifact_id,reviewer_subject,discrepancy_type,source_refs,statement,created_at"""),
                {
                    "id": uuid4(),
                    "tenant": tenant,
                    "project": row["project_id"],
                    "artifact": artifact_id,
                    "reviewer": principal.subject,
                    "kind": body.discrepancy_type,
                    "refs": json.dumps([str(item) for item in body.source_refs]),
                    "statement": body.statement,
                },
            )
            .mappings()
            .one()
        )
        return ChallengeView.model_validate(created)


@router.get("/artifacts/{artifact_id}/challenges", response_model=list[ChallengeView])
def list_challenges(
    artifact_id: UUID, request: Request, principal: CurrentPrincipal
) -> list[ChallengeView]:
    tenant = _review_tenant(request, principal, artifact_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        row, _ = _original(session, tenant, artifact_id)
        _review_access(session, tenant, row["project_id"], artifact_id, principal, write=False)
        records = (
            session.execute(
                text("""SELECT c.id,c.artifact_id,c.reviewer_subject,c.discrepancy_type,
                c.source_refs,c.statement,c.created_at,
                (SELECT count(*) FROM research.challenge_responses r
                  WHERE r.tenant_id=c.tenant_id AND r.project_id=c.project_id
                    AND r.challenge_id=c.id) AS response_count,
                CASE WHEN EXISTS(SELECT 1 FROM research.challenge_resolutions z
                  WHERE z.tenant_id=c.tenant_id AND z.project_id=c.project_id
                    AND z.challenge_id=c.id AND z.verdict='inconclusive') THEN 'inconclusive'
                  WHEN EXISTS(SELECT 1 FROM research.challenge_resolutions z
                  WHERE z.tenant_id=c.tenant_id AND z.project_id=c.project_id
                    AND z.challenge_id=c.id) THEN 'resolved'
                  WHEN EXISTS(SELECT 1 FROM research.challenge_responses r
                  WHERE r.tenant_id=c.tenant_id AND r.project_id=c.project_id
                    AND r.challenge_id=c.id) THEN 'responded' ELSE 'open' END AS status
              FROM research.scientific_challenges c
              WHERE c.tenant_id=:tenant AND c.artifact_id=:artifact
              ORDER BY c.created_at,c.id LIMIT 100"""),
                {"tenant": tenant, "artifact": artifact_id},
            )
            .mappings()
            .all()
        )
        return [ChallengeView.model_validate(item) for item in records]


@router.post(
    "/challenges/{challenge_id}/responses", response_model=ChallengeResponseView, status_code=201
)
def respond_to_challenge(
    challenge_id: UUID,
    body: ChallengeResponseRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> ChallengeResponseView:
    with engine(request).connect() as connection:
        resolved = connection.execute(
            text("SELECT iam.resolve_challenge_tenant(:subject,:challenge)"),
            {"subject": principal.subject, "challenge": challenge_id},
        ).scalar_one_or_none()
    if resolved is None:
        raise HTTPException(404, "Challenge not found")
    tenant = cast(UUID, resolved)
    with TenantUnitOfWork(engine(request), tenant) as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:id AS text),0))"),
            {"id": challenge_id},
        )
        challenge = (
            session.execute(
                text("""SELECT project_id,artifact_id FROM research.scientific_challenges
                  WHERE tenant_id=:tenant AND id=:challenge"""),
                {"tenant": tenant, "challenge": challenge_id},
            )
            .mappings()
            .one_or_none()
        )
        if challenge is None:
            raise HTTPException(404, "Challenge not found")
        terminal = session.execute(
            text("""SELECT 1 FROM research.challenge_resolutions
              WHERE tenant_id=:tenant AND challenge_id=:challenge"""),
            {"tenant": tenant, "challenge": challenge_id},
        ).scalar_one_or_none()
        if terminal is not None:
            raise HTTPException(409, "Challenge has already been resolved")
        try:
            authorize_project(session, tenant, challenge["project_id"], principal, "evidence:write")
        except (LookupError, PermissionError) as error:
            raise HTTPException(403, "Evidence response permission required") from error
        if body.response_artifact_id == challenge["artifact_id"]:
            raise HTTPException(422, "A response must link a new immutable artifact")
        original, _ = _original(session, tenant, challenge["artifact_id"])
        correction, _ = _original(session, tenant, body.response_artifact_id)
        if (
            correction["project_id"] != original["project_id"]
            or correction["investigation_id"] != original["investigation_id"]
        ):
            raise HTTPException(422, "Response artifact must belong to the original investigation")
        created = (
            session.execute(
                text("""INSERT INTO research.challenge_responses
                  (id,tenant_id,project_id,challenge_id,responder_subject,response_artifact_id,statement)
                  VALUES(:id,:tenant,:project,:challenge,:actor,:artifact,:statement)
                  RETURNING id,challenge_id,responder_subject,response_artifact_id,statement,created_at"""),
                {
                    "id": uuid4(),
                    "tenant": tenant,
                    "project": original["project_id"],
                    "challenge": challenge_id,
                    "actor": principal.subject,
                    "artifact": body.response_artifact_id,
                    "statement": body.statement,
                },
            )
            .mappings()
            .one()
        )
        return ChallengeResponseView.model_validate(created)


@router.post(
    "/challenges/{challenge_id}/resolve", response_model=ChallengeResolutionView, status_code=201
)
def resolve_challenge(
    challenge_id: UUID,
    body: ResolveChallengeRequest,
    request: Request,
    principal: CurrentPrincipal,
    if_match: str = Header(),
) -> ChallengeResolutionView:
    with engine(request).connect() as connection:
        resolved = connection.execute(
            text("SELECT iam.resolve_challenge_tenant(:subject,:challenge)"),
            {"subject": principal.subject, "challenge": challenge_id},
        ).scalar_one_or_none()
    if resolved is None:
        raise HTTPException(404, "Challenge not found")
    tenant = cast(UUID, resolved)
    with TenantUnitOfWork(engine(request), tenant) as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(CAST(:id AS text),0))"),
            {"id": challenge_id},
        )
        challenge = (
            session.execute(
                text("""SELECT project_id,artifact_id,reviewer_subject
                  FROM research.scientific_challenges
                  WHERE tenant_id=:tenant AND id=:challenge"""),
                {"tenant": tenant, "challenge": challenge_id},
            )
            .mappings()
            .one_or_none()
        )
        if challenge is None:
            raise HTTPException(404, "Challenge not found")
        response_count = session.execute(
            text("""SELECT count(*) FROM research.challenge_responses
              WHERE tenant_id=:tenant AND challenge_id=:challenge"""),
            {"tenant": tenant, "challenge": challenge_id},
        ).scalar_one()
        if if_match != f'"{challenge_id}:{response_count}"':
            raise HTTPException(412, "Challenge response state changed")
        original, manifest = _original(session, tenant, challenge["artifact_id"])
        _review_access(
            session, tenant, original["project_id"], original["id"], principal, write=True
        )
        responders = session.execute(
            text("""SELECT responder_subject FROM research.challenge_responses
              WHERE tenant_id=:tenant AND challenge_id=:challenge"""),
            {"tenant": tenant, "challenge": challenge_id},
        ).scalars()
        _require_separate_resolver(
            principal.subject, manifest.created_by, challenge["reviewer_subject"], set(responders)
        )
        for artifact_id in body.arbitration_refs:
            reference, _ = _original(session, tenant, artifact_id)
            if (
                artifact_id == original["id"]
                or reference["project_id"] != original["project_id"]
                or reference["investigation_id"] != original["investigation_id"]
            ):
                raise HTTPException(
                    422, "Arbitration must reference separate investigation evidence"
                )
        try:
            created = (
                session.execute(
                    text("""INSERT INTO research.challenge_resolutions
                      (id,tenant_id,project_id,challenge_id,resolver_subject,verdict,
                       arbitration_refs,limitations)
                      VALUES(:id,:tenant,:project,:challenge,:actor,:verdict,
                             CAST(:refs AS jsonb),:limitations)
                      RETURNING id,challenge_id,resolver_subject,verdict,arbitration_refs,
                        limitations,independence_verified,created_at"""),
                    {
                        "id": uuid4(),
                        "tenant": tenant,
                        "project": original["project_id"],
                        "challenge": challenge_id,
                        "actor": principal.subject,
                        "verdict": body.verdict,
                        "refs": json.dumps([str(item) for item in body.arbitration_refs]),
                        "limitations": body.limitations,
                    },
                )
                .mappings()
                .one()
            )
        except IntegrityError as error:
            raise HTTPException(409, "Challenge already has a resolution") from error
        return ChallengeResolutionView.model_validate(created)


@router.post(
    "/artifacts/{artifact_id}/reviews", response_model=IndependentReviewView, status_code=201
)
def create_review(
    artifact_id: UUID,
    body: IndependentReviewRequest,
    request: Request,
    principal: CurrentPrincipal,
) -> IndependentReviewView:
    tenant = _review_tenant(request, principal, artifact_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        row, manifest = _original(session, tenant, artifact_id)
        _review_access(session, tenant, row["project_id"], artifact_id, principal, write=True)
        if principal.subject == manifest.created_by:
            raise HTTPException(
                403, "The result author cannot independently review their own result"
            )
        created = (
            session.execute(
                text("""INSERT INTO research.independent_reviews
              (id,tenant_id,project_id,artifact_id,reviewer_subject,verdict,statement,reproduction_digest)
              VALUES(:id,:tenant,:project,:artifact,:reviewer,:verdict,:statement,:digest)
              RETURNING id,artifact_id,reviewer_subject,verdict,statement,reproduction_digest,
                independence_verified,created_at"""),
                {
                    "id": uuid4(),
                    "tenant": tenant,
                    "project": row["project_id"],
                    "artifact": artifact_id,
                    "reviewer": principal.subject,
                    "verdict": body.verdict,
                    "statement": body.statement,
                    "digest": body.reproduction_digest,
                },
            )
            .mappings()
            .one()
        )
        return IndependentReviewView.model_validate(created)


_PORTABLE_REVIEW_MEDIA = "application/vnd.orca.review+json"


@router.get(
    "/artifacts/{artifact_id}/reviews",
    response_model=list[IndependentReviewView],
    responses={200: {"content": {_PORTABLE_REVIEW_MEDIA: {"schema": {"type": "object"}}}}},
)
def list_reviews(
    artifact_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    accept: str = Header(default="application/json"),
) -> list[IndependentReviewView] | Response:
    tenant = _review_tenant(request, principal, artifact_id)
    with TenantUnitOfWork(engine(request), tenant) as session:
        if accept.partition(";")[0].strip().lower() == _PORTABLE_REVIEW_MEDIA:
            try:
                state = request.app.state
                package = assemble_review_export(
                    session,
                    tenant,
                    artifact_id,
                    principal,
                    signing_key=getattr(state, "review_export_signing_key", None),
                    signer_key_id=getattr(state, "review_export_key_id", None),
                )
            except LookupError as error:
                raise HTTPException(404, "Review artifact not found") from error
            except PermissionError as error:
                raise HTTPException(403, "Evidence read permission required") from error
            except ValueError as error:
                raise HTTPException(409, "Review evidence is inconsistent") from error
            return Response(
                package.document,
                media_type=_PORTABLE_REVIEW_MEDIA,
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": f'attachment; filename="orca-review-{artifact_id}.json"',
                    "ETag": f'"{package.payload_digest}"',
                    "Vary": "Accept",
                    "X-ORCA-Package-Signature": "signed" if package.signed else "unsigned",
                },
            )
        row, _ = _original(session, tenant, artifact_id)
        _review_access(session, tenant, row["project_id"], artifact_id, principal, write=False)
        records = (
            session.execute(
                text("""SELECT id,artifact_id,reviewer_subject,verdict,statement,reproduction_digest,
                independence_verified,created_at FROM research.independent_reviews
              WHERE tenant_id=:tenant AND artifact_id=:artifact ORDER BY created_at,id LIMIT 100"""),
                {"tenant": tenant, "artifact": artifact_id},
            )
            .mappings()
            .all()
        )
        return [IndependentReviewView.model_validate(item) for item in records]

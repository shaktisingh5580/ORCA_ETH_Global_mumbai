"""Purpose: expose P06 evidence only when immutably published by a completed research run.
Behavior: resolve tenant server-side, recheck project permission and return verified manifest lineage.
Refs: main_implementation plan.md P06.1; api design.md §3.7; Phase: P06.
"""

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.investigations import investigation_context
from orca.api.routers.projects import engine
from orca.application.workspace_access import authorize_project
from orca.contracts.common import Page
from orca.contracts.evidence import ArtifactView
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.science.manifests import verify_manifest

router = APIRouter(prefix="/v1/research", tags=["evidence"])


def _authorize(session: Any, tenant: UUID, project: UUID, principal: Principal) -> None:
    try:
        authorize_project(session, tenant, project, principal, "evidence:read")
    except LookupError as error:
        raise HTTPException(404, "Artifact not found") from error
    except PermissionError as error:
        raise HTTPException(403, "Evidence access not permitted") from error


def _authorize_artifact_read(
    session: Any, tenant: UUID, project: UUID, artifact_id: UUID, principal: Principal
) -> None:
    """Allow a named external reviewer only while their exact artifact grant remains active."""
    try:
        authorize_project(session, tenant, project, principal, "evidence:read")
        return
    except (LookupError, PermissionError):
        pass
    if "evidence:read" not in principal.token_scopes:
        raise HTTPException(403, "Evidence read scope is required")
    active = session.execute(
        text("""SELECT 1 FROM research.artifact_review_grants WHERE tenant_id=:tenant
          AND project_id=:project AND artifact_id=:artifact AND reviewer_subject=:subject
          AND revoked_at IS NULL AND expires_at>clock_timestamp() FOR SHARE"""),
        {
            "tenant": tenant,
            "project": project,
            "artifact": artifact_id,
            "subject": principal.subject,
        },
    ).scalar_one_or_none()
    if active is None:
        raise HTTPException(404, "Artifact not found")


def _artifact_row(session: Any, tenant: UUID, artifact_id: UUID) -> Any:
    row = (
        session.execute(
            text("""SELECT a.id,a.project_id,a.investigation_id,a.run_id,a.evidence_manifest_id,
          a.scientific_status,a.created_at,m.manifest,m.manifest_digest
          FROM research.run_artifacts a JOIN research.science_evidence_manifests m
            ON m.tenant_id=a.tenant_id AND m.project_id=a.project_id AND m.id=a.evidence_manifest_id
          WHERE a.tenant_id=:tenant AND a.id=:artifact"""),
            {"tenant": tenant, "artifact": artifact_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Artifact not found")
    return row


def _verified(row: Any) -> tuple[ScienceEvidenceManifest, ArtifactView]:
    manifest = ScienceEvidenceManifest.model_validate(row["manifest"])
    verify_manifest(manifest)
    if (
        manifest.id != row["evidence_manifest_id"]
        or manifest.manifest_digest != row["manifest_digest"]
        or manifest.result_status != row["scientific_status"]
    ):
        raise HTTPException(409, "Published evidence identity is inconsistent")
    view = ArtifactView(
        id=row["id"],
        investigation_id=row["investigation_id"],
        run_id=row["run_id"],
        evidence_manifest_id=manifest.id,
        manifest_digest=manifest.manifest_digest,
        scientific_status=row["scientific_status"],
        source_mode=manifest.source_mode,
        method_id=manifest.method_id,
        method_version=manifest.method_version,
        created_at=row["created_at"],
    )
    return manifest, view


def _artifact_context(request: Request, principal: Principal, artifact_id: UUID) -> UUID:
    db = engine(request)
    with db.connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_artifact_tenant(:subject,:artifact)"),
            {"subject": principal.subject, "artifact": artifact_id},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Artifact not found")
    return cast(UUID, tenant)


@router.get("/investigations/{investigation_id}/artifacts", response_model=Page[ArtifactView])
def list_artifacts(
    investigation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
) -> Page[ArtifactView]:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        _authorize(session, tenant, project, principal)
        rows = (
            session.execute(
                text("""SELECT a.id,a.project_id,a.investigation_id,a.run_id,a.evidence_manifest_id,
              a.scientific_status,a.created_at,m.manifest,m.manifest_digest
              FROM research.run_artifacts a JOIN research.science_evidence_manifests m
                ON m.tenant_id=a.tenant_id AND m.project_id=a.project_id AND m.id=a.evidence_manifest_id
              WHERE a.tenant_id=:tenant AND a.project_id=:project AND a.investigation_id=:investigation
                AND (CAST(:cursor AS uuid) IS NULL OR a.id<CAST(:cursor AS uuid))
              ORDER BY a.id DESC LIMIT :limit"""),
                {
                    "tenant": tenant,
                    "project": project,
                    "investigation": investigation_id,
                    "limit": limit + 1,
                    "cursor": cursor,
                },
            )
            .mappings()
            .all()
        )
        items = [_verified(row)[1] for row in rows[:limit]]
    return Page(
        items=items,
        page_size=limit,
        next_cursor=str(items[-1].id) if len(rows) > limit and items else None,
    )


def _read_artifact(
    request: Request, principal: Principal, artifact_id: UUID
) -> tuple[ScienceEvidenceManifest, ArtifactView]:
    db = engine(request)
    with db.connect() as connection:
        resolved = connection.execute(
            text("SELECT iam.resolve_review_artifact_tenant(:subject,:artifact)"),
            {"subject": principal.subject, "artifact": artifact_id},
        ).scalar_one_or_none()
    if resolved is None:
        raise HTTPException(404, "Artifact not found")
    tenant = cast(UUID, resolved)
    with TenantUnitOfWork(db, tenant) as session:
        row = _artifact_row(session, tenant, artifact_id)
        _authorize_artifact_read(session, tenant, row["project_id"], artifact_id, principal)
        return _verified(row)


@router.get("/artifacts/{artifact_id}", response_model=ArtifactView)
def get_artifact(artifact_id: UUID, request: Request, principal: CurrentPrincipal) -> ArtifactView:
    return _read_artifact(request, principal, artifact_id)[1]


@router.get("/artifacts/{artifact_id}/manifest", response_model=ScienceEvidenceManifest)
def get_artifact_manifest(
    artifact_id: UUID, request: Request, principal: CurrentPrincipal
) -> ScienceEvidenceManifest:
    return _read_artifact(request, principal, artifact_id)[0]


@router.get("/artifacts/{artifact_id}/lineage")
def get_artifact_lineage(
    artifact_id: UUID, request: Request, principal: CurrentPrincipal
) -> dict[str, object]:
    manifest, _ = _read_artifact(request, principal, artifact_id)
    return {
        "artifact_id": str(artifact_id),
        "manifest_digest": manifest.manifest_digest,
        "method_id": manifest.method_id,
        "method_version": manifest.method_version,
        "method_code_digest": manifest.method_code_digest,
        "inputs": [item.model_dump(mode="json") for item in manifest.inputs],
    }

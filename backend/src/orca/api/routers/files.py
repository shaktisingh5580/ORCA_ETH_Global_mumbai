"""Purpose: expose staged uploads, validation status and authorized finalized-file downloads.
Behavior: require project access, hide quarantine bytes, and fail closed without private storage.
Refs: API 3.3; main plan P02.5; Phase: P02 completion repair.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, cast
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Query, Request, Response
from sqlalchemy import Engine, text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.organizations import errors
from orca.api.routers.projects import engine, resolve_project
from orca.application.files import FILE_VISIBILITY_SQL, Files
from orca.application.ports.object_store import ValidatingObjectStore
from orca.application.workspace_access import authorize_project
from orca.contracts.common import Page
from orca.contracts.files import (
    CompleteUpload,
    DownloadIntent,
    EphemeralDownload,
    ProjectFile,
    UploadSession,
    UploadStatus,
)
from orca.contracts.projects import UploadIntent
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork

router = APIRouter(prefix="/v1/research", tags=["project files"])


def storage(request: Request) -> ValidatingObjectStore:
    store = getattr(request.app.state, "upload_store", None)
    if store is None:
        raise HTTPException(503, "Project upload storage is not enabled")
    return cast(ValidatingObjectStore, store)


@contextmanager
def file_errors() -> Iterator[None]:
    with errors():
        try:
            yield
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except (BotoCoreError, ClientError) as exc:
            raise HTTPException(
                409, "Object storage operation unavailable or upload not ready"
            ) from exc


def upload_tenant(db: Engine, identifier: UUID, principal: Principal) -> UUID:
    with db.connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_upload_tenant(:s,:id)"),
            {"s": principal.subject, "id": identifier},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Upload or file not found")
    return cast(UUID, tenant)


@router.post("/projects/{project_id}/uploads", response_model=UploadSession, status_code=201)
def create_upload(
    project_id: UUID, body: UploadIntent, request: Request, principal: CurrentPrincipal
) -> UploadSession:
    store, db = storage(request), engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with file_errors(), TenantUnitOfWork(db, tenant) as session:
        return Files(session, tenant, store).create(project_id, body, principal)


@router.post("/uploads/{upload_id}/complete", response_model=UploadStatus, status_code=202)
def complete_upload(
    upload_id: UUID, body: CompleteUpload, request: Request, principal: CurrentPrincipal
) -> UploadStatus:
    store, db = storage(request), engine(request)
    tenant = upload_tenant(db, upload_id, principal)
    with file_errors(), TenantUnitOfWork(db, tenant) as session:
        return Files(session, tenant, store).complete(upload_id, body, principal)


@router.get("/uploads/{upload_id}", response_model=UploadStatus)
def read_upload(upload_id: UUID, request: Request, principal: CurrentPrincipal) -> UploadStatus:
    store, db = storage(request), engine(request)
    tenant = upload_tenant(db, upload_id, principal)
    with file_errors(), TenantUnitOfWork(db, tenant) as session:
        files = Files(session, tenant, store)
        return files.status(files.row(upload_id, principal, "project:read", require_owner=True))


@router.delete("/uploads/{upload_id}", status_code=204)
def abort_upload(upload_id: UUID, request: Request, principal: CurrentPrincipal) -> Response:
    store, db = storage(request), engine(request)
    tenant = upload_tenant(db, upload_id, principal)
    with file_errors(), TenantUnitOfWork(db, tenant) as session:
        Files(session, tenant, store).abort(upload_id, principal)
    return Response(status_code=204)


@router.get("/projects/{project_id}/files", response_model=Page[ProjectFile])
def list_files(
    project_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: UUID | None = None,
    classification: str | None = None,
    media_type: str | None = None,
) -> Page[ProjectFile]:
    storage(request)
    db = engine(request)
    tenant, _ = resolve_project(db, principal, project_id)
    with file_errors(), TenantUnitOfWork(db, tenant) as session:
        authorize_project(session, tenant, project_id, principal, "project:read")
        rows = list(
            session.execute(
                text(
                    """SELECT u.* FROM research.uploads u WHERE tenant_id=:t
          AND project_id=:p AND finalized AND status='finalized'
          AND (CAST(:cursor AS uuid) IS NULL OR id>CAST(:cursor AS uuid))
          AND (CAST(:classification AS text) IS NULL OR classification=:classification)
          AND (CAST(:media AS text) IS NULL OR media_type=:media)
          AND """
                    + FILE_VISIBILITY_SQL
                    + " ORDER BY id LIMIT :limit"
                ),
                {
                    "t": tenant,
                    "actor": principal.subject,
                    "p": project_id,
                    "cursor": cursor,
                    "classification": classification,
                    "media": media_type,
                    "limit": limit + 1,
                },
            ).mappings()
        )
        return Page[ProjectFile](
            items=[Files.metadata(row) for row in rows[:limit]],
            page_size=limit,
            next_cursor=str(rows[limit - 1]["id"]) if len(rows) > limit else None,
        )


@router.get("/files/{file_id}", response_model=ProjectFile)
def read_file(file_id: UUID, request: Request, principal: CurrentPrincipal) -> ProjectFile:
    store, db = storage(request), engine(request)
    tenant = upload_tenant(db, file_id, principal)
    with file_errors(), TenantUnitOfWork(db, tenant) as session:
        row = Files(session, tenant, store).row(file_id, principal, "project:read")
        if not row["finalized"]:
            raise HTTPException(404, "File not found")
        return Files.metadata(row)


@router.post("/files/{file_id}/download-intents", response_model=EphemeralDownload)
def download_file(
    file_id: UUID, body: DownloadIntent, request: Request, principal: CurrentPrincipal
) -> EphemeralDownload:
    store, db = storage(request), engine(request)
    tenant = upload_tenant(db, file_id, principal)
    with file_errors(), TenantUnitOfWork(db, tenant) as session:
        return Files(session, tenant, store).download(file_id, principal, body.purpose)

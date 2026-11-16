"""Purpose: create and serve P06 versioned scientific reports from published evidence.
Behavior: recheck project access per download, preserve report bytes, and gate public publication on source rights.
Refs: main_implementation plan.md P06.2/P06.4; api design.md §3.7; Phase: P06.
"""

import hashlib
import re
from typing import Any, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import text

from orca.api.dependencies import CurrentPrincipal
from orca.api.routers.evidence import _artifact_row, _verified
from orca.api.routers.investigations import investigation_context
from orca.api.routers.projects import engine
from orca.application.workspace_access import authorize_project
from orca.contracts.reports import CreateScientificReport, ScientificReportView
from orca.domain.identity import Principal
from orca.infrastructure.db.unit_of_work import TenantUnitOfWork
from orca.workers.handlers.report import (
    render_report_html,
    render_report_json,
    render_report_pdf,
    report_view,
)

router = APIRouter(prefix="/v1/research", tags=["scientific reports"])
_RANGE = re.compile(r"^bytes=(\d{1,12})-(\d{0,12})$")


def _authorize(session: Any, tenant: UUID, project: UUID, principal: Principal, scope: str) -> None:
    try:
        authorize_project(session, tenant, project, principal, scope)
    except LookupError as error:
        raise HTTPException(404, "Report not found") from error
    except PermissionError as error:
        raise HTTPException(403, "Report permission required") from error


def _report_tenant(request: Request, principal: Principal, report_id: UUID) -> UUID:
    with engine(request).connect() as connection:
        tenant = connection.execute(
            text("SELECT iam.resolve_report_tenant(:subject,:report)"),
            {"subject": principal.subject, "report": report_id},
        ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Report not found")
    return cast(UUID, tenant)


def _row(session: Any, tenant: UUID, report_id: UUID) -> Any:
    row = (
        session.execute(
            text("""SELECT id,project_id,investigation_id,artifact_id,revision,created_by,status,
              document,document_digest,html_bytes,html_digest,pdf_bytes,pdf_digest,
              created_at,published_at FROM research.scientific_reports
              WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant, "id": report_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(404, "Report not found")
    return row


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _verified_report(row: Any) -> ScientificReportView:
    document = row["document"]
    if (
        _digest(render_report_json(document)) != row["document_digest"]
        or _digest(row["html_bytes"]) != row["html_digest"]
        or _digest(row["pdf_bytes"]) != row["pdf_digest"]
        or render_report_html(document) != row["html_bytes"]
        or render_report_pdf(document) != row["pdf_bytes"]
    ):
        raise HTTPException(409, "Stored report rendering differs from its immutable digest")
    return ScientificReportView.model_validate(
        {field: row[field] for field in ScientificReportView.model_fields}
    )


@router.post(
    "/investigations/{investigation_id}/reports",
    response_model=ScientificReportView,
    status_code=201,
)
def create_report(
    investigation_id: UUID,
    body: CreateScientificReport,
    request: Request,
    principal: CurrentPrincipal,
) -> ScientificReportView:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        _authorize(session, tenant, project, principal, "report:publish")
        session.execute(
            text("""SELECT id FROM research.investigations WHERE tenant_id=:tenant
              AND project_id=:project AND id=:investigation FOR UPDATE"""),
            {"tenant": tenant, "project": project, "investigation": investigation_id},
        ).scalar_one()
        artifact = _artifact_row(session, tenant, body.artifact_id)
        if artifact["project_id"] != project or artifact["investigation_id"] != investigation_id:
            raise HTTPException(404, "Artifact not found in investigation")
        manifest, _ = _verified(artifact)
        try:
            document = report_view(manifest, title=body.title, limitations=tuple(body.limitations))
            encoded, html_bytes, pdf_bytes = (
                render_report_json(document),
                render_report_html(document),
                render_report_pdf(document),
            )
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        revision: int = session.execute(
            text("""SELECT COALESCE(MAX(revision),0)+1 FROM research.scientific_reports
              WHERE tenant_id=:tenant AND project_id=:project AND investigation_id=:investigation"""),
            {"tenant": tenant, "project": project, "investigation": investigation_id},
        ).scalar_one()
        identifier = uuid4()
        session.execute(
            text("""INSERT INTO research.scientific_reports
              (id,tenant_id,project_id,investigation_id,artifact_id,revision,created_by,
               document,document_digest,html_bytes,html_digest,pdf_bytes,pdf_digest)
              VALUES(:id,:tenant,:project,:investigation,:artifact,:revision,:actor,
                CAST(:document AS jsonb),:json_digest,:html,:html_digest,:pdf,:pdf_digest)"""),
            {
                "id": identifier,
                "tenant": tenant,
                "project": project,
                "investigation": investigation_id,
                "artifact": body.artifact_id,
                "revision": revision,
                "actor": principal.subject,
                "document": encoded.decode("utf-8"),
                "json_digest": _digest(encoded),
                "html": html_bytes,
                "html_digest": _digest(html_bytes),
                "pdf": pdf_bytes,
                "pdf_digest": _digest(pdf_bytes),
            },
        )
        return _verified_report(_row(session, tenant, identifier))


@router.get("/investigations/{investigation_id}/reports", response_model=list[ScientificReportView])
def list_reports(
    investigation_id: UUID,
    request: Request,
    principal: CurrentPrincipal,
    limit: int = Query(default=50, ge=1, le=100),
) -> list[ScientificReportView]:
    db = engine(request)
    tenant, project = investigation_context(db, principal, investigation_id)
    with TenantUnitOfWork(db, tenant) as session:
        _authorize(session, tenant, project, principal, "project:read")
        identifiers = cast(
            list[UUID],
            (
                session.execute(
                    text("""SELECT id FROM research.scientific_reports WHERE tenant_id=:tenant
              AND project_id=:project AND investigation_id=:investigation
              ORDER BY revision DESC LIMIT :limit"""),
                    {
                        "tenant": tenant,
                        "project": project,
                        "investigation": investigation_id,
                        "limit": limit,
                    },
                )
                .scalars()
                .all()
            ),
        )
        return [_verified_report(_row(session, tenant, item)) for item in identifiers]


def _read(
    request: Request, principal: Principal, report_id: UUID
) -> tuple[Any, ScientificReportView]:
    db = engine(request)
    tenant = _report_tenant(request, principal, report_id)
    with TenantUnitOfWork(db, tenant) as session:
        row = _row(session, tenant, report_id)
        _authorize(session, tenant, row["project_id"], principal, "project:read")
        return row, _verified_report(row)


@router.get("/reports/{report_id}", response_model=ScientificReportView)
def get_report(
    report_id: UUID, request: Request, principal: CurrentPrincipal
) -> ScientificReportView:
    return _read(request, principal, report_id)[1]


@router.post("/reports/{report_id}/publish", response_model=ScientificReportView)
def publish_report(
    report_id: UUID, request: Request, principal: CurrentPrincipal
) -> ScientificReportView:
    db = engine(request)
    tenant = _report_tenant(request, principal, report_id)
    with TenantUnitOfWork(db, tenant) as session:
        row = _row(session, tenant, report_id)
        _authorize(session, tenant, row["project_id"], principal, "report:publish")
        if row["status"] != "draft":
            raise HTTPException(409, "Report is already published")
        artifact = _artifact_row(session, tenant, row["artifact_id"])
        manifest, _ = _verified(artifact)
        if manifest.source_mode == "synthetic_test":
            raise HTTPException(409, "Synthetic inputs cannot be publicly published")
        rights = (
            session.execute(
                text("""SELECT count(*) AS source_count,
                  COALESCE(bool_and(p.access_class='public' AND p.metadata_publication_allowed
                    AND COALESCE((s.manifest #>> '{product_version,license,can_redistribute}')::boolean,false)),false)
                    AS public_rights
                  FROM research.science_evidence_inputs i
                  JOIN research.dataset_snapshots s ON s.tenant_id=i.tenant_id AND s.id=i.snapshot_id
                  JOIN research.catalog_versions v ON v.tenant_id=s.tenant_id AND v.id=s.version_id
                  JOIN research.catalog_products p ON p.tenant_id=v.tenant_id AND p.id=v.product_id
                  WHERE i.tenant_id=:tenant AND i.project_id=:project AND i.manifest_id=:manifest"""),
                {"tenant": tenant, "project": row["project_id"], "manifest": manifest.id},
            )
            .mappings()
            .one()
        )
        if rights["source_count"] != len(manifest.inputs) or not rights["public_rights"]:
            raise HTTPException(403, "Source publication rights are not established")
        changed = session.execute(
            text("""UPDATE research.scientific_reports SET status='published',published_at=clock_timestamp()
              WHERE tenant_id=:tenant AND id=:id AND status='draft' RETURNING id"""),
            {"tenant": tenant, "id": report_id},
        ).scalar_one_or_none()
        if changed is None:
            raise HTTPException(409, "Report publication raced with another request")
        return _verified_report(_row(session, tenant, report_id))


@router.get("/reports/{report_id}/content/{format}")
def report_content(
    report_id: UUID,
    format: str,
    request: Request,
    principal: CurrentPrincipal,
) -> Response:
    if format not in {"json", "html", "pdf"}:
        raise HTTPException(404, "Report format not found")
    row, _ = _read(request, principal, report_id)
    contents = {
        "json": render_report_json(row["document"]),
        "html": row["html_bytes"],
        "pdf": row["pdf_bytes"],
    }
    content = contents[format]
    media = {
        "json": "application/json",
        "html": "text/html; charset=utf-8",
        "pdf": "application/pdf",
    }[format]
    headers = {
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'attachment; filename="orca-report-{report_id}.{format}"',
    }
    requested = request.headers.get("range")
    if requested is None:
        return Response(content, media_type=media, headers=headers)
    match = _RANGE.fullmatch(requested)
    if match is None:
        raise HTTPException(416, "Only one bounded byte range is supported")
    start = int(match[1])
    end = int(match[2]) if match[2] else len(content) - 1
    if start >= len(content) or end < start or end >= len(content):
        raise HTTPException(416, "Range is outside the report")
    headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
    return Response(content[start : end + 1], status_code=206, media_type=media, headers=headers)

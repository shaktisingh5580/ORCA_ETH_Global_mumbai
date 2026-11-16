"""Purpose: check P06 immutable report formats and protected byte-range behavior.
Behavior: reject altered renderings and advertise only authenticated report routes.
Refs: main_implementation plan.md P06.2/P06.4; Phase: P06.
"""

import re
import secrets
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.routers import reports
from orca.config import Settings
from orca.domain.identity import Principal
from orca.workers.handlers.report import render_report_html, render_report_json, render_report_pdf


def report_row() -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "orca.report/1.0",
        "title": "SST evidence",
        "status": "insufficient_evidence",
        "source_mode": "preserved_real",
        "evidence_manifest_digest": "sha256:" + "a" * 64,
        "method_id": "unapproved",
        "method_version": "0",
        "result_digest": "sha256:" + "b" * 64,
        "limitations": ["No approved method"],
    }
    html = render_report_html(document)
    pdf = render_report_pdf(document)
    return {
        "id": uuid4(),
        "investigation_id": uuid4(),
        "artifact_id": uuid4(),
        "project_id": uuid4(),
        "revision": 1,
        "created_by": "author",
        "status": "draft",
        "document": document,
        "document_digest": reports._digest(render_report_json(document)),
        "html_bytes": html,
        "html_digest": reports._digest(html),
        "pdf_bytes": pdf,
        "pdf_digest": reports._digest(pdf),
        "created_at": datetime.now(timezone.utc),
        "published_at": None,
    }


def request(range_value: str | None = None) -> Request:
    headers = [] if range_value is None else [(b"range", range_value.encode("ascii"))]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_report_routes_and_pdf_offsets() -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    paths = app.openapi()["paths"]
    assert "post" in paths["/v1/research/investigations/{investigation_id}/reports"]
    assert "get" in paths["/v1/research/reports/{report_id}/content/{format}"]
    pdf = report_row()["pdf_bytes"]
    assert isinstance(pdf, bytes)
    match = re.search(rb"startxref\n(\d+)\n", pdf)
    assert match is not None and pdf[int(match[1]) :].startswith(b"xref\n")


def test_report_digest_mismatch_rejected() -> None:
    row = report_row()
    reports._verified_report(row)
    row["pdf_bytes"] = b"corrupt"
    with pytest.raises(HTTPException) as error:
        reports._verified_report(row)
    assert error.value.status_code == 409


def test_report_range_and_invalid_range(monkeypatch: pytest.MonkeyPatch) -> None:
    row = report_row()
    principal = Principal("reader", "issuer", frozenset({"project:read"}))
    monkeypatch.setattr(reports, "_read", lambda *_args: (row, reports._verified_report(row)))
    response = reports.report_content(row["id"], "pdf", request("bytes=0-3"), principal)
    assert response.status_code == 206 and response.body == b"%PDF"
    assert response.headers["content-range"].startswith("bytes 0-3/")
    with pytest.raises(HTTPException) as error:
        reports.report_content(row["id"], "pdf", request("bytes=999999-"), principal)
    assert error.value.status_code == 416

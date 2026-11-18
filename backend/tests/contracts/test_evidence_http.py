"""Purpose: check P06 published-artifact API surface and evidence identity guards.
Behavior: verify routes are registered and mismatched stored manifest status cannot be displayed.
Refs: main_implementation plan.md P06.1; api design.md §3.7; Phase: P06.
"""

import io
import json
import secrets
import zipfile
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from p06_evidence import insufficient_manifest
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.routers import evidence
from orca.api.routers.evidence import _verified
from orca.config import Settings


def test_published_artifact_routes_registered() -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    paths = app.openapi()["paths"]
    assert "/v1/research/investigations/{investigation_id}/artifacts" in paths
    assert "/v1/research/artifacts/{artifact_id}/manifest" in paths
    assert "/v1/research/artifacts/{artifact_id}/lineage" in paths


def test_incomplete_result_blocks_supported_artifact_view() -> None:
    manifest = insufficient_manifest()
    row = {
        "id": uuid4(),
        "project_id": manifest.project_id,
        "investigation_id": uuid4(),
        "run_id": uuid4(),
        "evidence_manifest_id": manifest.id,
        "scientific_status": "supported",
        "created_at": datetime.now(timezone.utc),
        "manifest": manifest.model_dump(mode="json"),
        "manifest_digest": manifest.manifest_digest,
    }
    with pytest.raises(HTTPException) as failure:
        _verified(row)
    assert failure.value.status_code == 409


def test_reference_export_rechecks_source_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = insufficient_manifest()
    artifact = uuid4()
    active = True

    class Session:
        def execute(self, _, params):
            assert params["id"] in {item.snapshot_id for item in manifest.inputs}
            return SimpleNamespace(scalar_one_or_none=lambda: 1 if active else None)

    def snapshot(identifier):
        source = next(item for item in manifest.inputs if item.snapshot_id == identifier)
        return SimpleNamespace(
            raw_content_digest=source.raw_content_digest,
            byte_count=source.byte_count,
            product_version=SimpleNamespace(license=SimpleNamespace(license_ref="test-license")),
        )

    monkeypatch.setattr(evidence, "engine", lambda _: None)
    monkeypatch.setattr(evidence, "_artifact_context", lambda *_: manifest.tenant_id)
    monkeypatch.setattr(evidence, "TenantUnitOfWork", lambda *_: nullcontext(Session()))
    monkeypatch.setattr(
        evidence,
        "_artifact_row",
        lambda *_: {
            "id": artifact,
            "project_id": manifest.project_id,
            "investigation_id": uuid4(),
            "run_id": uuid4(),
            "evidence_manifest_id": manifest.id,
            "scientific_status": manifest.result_status,
            "created_at": manifest.created_at,
            "manifest": manifest.model_dump(mode="json"),
            "manifest_digest": manifest.manifest_digest,
        },
    )
    monkeypatch.setattr(evidence, "_authorize", lambda *_: None)
    monkeypatch.setattr(evidence, "SnapshotRepository", lambda *_: SimpleNamespace(get=snapshot))
    response = evidence.download_reference_export(artifact, None, None)
    assert response.headers["cache-control"] == "private, no-store"
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert archive.namelist() == ["manifest.json"]
        references = json.loads(archive.read("manifest.json"))
        assert len(references["inputs"]) == 2
        assert all(item["path"] is None for item in references["inputs"])
    active = False
    with pytest.raises(HTTPException) as failure:
        evidence.download_reference_export(artifact, None, None)
    assert failure.value.status_code == 403

"""Purpose: check P06 published-artifact API surface and evidence identity guards.
Behavior: verify routes are registered and mismatched stored manifest status cannot be displayed.
Refs: main_implementation plan.md P06.1; api design.md §3.7; Phase: P06.
"""

import secrets
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from p06_evidence import insufficient_manifest
from pydantic import SecretStr

from orca.api.app import create_app
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

"""Purpose: test bounded snapshot requests and honest worker-readiness authorization.
Behavior: isolate HTTP persistence in tests and reject malformed acquisition plans.
Refs: API 3.4; main plan P03.5; Phase: P03.5.
"""

import secrets
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.contracts.snapshot_requests import SnapshotRequest
from orca.domain.identity import Principal


def payload():
    return {
        "organization_id": str(uuid4()),
        "product_version_id": str(uuid4()),
        "geo_scope": {"type": "Point", "geometry": {"type": "Point", "coordinates": [68.5, 20.5]}},
        "time_window": {"start": "2025-01-01T00:00:00Z", "end": "2025-02-01T00:00:00Z"},
        "variables": ["sst"],
    }


def test_snapshot_request_bounds():
    body = payload()
    assert SnapshotRequest.model_validate(body).variables == ["sst"]
    for change in (
        {"variables": []},
        {"variables": ["sst", "sst"]},
        {"depth": -1},
        {"time_window": {"start": "2025-01-01T00:00:00Z", "end": "2025-03-01T00:00:00Z"}},
    ):
        with pytest.raises(ValidationError):
            SnapshotRequest.model_validate(body | change)


@pytest.mark.enable_socket
def test_snapshot_request_worker_readiness_and_scope():
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"research:plan"})
    )
    with (
        patch("orca.api.routers.snapshot_requests.engine"),
        patch(
            "orca.api.routers.snapshot_requests.resolve_tenant",
            return_value=(uuid4(), {"researcher"}),
        ),
        patch("orca.api.routers.snapshot_requests.SnapshotRequests") as service,
        TestClient(app) as client,
    ):
        response = client.post(
            "/v1/catalog/snapshot-requests", json=payload(), headers={"Idempotency-Key": "test"}
        )
        assert response.status_code == 503
        service.assert_not_called()
        app.dependency_overrides[current_principal] = lambda: Principal("test", "test", frozenset())
        assert (
            client.post(
                "/v1/catalog/snapshot-requests", json=payload(), headers={"Idempotency-Key": "test"}
            ).status_code
            == 403
        )
        service.assert_not_called()

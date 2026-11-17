"""Purpose: test bounded snapshot requests and honest worker-readiness authorization.
Behavior: isolate HTTP persistence in tests and reject malformed acquisition plans.
Refs: API 3.4; main plan P03.5; Phase: P03.5.
"""

import secrets
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.contracts.catalog import ProductVersion
from orca.contracts.snapshot_requests import SnapshotRequest, request_digest
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


def test_cache_keys_org_license_cycle_depth_as_of():
    tenant, product, version_id, org = uuid4(), uuid4(), uuid4(), uuid4()
    version = ProductVersion(
        id=version_id,
        tenant_id=tenant,
        product_id=product,
        source_version="cycle-2025-01",
        metadata_digest="sha256:" + "a" * 64,
        native_variable="sst",
        standard_name="sea_surface_temperature",
        physical_definition="test SST",
        native_units="K",
        calendar="gregorian",
        retrieved_at="2025-01-01T00:00:00Z",
        available_at="2025-01-01T00:00:00Z",
        license={
            "license_ref": "license-v1",
            "evidence_digest": "sha256:" + "b" * 64,
            "can_preserve": True,
        },
    )
    body = SnapshotRequest.model_validate(
        payload()
        | {
            "organization_id": str(org),
            "product_version_id": str(version_id),
            "time_window": {
                "start": "2025-01-01T00:00:00Z",
                "end": "2025-01-31T00:00:00Z",
            },
            "as_of": "2025-02-01T00:00:00Z",
        }
    )
    key = request_digest(body, version)
    assert request_digest(body, version) == key
    with pytest.raises(ValueError, match="pinned source version"):
        request_digest(body, version.model_copy(update={"id": uuid4()}))
    assert request_digest(body.model_copy(update={"organization_id": uuid4()}), version) != key
    assert request_digest(body, version.model_copy(update={"tenant_id": uuid4()})) != key
    assert request_digest(body.model_copy(update={"depth": 5.0}), version) != key
    assert (
        request_digest(
            body.model_copy(update={"as_of": datetime(2025, 2, 2, tzinfo=timezone.utc)}), version
        )
        != key
    )
    assert (
        request_digest(
            SnapshotRequest.model_validate(
                body.model_dump(mode="json")
                | {
                    "geo_scope": {
                        "type": "Point",
                        "geometry": {"type": "Point", "coordinates": [69.0, 20.5]},
                    }
                }
            ),
            version,
        )
        != key
    )
    assert (
        request_digest(
            SnapshotRequest.model_validate(
                body.model_dump(mode="json")
                | {
                    "time_window": {
                        "start": "2025-01-02T00:00:00Z",
                        "end": "2025-01-31T00:00:00Z",
                    }
                }
            ),
            version,
        )
        != key
    )
    assert (
        request_digest(
            SnapshotRequest.model_validate(
                body.model_dump(mode="json") | {"variables": ["sst", "x"]}
            ),
            version,
        )
        != key
    )
    assert (
        request_digest(
            body,
            ProductVersion.model_validate(
                version.model_dump(mode="json") | {"source_version": "cycle-2025-02"}
            ),
        )
        != key
    )
    assert (
        request_digest(
            body,
            ProductVersion.model_validate(
                version.model_dump(mode="json")
                | {
                    "license": {
                        "license_ref": "license-v2",
                        "evidence_digest": "sha256:" + "c" * 64,
                        "can_preserve": True,
                    }
                }
            ),
        )
        != key
    )


@pytest.mark.enable_socket
def test_source_unavailable_error():
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
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["code"] == "SOURCE_UNAVAILABLE"
        assert response.json()["request_id"]
        service.assert_not_called()
        app.dependency_overrides[current_principal] = lambda: Principal("test", "test", frozenset())
        assert (
            client.post(
                "/v1/catalog/snapshot-requests", json=payload(), headers={"Idempotency-Key": "test"}
            ).status_code
            == 403
        )
        service.assert_not_called()

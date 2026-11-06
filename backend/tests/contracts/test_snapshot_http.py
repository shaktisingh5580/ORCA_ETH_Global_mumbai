"""Purpose: test P03 snapshot HTTP authorization and absence semantics.
Behavior: inject test-only persistence and verify explicit filter/schema errors.
Refs: API 3.4; P03-snapshot-read-contract.json; Phase: P03.5.
"""

import secrets
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.domain.identity import Principal


@pytest.mark.enable_socket
def test_snapshot_http_empty_concealed_and_denied():
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"dataset:read"})
    )
    tenant = uuid4()
    with (
        patch("orca.api.routers.snapshots.authorize", return_value=tenant) as authorize,
        patch("orca.api.routers.snapshots.engine"),
        patch("orca.api.routers.snapshots.TenantUnitOfWork"),
        patch("orca.api.routers.snapshots.SnapshotRepository") as repository,
        TestClient(app) as client,
    ):
        query = {"organization_id": str(uuid4())}
        repository.return_value.list.return_value = []
        repository.return_value.get.return_value = None
        path = "/v1/catalog/snapshots"
        response = client.get(path, params=query)
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None, "page_size": 20}
        assert client.get(path, params=query | {"digest": "invalid"}).status_code == 422
        assert client.get(path, params=query | {"project_id": str(uuid4())}).status_code == 422
        for suffix in (f"/{uuid4()}", f"/{uuid4()}/manifest"):
            assert client.get(path + suffix, params=query).status_code == 404
        authorize.side_effect = HTTPException(403, "Dataset read scope required")
        repository.reset_mock()
        assert client.get(path, params=query).status_code == 403
        repository.assert_not_called()

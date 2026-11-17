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
from orca.contracts.catalog import LicenseProfile, ProductVersion
from orca.contracts.snapshots import DatasetSnapshotManifest
from orca.domain.identity import Principal


def snapshot_cursor_round_trip(repeat_cursor: bool):
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"dataset:read"})
    )
    tenant = uuid4()
    version = ProductVersion(
        id=uuid4(),
        tenant_id=tenant,
        product_id=uuid4(),
        source_version="v1",
        metadata_digest="sha256:" + "a" * 64,
        native_variable="sst",
        standard_name="sea_surface_temperature",
        physical_definition="Test SST",
        native_units="K",
        calendar="gregorian",
        retrieved_at="2025-01-01T00:00:00Z",
        license=LicenseProfile(
            license_ref="test", evidence_digest="sha256:" + "b" * 64, can_preserve=True
        ),
    )
    first, second = [
        DatasetSnapshotManifest(
            id=asset_id,
            tenant_id=tenant,
            product_version=version,
            raw_content_digest="sha256:" + "c" * 64,
            byte_count=4,
            retrieved_at=version.retrieved_at,
            origin_uri="https://example.org/test",
        )
        for asset_id in sorted((uuid4(), uuid4()))
    ]
    with (
        patch("orca.api.routers.snapshots.authorize", return_value=tenant),
        patch("orca.api.routers.snapshots.engine"),
        patch("orca.api.routers.snapshots.TenantUnitOfWork"),
        patch("orca.api.routers.snapshots.SnapshotRepository") as repository,
        TestClient(app) as client,
    ):
        query = {"organization_id": str(uuid4()), "product_id": str(version.product_id), "limit": 1}
        path = "/v1/catalog/snapshots"
        repository.return_value.list.side_effect = [[first, second], [second], [second]]
        response = client.get(path, params=query)
        assert response.status_code == 200
        page = response.json()
        assert [item["id"] for item in page["items"]] == [str(first.id)]
        assert page["next_cursor"] == str(first.id)
        response = client.get(path, params=query | {"cursor": page["next_cursor"]})
        assert response.status_code == 200
        assert [item["id"] for item in response.json()["items"]] == [str(second.id)]
        assert response.json()["next_cursor"] is None
        if repeat_cursor:
            repeated = client.get(path, params=query | {"cursor": page["next_cursor"]})
            assert repeated.status_code == 200
            assert repeated.json() == response.json()
        repository.return_value.list.assert_called_with(
            after=first.id, limit=2, product_id=version.product_id, digest=None, project_id=None
        )


@pytest.mark.enable_socket
def test_asset_page_cursor():
    snapshot_cursor_round_trip(False)


@pytest.mark.enable_socket
def test_cursor_page_stable():
    snapshot_cursor_round_trip(True)


@pytest.mark.enable_socket
def test_no_dataset_on_cache_miss():
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
        assert response.headers["cache-control"] == "no-store"
        assert client.get(path, params=query | {"digest": "invalid"}).status_code == 422
        project = uuid4()
        repository.return_value.project_member.return_value = False
        assert client.get(path, params=query | {"project_id": str(project)}).status_code == 404
        repository.return_value.project_member.return_value = True
        assert client.get(path, params=query | {"project_id": str(project)}).status_code == 200
        assert repository.return_value.list.call_args.kwargs["project_id"] == project
        for suffix in (f"/{uuid4()}", f"/{uuid4()}/manifest"):
            assert client.get(path + suffix, params=query).status_code == 404
        authorize.side_effect = HTTPException(403, "Dataset read scope required")
        repository.reset_mock()
        assert client.get(path, params=query).status_code == 403
        repository.assert_not_called()


@pytest.mark.enable_socket
def test_snapshot_reads_recheck_access_without_cached_results():
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"dataset:read"})
    )
    with (
        patch("orca.api.routers.snapshots.authorize", return_value=uuid4()) as authorize,
        patch("orca.api.routers.snapshots.engine"),
        patch("orca.api.routers.snapshots.TenantUnitOfWork"),
        patch("orca.api.routers.snapshots.SnapshotRepository") as repository,
        TestClient(app) as client,
    ):
        path = "/v1/catalog/snapshots"
        query = {"organization_id": str(uuid4()), "project_id": str(uuid4())}
        repository.return_value.project_member.side_effect = [True, False]
        repository.return_value.list.return_value = []

        first = client.get(path, params=query)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        second = client.get(path, params=query)
        assert second.status_code == 404
        assert second.headers["cache-control"] == "no-store"
        assert authorize.call_count == 2
        assert repository.return_value.project_member.call_count == 2
        repository.return_value.list.assert_called_once()

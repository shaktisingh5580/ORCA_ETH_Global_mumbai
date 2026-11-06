"""Purpose: verify P03 HTTP scope checks, page boundaries and concealed product misses.
Behavior: inject only test-layer persistence; real database RLS has separate integration tests.
Refs: API 3.4; P03-catalog-read-contract.json; Phase: P03.5.
"""

import secrets
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.application.catalog import CatalogService
from orca.config import Settings
from orca.contracts.catalog import ProductVersion, ScientificProduct, SourceProvider
from orca.domain.identity import Principal


@pytest.mark.enable_socket
def test_catalog_http_authorization_and_cursor():
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    tenant, organization = uuid4(), uuid4()
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"dataset:read"})
    )
    products = [
        ScientificProduct(
            id=uuid4(),
            tenant_id=tenant,
            provider_id="test",
            source_product_id=str(i),
            kind="observation",
        )
        for i in range(2)
    ]
    with (
        patch("orca.api.routers.catalog.engine"),
        patch("orca.api.routers.catalog.resolve_tenant", return_value=(tenant, {"researcher"})),
        patch("orca.api.routers.catalog.TenantUnitOfWork"),
        patch("orca.api.routers.catalog.CatalogService") as service,
        TestClient(app) as client,
    ):
        service.return_value.products.return_value = products
        query = {"organization_id": str(organization), "limit": 1}
        service.return_value.providers.return_value = [
            SourceProvider(id="a"),
            SourceProvider(id="b"),
        ]
        provider_response = client.get("/v1/catalog/providers", params=query | {"q": "a"})
        assert provider_response.status_code == 200
        assert provider_response.json()["next_cursor"] == "a"
        assert provider_response.json()["items"] == [{"id": "a", "live_verification": "unknown"}]
        service.return_value.providers.assert_called_with(after=None, query="a", limit=2)
        service.return_value.providers.return_value = []
        assert client.get("/v1/catalog/providers", params=query).json()["next_cursor"] is None
        assert (
            client.get(
                "/v1/catalog/providers", params=query | {"source_class": "anything"}
            ).status_code
            == 422
        )
        response = client.get("/v1/catalog/products", params=query)
        assert response.status_code == 200
        assert len(response.json()["items"]) == 1
        assert response.json()["next_cursor"] == str(products[0].id)
        service.return_value.products.return_value = products[:1]
        assert client.get("/v1/catalog/products", params=query).json()["next_cursor"] is None
        assert (
            client.get("/v1/catalog/products", params=query | {"bbox": "unsupported"}).status_code
            == 422
        )
        assert (
            client.get("/v1/catalog/products", params=query | {"kind": "invalid"}).status_code
            == 422
        )
        assert (
            client.get(
                "/v1/catalog/products",
                params=query
                | {
                    "provider_id": "test",
                    "kind": "observation",
                    "variable": "sea_surface_temperature",
                },
            ).status_code
            == 200
        )
        service.return_value.products.assert_called_with(
            after=None,
            limit=2,
            provider_id="test",
            kind="observation",
            variable="sea_surface_temperature",
            bbox=None,
            start=None,
            end=None,
        )
        service.return_value.product.return_value = None
        for bad in ("NaN,20,69,21", "181,20,182,21", "69,20,68,21", "68,20,69"):
            assert (
                client.get("/v1/catalog/products", params=query | {"bbox": bad}).status_code == 422
            )
        assert (
            client.get(
                "/v1/catalog/products",
                params=query | {"start": "2025-02-01T00:00:00Z", "end": "2025-01-01T00:00:00Z"},
            ).status_code
            == 422
        )
        assert (
            client.get(
                f"/v1/catalog/products/{uuid4()}", params={"organization_id": str(organization)}
            ).status_code
            == 404
        )
        app.dependency_overrides[current_principal] = lambda: Principal("test", "test", frozenset())
        service.reset_mock()
        assert client.get("/v1/catalog/products", params=query).status_code == 403
        assert client.get("/v1/catalog/providers", params=query).status_code == 403
        service.assert_not_called()


@pytest.mark.enable_socket
def test_catalog_versions_pagination_and_concealment():
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    tenant, organization, product = uuid4(), uuid4(), uuid4()
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"dataset:read"})
    )
    versions = [
        ProductVersion(
            id=uuid4(),
            tenant_id=tenant,
            product_id=product,
            source_version=str(i),
            metadata_digest="sha256:" + "a" * 64,
            native_variable="sst",
            standard_name="sea_surface_temperature",
            physical_definition="test-only SST",
            native_units="K",
            calendar="gregorian",
            retrieved_at="2025-02-01T00:00:00Z",
            license={},
        )
        for i in range(2)
    ]
    with (
        patch("orca.api.routers.catalog.engine"),
        patch("orca.api.routers.catalog.resolve_tenant", return_value=(tenant, {"researcher"})),
        patch("orca.api.routers.catalog.TenantUnitOfWork"),
        patch("orca.api.routers.catalog.CatalogService") as service,
        TestClient(app) as client,
    ):
        path = f"/v1/catalog/products/{product}/versions"
        query = {"organization_id": str(organization), "limit": 1}
        service.return_value.versions.return_value = versions
        response = client.get(path, params=query)
        assert response.status_code == 200
        assert response.json()["next_cursor"] == str(versions[0].id)
        assert len(response.json()["items"]) == 1
        service.return_value.versions.return_value = []
        variable_path = f"/v1/catalog/products/{product}/variables"
        assert client.get(variable_path, params=query).json()["items"] == []
        service.return_value.versions.return_value = versions
        variable_response = client.get(variable_path, params=query)
        assert variable_response.status_code == 200
        assert variable_response.json()["items"][0]["version_id"] == str(versions[0].id)
        assert variable_response.json()["items"][0]["depth_m"] is None
        assert variable_response.json()["next_cursor"] == str(versions[0].id)
        assert client.get(variable_path, params=query | {"kind": "unsupported"}).status_code == 422
        service.return_value.versions.return_value = []
        response = client.get(path, params=query | {"cursor": str(versions[0].id)})
        assert response.json()["next_cursor"] is None
        service.return_value.versions.assert_called_with(product, after=versions[0].id, limit=2)
        assert client.get(path, params=query | {"cursor": "invalid"}).status_code == 422
        service.return_value.product.return_value = None
        service.return_value.versions.reset_mock()
        assert client.get(path, params=query).status_code == 404
        service.return_value.versions.assert_not_called()

        assert client.get(variable_path, params=query).status_code == 404
        service.return_value.versions.assert_not_called()

        service.return_value.product.return_value = True
        evidence_query = {"organization_id": str(organization)}
        coverage_path = f"/v1/catalog/products/{product}/coverage"
        license_path = f"/v1/catalog/products/{product}/licenses"
        service.return_value.versions.return_value = []
        response = client.get(coverage_path, params=evidence_query)
        assert response.status_code == 200 and response.json()["coverage"] is None
        assert not response.json()["valid_observation_coverage_verified"]
        assert not client.get(license_path, params=evidence_query).json()["can_preserve"]
        assert (
            client.get(coverage_path, params=evidence_query | {"version": str(uuid4())}).status_code
            == 404
        )
        service.return_value.versions.return_value = versions
        assert client.get(coverage_path, params=evidence_query).status_code == 409
        assert client.get(license_path, params=evidence_query).status_code == 409
        service.return_value.versions.return_value = versions[:1]
        response = client.get(
            coverage_path, params=evidence_query | {"version": str(versions[0].id)}
        )
        assert response.status_code == 200 and response.json()["version_id"] == str(versions[0].id)
        service.return_value.versions.assert_called_with(
            product, version_id=versions[0].id, limit=2
        )
        service.return_value.product.return_value = None
        service.return_value.versions.reset_mock()
        assert client.get(license_path, params=evidence_query).status_code == 404
        service.return_value.versions.assert_not_called()


def test_catalog_version_manifest_identity_integrity():
    tenant, product, version_id = uuid4(), uuid4(), uuid4()
    manifest = ProductVersion(
        id=version_id,
        tenant_id=tenant,
        product_id=product,
        source_version="v1",
        metadata_digest="sha256:" + "a" * 64,
        native_variable="sst",
        standard_name="sea_surface_temperature",
        physical_definition="test-only SST",
        native_units="K",
        calendar="gregorian",
        retrieved_at="2025-02-01T00:00:00Z",
        license={},
    )
    session = MagicMock()
    row = {
        field: getattr(manifest, field)
        for field in ("id", "tenant_id", "product_id", "source_version", "metadata_digest")
    }
    row["manifest"] = manifest.model_dump(mode="json")
    session.execute.return_value.mappings.return_value = [row]
    service = CatalogService(session, tenant)
    assert service.versions(product) == [manifest]
    row["tenant_id"] = uuid4()
    with pytest.raises(ValueError, match="disagrees"):
        service.versions(product)

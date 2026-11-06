"""Purpose: exercise scope, tenant and unknown-result behavior for P03 metadata checks.
Behavior: inject test-only persistence and membership at the HTTP boundary.
Refs: API 3.4; main plan P03.5; Phase: P03.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.contracts.catalog import ProductVersion
from orca.domain.identity import Principal


@pytest.mark.enable_socket
def test_fitness_http_scope_and_product_binding():
    tenant, product, identifier, org = uuid4(), uuid4(), uuid4(), uuid4()
    app = create_app(Settings(monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz"))
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"research:plan"})
    )
    body = {
        "organization_id": str(org),
        "method_id": "sst",
        "version_ref": str(identifier),
        "geo_scope": {"type": "Point", "geometry": {"type": "Point", "coordinates": [68.5, 20.5]}},
        "time_window": {"start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"},
    }
    version = ProductVersion(
        id=identifier,
        tenant_id=tenant,
        product_id=product,
        source_version="test",
        metadata_digest="sha256:" + "a" * 64,
        native_variable="sst",
        standard_name="sea_surface_temperature",
        physical_definition="test",
        native_units="K",
        calendar="gregorian",
        retrieved_at="2025-02-01T00:00:00Z",
        license={},
    )
    session = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = version.model_dump(mode="json")
    with (
        patch("orca.api.routers.fitness.engine"),
        patch("orca.api.routers.fitness.resolve_tenant", return_value=(tenant, {"researcher"})),
        patch("orca.api.routers.fitness.TenantUnitOfWork") as uow,
    ):
        uow.return_value.__enter__.return_value = session
        client = TestClient(app)
        result = client.post(f"/v1/catalog/products/{product}/fitness-checks", json=body)
        assert result.status_code == 200 and result.json()["status"] == "incompatible"
        compatibility = {
            "organization_id": str(org),
            "method_id": "sst",
            "products": [str(identifier)],
            "baseline_ref": str(identifier),
            "geo_scope": body["geo_scope"],
            "time_window": body["time_window"],
        }
        result = client.post("/v1/catalog/compatibility-checks", json=compatibility)
        assert (
            result.status_code == 200 and result.json()["scientific_validation"] == "not_validated"
        )
        assert result.json()["status"] == "incompatible"
        assert (
            client.post(f"/v1/catalog/products/{uuid4()}/fitness-checks", json=body).status_code
            == 404
        )
        session.execute.return_value.scalar_one_or_none.return_value = None
        assert (
            client.post(f"/v1/catalog/products/{product}/fitness-checks", json=body).status_code
            == 404
        )
        app.dependency_overrides[current_principal] = lambda: Principal("test", "test", frozenset())
        assert (
            client.post(f"/v1/catalog/products/{product}/fitness-checks", json=body).status_code
            == 403
        )

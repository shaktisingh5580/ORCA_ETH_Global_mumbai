"""Purpose: verify P04 science routes expose only approved metadata and scope-bound checks.
Behavior: test-only registries and catalog versions exercise list, detail, validation and denial paths.
Refs: main_implementation plan.md P04; api design.md §3.5; Phase: P04.
"""

import secrets
from contextlib import contextmanager
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.contracts.catalog import LicenseProfile, ProductVersion
from orca.contracts.science_api import QualityCodebook, ScientificPolicySummary
from orca.domain.identity import Principal
from orca.science.registry import MethodRegistry, MethodVersion

DIGEST = "a" * 64
QC_DIGEST = "c" * 64
ORG = uuid4()


def method(status: str = "approved") -> MethodVersion:
    return MethodVersion(
        method_id="test.sst",
        version="1.0.0",
        code_digest=DIGEST,
        approval_status=status,
        required_baseline_years=2,
        measurement_semantics="sea_surface_foundation_temperature",
        qc_policy_id="test-qc",
        qc_policy_digest=QC_DIGEST,
    )


def policy() -> ScientificPolicySummary:
    return ScientificPolicySummary(
        method_id="test.sst",
        version="1.0.0",
        qc_policy_id="test-qc",
        qc_policy_digest=QC_DIGEST,
        threshold_status="approved",
        minimum_spatial_coverage=0.8,
        minimum_temporal_coverage=0.9,
        minimum_valid_samples=20,
        numerical_tolerance_celsius=1e-9,
        rationale="Test-only independent policy fixture",
    )


@contextmanager
def client(*, role: str = "researcher", registry: MethodRegistry | None = None):
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    app.state.science_registry = registry or MethodRegistry()
    app.dependency_overrides[current_principal] = lambda: Principal(
        "subject", "issuer", frozenset({"dataset:read", "research:plan"})
    )
    with (
        patch("orca.api.routers.science.engine"),
        patch("orca.api.routers.science.resolve_tenant", return_value=(ORG, {role})),
        TestClient(app) as http,
    ):
        yield http


def recipe() -> dict:
    return {
        "method_id": "test.sst",
        "method_version": "1.0.0",
        "method_digest": DIGEST,
        "current_qc_policy_id": "test-qc",
        "current_qc_policy_digest": QC_DIGEST,
        "baseline_qc_policy_id": "test-qc",
        "baseline_qc_policy_digest": QC_DIGEST,
        "as_of": "2025-02-01T00:00:00Z",
        "current_start": "2025-01-01T00:00:00Z",
        "current_end": "2025-02-01T00:00:00Z",
        "calendar": "gregorian",
        "baseline_years": [2003, 2004],
        "current_season": "gregorian-month:01",
        "baseline_season": "gregorian-month:01",
        "current_measurement": "sea_surface_foundation_temperature",
        "baseline_measurement": "sea_surface_foundation_temperature",
        "current_depth_m": 0.0,
        "baseline_depth_m": 0.0,
        "current_unit": "degree_C",
        "baseline_unit": "degree_Celsius",
    }


def request_body() -> dict:
    return {
        "organization_id": str(ORG),
        "recipe": recipe(),
        "selected_sources": {
            "current_version_id": str(uuid4()),
            "baseline_version_id": str(uuid4()),
        },
    }


@pytest.mark.enable_socket
def test_science_routes_fail_closed_before_review():
    with client() as http:
        query = {"organization_id": str(ORG)}
        for path in ("methods", "recipe-templates"):
            response = http.get(f"/v1/science/{path}", params=query)
            assert response.status_code == 200
            assert response.json()["items"] == []
        for path, filters in (
            ("methods/test.sst", {}),
            ("methods/test.sst/versions/1.0.0", {}),
            ("recipe-templates/test.sst@1.0.0", {}),
            ("policies", {"method_id": "test.sst", "version": "1.0.0"}),
            ("quality-codebook", {"source_id": "mur", "variable": "sst"}),
        ):
            assert http.get(f"/v1/science/{path}", params=query | filters).status_code == 404
        response = http.post("/v1/science/recipe-validations", json=request_body())
        assert response.status_code == 200
        assert response.json()["status"] == "invalid"
        assert response.json()["scientific_finding"] is False


@pytest.mark.enable_socket
def test_recipe_validation_requires_reviewed_policy_and_allows_same_product_version():
    body = request_body()
    body["selected_sources"]["baseline_version_id"] = body["selected_sources"]["current_version_id"]
    with client(registry=MethodRegistry((method(),))) as http:
        response = http.post("/v1/science/recipe-validations", json=body)
        assert response.status_code == 200
        assert response.json()["status"] == "invalid"
        assert response.json()["checks"][0]["name"] == "reviewed_policy"


@pytest.mark.enable_socket
def test_science_routes_require_tenant_role_and_exact_query():
    query = {"organization_id": str(ORG)}
    with client(role="viewer") as http:
        assert http.get("/v1/science/methods", params=query).status_code == 403
        assert http.post("/v1/science/recipe-validations", json=request_body()).status_code == 403
    with client() as http:
        assert http.get("/v1/science/methods", params=query | {"unknown": "1"}).status_code == 422
        assert http.get("/v1/science/methods", params=query | {"cursor": "!!!"}).status_code == 422
        assert (
            http.post(
                "/v1/science/recipe-validations", json=request_body() | {"extra": 1}
            ).status_code
            == 422
        )


@pytest.mark.enable_socket
def test_approved_science_views_are_pinned_and_paginated():
    registry = MethodRegistry((method("draft"),))
    with client(registry=registry) as http:
        assert (
            http.get("/v1/science/methods", params={"organization_id": str(ORG)}).json()["items"]
            == []
        )
    registry = MethodRegistry((method(),))
    with client(registry=registry) as http:
        http.app.state.science_policies[("test.sst", "1.0.0")] = policy()
        http.app.state.science_codebooks[("mur", "sst")] = QualityCodebook(
            source_id="mur",
            variable="sst",
            policy_id="test-qc",
            policy_digest=QC_DIGEST,
            flags={"1": "eligible open sea"},
        )
        query = {"organization_id": str(ORG)}
        listing = http.get("/v1/science/methods", params=query).json()
        assert listing["items"][0]["code_digest"] == DIGEST
        assert listing["next_cursor"] is None
        assert (
            http.get("/v1/science/methods/test.sst", params=query).json()["approved_versions"][0][
                "version"
            ]
            == "1.0.0"
        )
        assert (
            http.get("/v1/science/methods/test.sst/versions/1.0.0", params=query).json()[
                "qc_policy_digest"
            ]
            == QC_DIGEST
        )
        templates = http.get("/v1/science/recipe-templates", params=query).json()
        assert templates["items"][0]["source_compatibility_verified"] is False
        assert (
            http.get("/v1/science/recipe-templates/test.sst@1.0.0", params=query).status_code == 200
        )
        assert (
            http.get(
                "/v1/science/policies", params=query | {"method_id": "test.sst", "version": "1.0.0"}
            ).json()["minimum_valid_samples"]
            == 20
        )
        assert http.get(
            "/v1/science/quality-codebook", params=query | {"source_id": "mur", "variable": "sst"}
        ).json()["flags"] == {"1": "eligible open sea"}
        http.app.state.science_policies[("test.sst", "1.0.0")] = http.app.state.science_policies[
            ("test.sst", "1.0.0")
        ].model_copy(update={"qc_policy_digest": "f" * 64})
        assert (
            http.get(
                "/v1/science/policies", params=query | {"method_id": "test.sst", "version": "1.0.0"}
            ).status_code
            == 404
        )


def product(tenant_id, version_id, *, native_units: str) -> ProductVersion:
    return ProductVersion(
        id=version_id,
        tenant_id=tenant_id,
        product_id=uuid4(),
        source_version="test-only",
        metadata_digest="sha256:" + "b" * 64,
        native_variable="sst",
        standard_name="sea_surface_foundation_temperature",
        physical_definition="sea_surface_foundation_temperature",
        native_units=native_units,
        depth_m=0.0,
        calendar="gregorian",
        available_at="2025-01-31T00:00:00Z",
        retrieved_at="2025-02-01T00:00:00Z",
        license=LicenseProfile(
            license_ref="test-rights", evidence_digest="sha256:" + "d" * 64, can_preserve=True
        ),
    )


@pytest.mark.enable_socket
def test_recipe_validation_checks_stored_source_metadata_without_scientific_claim():
    body = request_body()
    ids = body["selected_sources"]
    current = product(ORG, ids["current_version_id"], native_units="degree_C")
    baseline = product(ORG, ids["baseline_version_id"], native_units="degree_Celsius")
    with (
        client(registry=MethodRegistry((method(),))) as http,
        patch("orca.api.routers.science.TenantUnitOfWork") as uow,
        patch("orca.api.routers.fitness.version", side_effect=[current, baseline]),
    ):
        http.app.state.science_policies[("test.sst", "1.0.0")] = policy()
        uow.return_value.__enter__.return_value = object()
        response = http.post("/v1/science/recipe-validations", json=body)
        assert response.status_code == 200
        assert response.json()["status"] == "requires_source_validation"
        assert response.json()["scientific_finding"] is False


@pytest.mark.enable_socket
def test_recipe_validation_rejects_incompatible_baseline():
    body = request_body()
    ids = body["selected_sources"]
    current = product(ORG, ids["current_version_id"], native_units="degree_C")
    baseline = product(ORG, ids["baseline_version_id"], native_units="degree_Celsius")
    baseline = baseline.model_copy(update={"physical_definition": "different SST semantics"})
    with (
        client(registry=MethodRegistry((method(),))) as http,
        patch("orca.api.routers.science.TenantUnitOfWork") as uow,
        patch("orca.api.routers.fitness.version", side_effect=[current, baseline]),
    ):
        http.app.state.science_policies[("test.sst", "1.0.0")] = policy()
        uow.return_value.__enter__.return_value = object()
        response = http.post("/v1/science/recipe-validations", json=body)
        assert response.status_code == 200
        assert response.json()["status"] == "incompatible"
        assert response.json()["scientific_finding"] is False


def test_openapi_registers_all_eight_p04_operations():
    app = create_app(
        Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    )
    actual = app.openapi()["paths"]
    required = {
        ("get", "/v1/science/methods"),
        ("get", "/v1/science/methods/{method_id}"),
        ("get", "/v1/science/methods/{method_id}/versions/{version}"),
        ("get", "/v1/science/policies"),
        ("get", "/v1/science/quality-codebook"),
        ("get", "/v1/science/recipe-templates"),
        ("get", "/v1/science/recipe-templates/{template_id}"),
        ("post", "/v1/science/recipe-validations"),
    }
    assert all(method in actual[path] for method, path in required)

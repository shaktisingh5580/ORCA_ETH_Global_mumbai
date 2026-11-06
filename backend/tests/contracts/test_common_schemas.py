"""Purpose: protect common wire contracts against coercion and scientific ambiguity.
Behavior: cover valid serialization plus invalid time, geometry, identity and money inputs.
Refs: main plan P01.2; API sections 2.4-2.6; Phase: P01.
"""

import secrets
from datetime import datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from orca.api.app import create_app
from orca.api.errors import Problem
from orca.config import Settings
from orca.contracts.common import (
    Capability,
    GeoScope,
    IdentityReference,
    MoneyBudget,
    Page,
    Polygon,
    ScientificOutcome,
    ScientificQuantity,
    SourceClock,
    TimeWindow,
)


def test_uuid_and_id_serialization():
    data = {"id": "00000000-0000-4000-8000-000000000001", "evidence_id": "ev_legacy:abc"}
    model = IdentityReference.model_validate(data)
    assert isinstance(model.id, UUID)
    assert model.model_dump(mode="json") == data
    for invalid in ("", "not-uuid", None, 42):
        with pytest.raises(ValidationError):
            IdentityReference.model_validate(dict(data, id=invalid))
    for invalid in ("", "has whitespace", 12, None):
        with pytest.raises(ValidationError):
            IdentityReference.model_validate(dict(data, evidence_id=invalid))


def test_timezone_required_utc():
    model = TimeWindow(start="2025-01-01T05:30:00+05:30", end="2025-01-02T00:00:00Z")
    assert model.model_dump(mode="json")["start"] == "2025-01-01T00:00:00Z"
    for invalid in ("2025-01-01", "2025-01-01T00:00:00", datetime(2025, 1, 1), 1735689600, None):
        with pytest.raises(ValidationError):
            TimeWindow(start=invalid, end="2025-01-02T00:00:00Z")
    with pytest.raises(ValidationError):
        TimeWindow(start="2025-01-01T00:00:00Z", end="2025-01-01T00:00:00Z")


def test_geojson_lon_lat_bounds():
    ring = [[70, 10], [71, 10], [71, 11], [70, 10]]
    assert Polygon(type="Polygon", coordinates=[ring]).model_dump(mode="json")["coordinates"] == [
        ring
    ]
    for bad in (
        [[181, 10], *ring[1:]],
        [[70, 91], *ring[1:]],
        [[float("nan"), 10], *ring[1:]],
        [[float("inf"), 10], *ring[1:]],
        ring[:-1],
        [[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]],
        [[179, 1], [-179, 1], [-179, 2], [179, 1]],
        [["70", 10], *ring[1:]],
    ):
        with pytest.raises(ValidationError):
            Polygon(type="Polygon", coordinates=[bad])
    for kind, coordinates in (("Point", [70, 10]), ("Polygon", [ring]), ("MultiPolygon", [[ring]])):
        assert (
            GeoScope(type=kind, geometry={"type": kind, "coordinates": coordinates}).crs
            == "EPSG:4326"
        )
    with pytest.raises(ValidationError):
        GeoScope(type="Polygon", geometry={"type": "Point", "coordinates": [70, 10]})


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        MoneyBudget(max_amount="1", invented_column=True)
    for data in (
        {"items": []},
        {"items": None, "next_cursor": None},
        {"items": [], "next_cursor": None, "page_size": 0},
    ):
        with pytest.raises(ValidationError):
            Page[str].model_validate(data)
    assert Page[str](items=[], next_cursor=None).items == []


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_problem_9457_stable_codes():
    token = secrets.token_urlsafe(48)
    app = create_app(Settings(monitoring_token=SecretStr(token)))

    # Test-only route exercises the real exception handler without shipping a stub.
    @app.post("/validation-probe")
    def probe(body: MoneyBudget):
        return body

    with TestClient(app) as client:
        response = client.get("/readyz")
        assert response.status_code == 401
        assert response.headers["content-type"] == "application/problem+json"
        body = Problem.model_validate(response.json())
        assert body.code == "UNAUTHENTICATED"
        assert body.request_id == response.headers["x-request-id"]
        invalid = client.post("/validation-probe", json={"max_amount": token})
        assert invalid.status_code == 422 and invalid.json()["code"] == "INVALID_FIELD"
        assert token not in invalid.text


def test_money_decimal_string():
    assert MoneyBudget(max_amount="0.12345678").model_dump()["max_amount"] == "0.12345678"
    for invalid in (0.1, 12, "NaN", "Infinity", "-1", "1e3", "01", "0.123456789", None, ""):
        with pytest.raises(ValidationError):
            MoneyBudget(max_amount=invalid)
    with pytest.raises(ValidationError):
        MoneyBudget(max_amount="1", spent_amount="0.8", reserved_amount="0.3")


def test_unknown_science_and_timestamps_are_explicit():
    assert ScientificQuantity(value=None, unit="K", status="unknown").value is None
    for data in (
        {"value": 0, "unit": "K", "status": "unknown"},
        {"value": None, "unit": "K", "status": "known"},
        {"value": float("nan"), "unit": "K", "status": "known"},
    ):
        with pytest.raises(ValidationError):
            ScientificQuantity.model_validate(data)
    with pytest.raises(ValidationError):
        ScientificOutcome(status="supported", claim_ids=[], artifact_ids=[], limitations=[])
    names = ("observed_at", "valid_at", "issued_at", "published_at", "available_at", "retrieved_at")
    clock = dict.fromkeys(names)
    with pytest.raises(ValidationError):
        SourceClock(**clock, availability_status="unavailable")
    assert (
        SourceClock(
            **clock,
            availability_status="unavailable",
            missing_reasons={n: "Not provided by source" for n in names},
        ).observed_at
        is None
    )
    with pytest.raises(ValidationError):
        Capability(name="science", live_verified=True)

"""Purpose: verify P08 readiness and liveness do not overclaim release health.
Behavior: check authenticated probes, science gating, database failures, and secret redaction.
Refs: main_implementation plan.md P08.1; api design.md §§3.1, 14; Phase: P08.
"""

import secrets

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.config import Settings


@pytest.mark.enable_socket
def test_readiness_503_if_science_incomplete() -> None:
    token = secrets.token_urlsafe(48)
    app = create_app(Settings(monitoring_token=SecretStr(token), environment="test"))

    class BrokenDatabase:
        def connect(self):
            raise RuntimeError("private database password must never be returned")

    app.state.runtime_engine = BrokenDatabase()
    with TestClient(app) as client:
        response = client.get("/readyz", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json()["components"]["database"] == "unavailable"
    assert response.json()["components"]["approved_science"] == "unavailable"
    assert "private database password" not in response.text
    assert token not in response.text


@pytest.mark.enable_socket
def test_liveness_does_not_claim_readiness() -> None:
    token = secrets.token_urlsafe(48)
    app = create_app(Settings(monitoring_token=SecretStr(token), environment="test"))
    with TestClient(app) as client:
        live = client.get("/livez")
        ready = client.get("/readyz", headers={"Authorization": f"Bearer {token}"})
    assert live.status_code == 200
    assert live.json() == {"status": "alive"}
    assert ready.status_code == 503
    assert ready.json()["components"]["database"] == "not_implemented"
    assert ready.json()["components"]["approved_science"] == "unavailable"


@pytest.mark.enable_socket
def test_database_probe_alone_does_not_clear_science_gate() -> None:
    token = secrets.token_urlsafe(48)
    app = create_app(Settings(monitoring_token=SecretStr(token), environment="test"))

    class Database:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def connect(self):
            return self

        def execute(self, statement: object) -> None:
            assert str(statement) == "SELECT 1"

    app.state.runtime_engine = Database()
    with TestClient(app) as client:
        response = client.get("/readyz", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 503
    assert response.json()["components"]["database"] == "ready"
    assert response.json()["components"]["approved_science"] == "unavailable"

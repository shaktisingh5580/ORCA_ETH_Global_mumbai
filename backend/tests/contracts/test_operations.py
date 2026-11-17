"""Purpose: verify P08 readiness and liveness do not overclaim release health.
Behavior: check authenticated probes, science gating, database failures, and secret redaction.
Refs: main_implementation plan.md P08.1; api design.md §§3.1, 14; Phase: P08.
"""

import secrets
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.api.routers import operations
from orca.config import Settings
from orca.domain.identity import Principal


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


@pytest.mark.enable_socket
def test_ops_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    organization_id, tenant = uuid4(), uuid4()
    principal = Principal("operator", "test", frozenset())
    app.dependency_overrides[current_principal] = lambda: principal
    monkeypatch.setattr(operations, "engine", lambda *_args: object())
    roles = {"org_owner"}
    monkeypatch.setattr(operations, "resolve_tenant", lambda *_args: (tenant, roles))

    class Result:
        def __init__(self, values: dict[str, int]) -> None:
            self.values = values

        def mappings(self):
            return self

        def one(self) -> dict[str, int]:
            return self.values

    class Session:
        def execute(self, statement: object, params: dict[str, object]) -> Result:
            assert params["tenant"] == tenant
            if "FROM research.job_queue" in str(statement):
                return Result(dict(queued=2, leased=1, overdue_leases=1, completed=3, cancelled=0))
            assert "FROM research.job_attempts" in str(statement)
            return Result(dict(expired_attempts_since=1, retry_attempts_since=2))

    class UnitOfWork:
        def __init__(self, *_args: object) -> None:
            pass

        def __enter__(self) -> Session:
            return Session()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(operations, "TenantUnitOfWork", UnitOfWork)
    with TestClient(app) as client:
        path = f"/v1/admin/job-health?organization_id={organization_id}"
        denied = client.get(path)
        assert denied.status_code == 403
        app.dependency_overrides[current_principal] = lambda: Principal(
            "operator", "test", frozenset({"ops:read"})
        )
        roles = {"viewer"}
        assert client.get(path).status_code == 403
        roles = {"org_owner"}
        allowed = client.get(path)
        assert allowed.status_code == 200
        assert allowed.json()["queued"] == 2
        assert allowed.json()["expired_attempts_since"] == 1
        assert allowed.json()["fencing_failures"] is None
        assert allowed.json()["worker_status"] == "unknown"
        assert client.get(path + "&queue=another").status_code == 422

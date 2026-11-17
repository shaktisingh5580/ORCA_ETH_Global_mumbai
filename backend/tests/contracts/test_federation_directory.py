"""Purpose: verify P09 directory scope and no accidental research trust escalation.
Behavior: test a qualified service, expired proof and role/token authorization.
Refs: main_implementation plan.md P09.1; api design.md §3.10; Phase: P09.
"""

import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.api.routers import federation
from orca.config import Settings
from orca.domain.identity import Principal


@pytest.mark.enable_socket
def test_untrusted_capability_cannot_be_automatically_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    organization, tenant, agent = uuid4(), uuid4(), uuid4()
    roles = {"org_owner"}
    app.dependency_overrides[current_principal] = lambda: Principal(
        "owner", "test", frozenset({"network:read"})
    )
    monkeypatch.setattr(federation, "engine", lambda *_args: object())
    monkeypatch.setattr(federation, "resolve_tenant", lambda *_args: (tenant, roles))
    expires = datetime.now(timezone.utc) + timedelta(hours=1)
    row = dict(
        id=agent,
        organization_id=organization,
        state="active",
        created_at=datetime.now(timezone.utc),
        registration=dict(
            name="Ocean service",
            capability="temperature_summary_v1",
            operator_mode="self_declared_external",
        ),
        qualification_expires_at=expires,
    )

    class Result:
        def mappings(self):
            return self

        def all(self):
            return [row]

    class Session:
        def execute(self, statement: object, params: dict[str, object]) -> Result:
            assert "federation.registrations" in str(statement)
            assert params["tenant"] == tenant
            return Result()

    class UnitOfWork:
        def __init__(self, *_args: object) -> None:
            pass

        def __enter__(self) -> Session:
            return Session()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(federation, "TenantUnitOfWork", UnitOfWork)
    with TestClient(app) as client:
        url = f"/v1/network/agents?organization_id={organization}"
        allowed = client.get(url)
        assert allowed.status_code == 200
        item = allowed.json()["items"][0]
        assert item["service_eligible"] is True
        assert item["ens_verified"] is False
        assert item["institutional_affiliation_verified"] is False
        assert item["research_dispatch_enabled"] is False
        assert client.get(url + "&verified_only=true").json()["items"] == []
        row["qualification_expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert client.get(url).json()["items"][0]["service_eligible"] is False
        roles = {"viewer"}
        assert client.get(url).status_code == 403
        roles = {"org_owner"}
        app.dependency_overrides[current_principal] = lambda: Principal(
            "owner", "test", frozenset()
        )
        assert client.get(url).status_code == 403

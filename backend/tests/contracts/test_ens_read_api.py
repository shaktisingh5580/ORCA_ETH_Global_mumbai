"""Purpose: verify P10 ENS observation scope and fail-closed identity semantics.
Behavior: deny unscoped reads, preserve inactive state, and surface retryable lookup failure.
Refs: main_implementation plan.md P10.4; api design.md §3.11; Phase: P10.
"""

import secrets
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.api.routers import ens
from orca.config import Settings
from orca.domain.identity import Principal


@pytest.mark.enable_socket
def test_unverified_identity_not_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    organization = uuid4()
    roles = {"org_owner"}
    app.dependency_overrides[current_principal] = lambda: Principal(
        "owner", "test", frozenset({"identity:manage"})
    )
    monkeypatch.setattr(ens, "engine", lambda *_args: object())
    monkeypatch.setattr(ens, "resolve_tenant", lambda *_args: (uuid4(), roles))
    monkeypatch.setattr(
        ens,
        "inspect_name",
        lambda name: {
            "name": name,
            "status": "NAME_OBSERVED_NOT_AUTHORIZED",
            "records": {"org.orca.endpoint": "https://example.org/a2a"},
            "block_number": "123",
            "block_hash": "0xabc",
            "hierarchy": [{"owner": "0xabc", "resolver": "0xdef"}],
            "authority_verified": False,
        },
    )
    with TestClient(app) as client:
        url = "/v1/identity/ens/resolutions"
        body = {
            "organization_id": str(organization),
            "name": "analyst.orca-research-lab.eth",
            "ens_chain_id": 11155111,
        }
        allowed = client.post(url, json=body)
        assert allowed.status_code == 200
        assert allowed.json()["observed_block"] == 123
        assert allowed.json()["finalized"] is True
        assert allowed.json()["name_active"] is True
        assert allowed.json()["controller"] == "0xabc"
        assert allowed.json()["authority_verified"] is False
        assert allowed.json()["research_dispatch_enabled"] is False
        assert client.post(url, json={**body, "ens_chain_id": 1}).status_code == 422
        roles = {"viewer"}
        assert client.post(url, json=body).status_code == 403
        roles = {"org_owner"}
        monkeypatch.setattr(
            ens,
            "inspect_name",
            lambda _name: (_ for _ in ()).throw(
                RuntimeError("ENS_READ_UNAVAILABLE_OR_UNSUPPORTED")
            ),
        )
        failed = client.post(url, json=body)
        assert failed.status_code == 503
        assert failed.headers["retry-after"] == "15"

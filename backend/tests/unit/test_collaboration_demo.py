"""Purpose: keep the accelerated collaboration walkthrough explicitly simulated.
Behavior: check its normal HTTP entrypoint and source/export disclosure labels.
Refs: ORCA_DEMO_FIRST_PLAN.md; Phase: accelerated demonstration test tooling.
"""

import secrets

import demo_bootstrap
import pytest
from fastapi.testclient import TestClient

from orca.config import Settings


@pytest.mark.enable_socket
def test_simulation_route_and_disclosure(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCA_DEMO_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        demo_bootstrap,
        "Settings",
        lambda: Settings(monitoring_token=secrets.token_urlsafe(40)),
    )
    with TestClient(demo_bootstrap.create_application()) as client:
        response = client.get("/demo")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "SIMULATED COLLABORATION DEMO" in response.text
        assert "signatures:'simulated'" in response.text
        assert "chain_publication:'simulated'" in response.text
        assert 'href="/research"' in response.text

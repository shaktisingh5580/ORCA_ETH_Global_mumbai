"""Purpose: verify same-origin workspace delivery and browser-facing configuration boundaries.
Behavior: ensure no model/database/signing secrets leak and the dashboard is prototype-only.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A05; Phase: A05 dashboard increment.
"""

import pytest
from fastapi.testclient import TestClient

from orca.api.app import create_app
from orca.config import Settings


@pytest.mark.enable_socket
def test_workspace_assets_and_public_configuration():
    settings = Settings(
        monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz",
        prototype_enabled=True,
        gemini_api_key="private-model-test-value",
        database_url="postgresql+psycopg://runtime:test@127.0.0.1:55433/orca_ui_test",
        object_store_endpoint="http://127.0.0.1:59000",
        object_store_access_key="test-only",
        object_store_secret_key="test-only",
        prototype_bundle_digest="sha256:" + "a" * 64,
        prototype_source_tenant="00000000-0000-0000-0000-000000000001",
    )
    with TestClient(create_app(settings)) as client:
        page = client.get("/research")
        assert page.status_code == 200
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert 'id="timeline"' in page.text and 'id="publish-consent"' in page.text
        config = client.get("/research/config")
        assert config.json()["reasoning_configured"] is True
        assert "private-model-test-value" not in config.text
        assert "monitoring_token" not in config.text
        assert 'id="collaboration"' in page.text
        assert "Onboard agent" in page.text
        assert 'id="agent-detail"' in page.text
        script = client.get("/research/assets/workspace.js")
        assert script.status_code == 200
        assert "/v1/network/agents?organization_id=" in script.text
        assert "generation !== state.generation" in script.text
        assert client.get("/research/assets/workspace.css").status_code == 200
    with TestClient(
        create_app(Settings(monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz"))
    ) as client:
        assert client.get("/research").status_code == 404

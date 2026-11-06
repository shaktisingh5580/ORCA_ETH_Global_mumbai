"""Purpose: verify organization creation policy and rejection of self-verification fields.
Behavior: exercise HTTP readiness before persistence and strict lifecycle request contracts.
Refs: API 3.2; main plan P02; Phase: P02 repair.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.contracts.organizations import CreateOrganization, PatchOrganization
from orca.domain.identity import Principal


@pytest.mark.enable_socket
def test_organization_self_service_policy_is_explicit():
    app = create_app(Settings(monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz"))
    app.dependency_overrides[current_principal] = lambda: Principal(
        str(uuid4()), "test", frozenset()
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/organizations",
            json={"name": "Test", "slug": "test-org"},
            headers={"Idempotency-Key": "new"},
        )
        assert response.status_code == 503


def test_organization_cannot_self_verify_or_override_security_settings():
    with pytest.raises(ValidationError):
        CreateOrganization.model_validate(
            {"name": "Test", "slug": "test-org", "verification_status": "verified"}
        )
    with pytest.raises(ValidationError):
        PatchOrganization.model_validate({"settings": {"bypass_rls": True}})

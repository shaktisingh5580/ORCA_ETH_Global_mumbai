"""Purpose: verify full-plan P09 onboarding can start without H04 prototype switches.
Behavior: derive separate key identity, expose onboarding routes and reject incomplete configuration.
Refs: main_implementation plan.md P09.1-P09.3; api design.md §3.10; Phase: P09 test tooling.
"""

from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Request
from pydantic import ValidationError

from orca.api.app import create_app
from orca.api.routers.agents import coordinator
from orca.config import Settings
from orca.infrastructure.federation.coordinator import load_coordinator, load_public_coordinator
from orca.infrastructure.federation.signing import b64


def _settings(**overrides: object) -> Settings:
    private = Ed25519PrivateKey.generate()
    configured = {
        "monitoring_token": "0123456789abcdefghijklmnopqrstuvwxyz",
        "database_url": "postgresql+psycopg://runtime:test@127.0.0.1:15432/orca_test",
        "agent_onboarding_enabled": True,
        "federation_coordinator_id": uuid4(),
        "federation_coordinator_key_id": "institution-coordinator",
        "federation_coordinator_public_key": b64(
            private.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
        ),
        "federation_coordinator_private_key": b64(private.private_bytes_raw()),
    }
    configured.update(overrides)
    return Settings(**configured)


def test_full_federation_onboarding_does_not_require_prototype_or_arkiv_wallet() -> None:
    settings = _settings(federation_coordinator_private_key=None)
    identity = load_public_coordinator(settings)
    app = create_app(settings)
    paths = set(app.openapi()["paths"])
    assert not settings.prototype_enabled and not settings.network_enabled
    assert "/v1/organizations/{org_id}/agents" in paths
    assert "/v1/network/institutional-agents" in paths
    assert "/v1/network/config" not in paths
    assert app.state.onboarding_coordinator.public_descriptor() == identity.public_descriptor()
    assert not hasattr(app.state.onboarding_coordinator, "private_key")
    assert coordinator(Request({"type": "http", "app": app})) == identity.public_descriptor()


def test_full_federation_onboarding_requires_separate_signing_identity() -> None:
    with pytest.raises(ValidationError):
        _settings(federation_coordinator_public_key=None)
    with pytest.raises(ValueError, match="32 bytes"):
        create_app(_settings(federation_coordinator_public_key=b64(b"short")))
    with pytest.raises(ValueError, match="differs"):
        load_coordinator(_settings(federation_coordinator_private_key=b64(b"x" * 32)))

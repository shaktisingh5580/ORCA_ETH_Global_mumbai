"""Purpose: reject incomplete official A2A Agent Cards before issuing a key challenge.
Behavior: pin the registered digest yet require the SDK schema, without network or database IO.
Refs: main_implementation plan.md P09.1; A2A 0.3.0; Phase: P09 test tooling.
"""

from uuid import uuid4

import pytest

from orca.application.agent_onboarding import Onboarding
from orca.infrastructure.federation.signing import digest


def test_agent_card_schema_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    malformed = {
        "url": "https://operator.example/a2a",
        "protocolVersion": "0.3.0",
        "skills": [{"id": "temperature_summary_v1"}],
    }
    onboarding = object.__new__(Onboarding)
    monkeypatch.setattr(
        onboarding,
        "expected",
        lambda _identifier, _version: {
            "state": "draft",
            "registration": {
                "card_digest": digest(malformed),
                "endpoint": malformed["url"],
                "capability": "temperature_summary_v1",
            },
        },
    )
    with pytest.raises(ValueError):
        onboarding.challenge(uuid4(), 1, malformed, {})

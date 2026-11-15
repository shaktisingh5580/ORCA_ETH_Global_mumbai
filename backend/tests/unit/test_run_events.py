"""Purpose: protect P05 event redaction boundaries.
Behavior: confirms operational event records never contain tokens, prompts or raw source content.
Refs: main_implementation plan.md P05.4; Phase: P05.
"""

from orca.infrastructure.events.postgres_outbox import redact_payload


def test_redacted_payloads() -> None:
    assert redact_payload(
        {"stage": "leased", "token": "secret", "prompt": "private", "progress": 0.2}
    ) == {"stage": "leased", "progress": 0.2}

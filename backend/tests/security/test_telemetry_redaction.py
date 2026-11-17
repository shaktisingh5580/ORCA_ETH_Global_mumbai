"""Purpose: prove P08 HTTP telemetry excludes secrets and raw request paths.
Behavior: test matched and unmatched requests with credentials in URL and headers.
Refs: main_implementation plan.md P08.1/P08.4; api design.md §14; Phase: P08.
"""

import logging
import secrets

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from orca.api.app import create_app
from orca.config import Settings


@pytest.mark.enable_socket
def test_no_traces_contain_secrets(caplog: pytest.LogCaptureFixture) -> None:
    monitoring = secrets.token_urlsafe(48)
    private = secrets.token_urlsafe(48)
    app = create_app(Settings(monitoring_token=SecretStr(monitoring), environment="test"))
    with caplog.at_level(logging.INFO, logger="orca.telemetry"), TestClient(app) as client:
        live = client.get(
            f"/livez?private={private}", headers={"Authorization": f"Bearer {monitoring}"}
        )
        missing = client.get(f"/unknown/{private}?token={monitoring}")
    assert live.status_code == 200
    assert missing.status_code == 404
    events = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "orca.telemetry"
    )
    assert "route=/livez" in events
    assert "route=unmatched" in events
    assert monitoring not in events
    assert private not in events
    assert "Authorization" not in events

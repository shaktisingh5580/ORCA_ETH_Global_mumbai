"""Purpose: verify source-probe bounds, evidence truth and operator authorization.
Behavior: inject test membership and ensure disabled or unauthorized probes cannot enqueue.
Refs: API 3.4; main plan P03.2/P03.4; Phase: P03.
"""

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from orca.api.app import create_app
from orca.api.dependencies import current_principal
from orca.config import Settings
from orca.contracts.source_probes import ProbeLimits, SourceProbeEvidence
from orca.domain.identity import Principal


def test_probe_limits_and_scientific_truth():
    for values in ({"max_bytes": 1_048_577}, {"timeout_seconds": 21}):
        with pytest.raises(ValidationError):
            ProbeLimits(**values)
    with pytest.raises(ValidationError):
        SourceProbeEvidence(
            response_digest="sha256:" + "a" * 64,
            response_bytes=1,
            retrieved_at="2025-01-01T00:00:00Z",
            metadata_matches_pinned_version=True,
            variable_count=1,
            region_and_time_verified=True,
        )


@pytest.mark.enable_socket
def test_probe_operator_and_readiness_guards():
    app = create_app(Settings(monitoring_token="0123456789abcdefghijklmnopqrstuvwxyz"))
    app.dependency_overrides[current_principal] = lambda: Principal(
        "test", "test", frozenset({"source:manage"})
    )
    body = {
        "organization_id": str(uuid4()),
        "provider_id": "test",
        "distribution_id": str(uuid4()),
        "region": {"type": "Point", "geometry": {"type": "Point", "coordinates": [68.5, 20.5]}},
        "time_window": {"start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z"},
    }
    with (
        patch("orca.api.routers.source_probes.engine"),
        patch(
            "orca.api.routers.source_probes.resolve_tenant",
            return_value=(uuid4(), {"source_operator"}),
        ),
        patch("orca.api.routers.source_probes.SourceProbes") as service,
    ):
        client = TestClient(app)
        path = "/v1/catalog/source-probes"
        assert client.post(path, json=body, headers={"Idempotency-Key": "test"}).status_code == 503
        service.assert_not_called()
        app.dependency_overrides[current_principal] = lambda: Principal("test", "test", frozenset())
        assert client.post(path, json=body, headers={"Idempotency-Key": "test"}).status_code == 403
        assert (
            client.get(
                path + "/" + str(uuid4()), params={"organization_id": body["organization_id"]}
            ).status_code
            == 403
        )

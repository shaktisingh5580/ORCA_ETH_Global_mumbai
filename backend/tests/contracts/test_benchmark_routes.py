"""Purpose: verify P07 benchmark reads expose metadata only and require project authority.
Behavior: inspect actual OpenAPI and the response contract; no reference answers appear.
Refs: main_implementation plan.md P07.4; api design.md §3.8; Phase: P07.
"""

import secrets
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from orca.api.app import create_app
from orca.config import Settings
from orca.contracts.benchmarks import BenchmarkDefinitionView


def test_benchmark_routes_are_authenticated_project_reads() -> None:
    app = create_app(
        Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    )
    paths = app.openapi()["paths"]
    for path in ("/v1/research/benchmarks", "/v1/research/benchmarks/{benchmark_id}"):
        route = paths[path]["get"]
        assert route["security"]
        assert any(
            item["name"] == "project_id" and item["required"] for item in route["parameters"]
        )


def test_benchmark_metadata_rejects_reference_answers() -> None:
    body = dict(
        id=uuid4(),
        project_id=uuid4(),
        title="Observed SST",
        domain="ocean",
        method_id="sst_mean",
        version="1",
        frozen_test_set_digest="sha256:" + "a" * 64,
        reference_policy_digest="sha256:" + "b" * 64,
        reference_kind="independent_observation",
        synthetic=False,
        created_at=datetime.now(timezone.utc),
    )
    assert BenchmarkDefinitionView.model_validate(body).synthetic is False
    with pytest.raises(ValidationError):
        BenchmarkDefinitionView.model_validate(dict(body, held_out_answers=[1.0, 2.0]))

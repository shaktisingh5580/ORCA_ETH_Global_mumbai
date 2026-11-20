"""Purpose: preserve source labels used in sponsor demos.
Behavior: check the real API contract and a persisted, private test-only demonstration.
Refs: main_implementation plan.md P14.4; docs/releases/demo_runbook.md. Phase: P14.
"""

import json
from pathlib import Path
from uuid import uuid4

from disagreement_store import DemoStore
from fastapi import FastAPI
from starlette.requests import Request

from orca.api.routers.prototype import public_config
from orca.config import Settings
from orca.contracts.prototype import PrototypeConfig


def test_synthetic_vs_real_demo_labels(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    receipt = json.loads((root / "docs/gates/HACKATHON/H02-live.json").read_text(encoding="utf-8"))
    app = FastAPI()
    app.state.prototype_settings = Settings(
        environment="test", monitoring_token="test-only-generated-token-0123456789"
    )
    config = PrototypeConfig.model_validate(public_config(Request({"type": "http", "app": app})))
    assert config.source_mode == receipt["source_mode"] == "real"
    assert receipt["scientific_status"] == "prototype_not_independently_validated"
    assert receipt["synthetic_production_data"] is False

    tenant, project = str(uuid4()), str(uuid4())
    database = tmp_path / "demo.sqlite3"
    started = DemoStore(database).create(tenant, project, "operator", "demo-1", False)
    reopened = DemoStore(database).read(started["id"], tenant, project)
    assert reopened["source_mode"] == started["source_mode"] == "synthetic_demo"
    assert reopened["publication_mode"] == "private"
    assert "not observed marine research" in reopened["fixture_notice"]

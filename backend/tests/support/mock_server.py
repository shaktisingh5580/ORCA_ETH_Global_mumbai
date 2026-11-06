"""Purpose: provide separately launched frontend scenarios without production fixtures.
Behavior: serve only labeled synthetic scenarios, validating against runtime DTO schemas.
Refs: main plan P01.3; API section 11; Phase: P01. Excluded from release package.
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from orca.api.errors import Problem
from orca.contracts.common import Page, ResearchProject, ScientificOutcome

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/api/scenarios.json"
TYPES = {
    "ProjectPage": Page[ResearchProject],
    "ScientificOutcome": ScientificOutcome,
    "Problem": Problem,
}


def create_mock_app() -> FastAPI:
    app = FastAPI(title="ORCA TEST-ONLY synthetic frontend harness")
    scenarios = json.loads(FIXTURES.read_text(encoding="utf-8"))["scenarios"]
    for scenario in scenarios.values():
        TYPES[scenario["schema"]].model_validate(scenario["body"])

    @app.get("/scenarios/{name}")
    def scenario(name: str) -> JSONResponse:
        if name not in scenarios:
            raise HTTPException(404, "Unknown test scenario")
        row = scenarios[name]
        return JSONResponse(
            row["body"],
            status_code=row["status"],
            media_type="application/problem+json" if row["status"] >= 400 else "application/json",
            headers={"X-ORCA-Test-Only": "synthetic-fixture"},
        )

    return app

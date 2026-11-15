"""Purpose: verify P05 investigation DTO constraints and bounded request shapes.
Behavior: reject missing scope and preserve explicit ETag-view contracts.
Refs: main_implementation plan.md P05.1; api design.md §§2.5, 3.6; Phase: P05.
"""

import pytest
from pydantic import ValidationError

from orca.contracts.investigations import CreateInvestigation, PatchInvestigation


def payload() -> dict[str, object]:
    return {
        "project_id": "00000000-0000-4000-8000-000000000001",
        "question": "What is the seasonal SST pattern?",
        "geo_scope": {
            "type": "Point",
            "geometry": {"type": "Point", "coordinates": [68.5, 20.5]},
        },
        "time_window": {
            "start": "2025-01-01T00:00:00Z",
            "end": "2025-02-01T00:00:00Z",
        },
        "risk_tier": "research",
    }


def test_create_investigation_requires_explicit_scope_and_risk() -> None:
    parsed = CreateInvestigation.model_validate(payload())
    assert parsed.geo_scope.type == "Point"
    for missing in ("geo_scope", "time_window", "risk_tier"):
        request = payload()
        del request[missing]
        with pytest.raises(ValidationError):
            CreateInvestigation.model_validate(request)


def test_patch_requires_at_least_one_field() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        PatchInvestigation.model_validate({})
    assert PatchInvestigation.model_validate({"description": None}).description is None
    with pytest.raises(ValidationError, match="cannot be cleared"):
        PatchInvestigation.model_validate({"question": None})


def test_create_rejects_question_outside_documented_bounds() -> None:
    request = payload() | {"question": "short"}
    with pytest.raises(ValidationError):
        CreateInvestigation.model_validate(request)

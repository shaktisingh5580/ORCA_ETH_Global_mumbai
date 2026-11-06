"""Purpose: verify model tool boundaries and evidence requirements without live provider calls.
Behavior: scripted model responses are test-only; actual transformations run in the real gateway.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; Phase: A02.
"""

from uuid import UUID

import pytest

from orca.application.reasoning_loop import plan_research

DATASET = str(UUID(int=77))
DATA = {
    DATASET: {
        "name": "Synthetic control",
        "source_mode": "synthetic_demo",
        "format": "csv",
        "content": "time,temp\n2025-01-01T00:00:00Z,27\n",
    }
}
MAPPING = {"timestamp_column": "time", "value_column": "temp", "unit": "C"}
PLAN = {
    "outcome": "supported",
    "recipe": "normalize",
    "summary": "Normalize declared Celsius observations.",
    "datasets": [{"dataset_id": DATASET, "role": "representation", "mapping": MAPPING}],
    "missing_evidence": [],
    "limitations": ["Synthetic test data"],
}


class Model:
    def __init__(self, calls):
        self.calls = iter(calls)

    def turn(self, instruction, contents, tools):
        name, args = next(self.calls)
        return {
            "model": "test-only",
            "content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]},
        }


def test_model_uses_actual_inspection_and_preview_before_plan():
    events = []
    model = Model(
        [
            ("inspect_dataset", {"dataset_id": DATASET}),
            ("preview_mapping", {"dataset_id": DATASET, "mapping": MAPPING}),
            ("submit_plan", PLAN),
        ]
    )
    plan = plan_research(
        "Normalize declared Celsius data", DATA, model, lambda k, d: events.append((k, d))
    )
    assert plan.outcome == "supported"
    preview = next(
        d["result"] for k, d in events if k == "tool_completed" and d["tool"] == "preview_mapping"
    )
    assert preview["preview"][0]["value"] == 27


def test_model_cannot_skip_evidence_or_read_foreign_data():
    events = []
    blocked = {
        **PLAN,
        "outcome": "needs_information",
        "datasets": [],
        "missing_evidence": ["Need approved inputs"],
    }
    model = Model(
        [
            ("submit_plan", PLAN),
            ("inspect_dataset", {"dataset_id": str(UUID(int=99))}),
            ("execute_shell", {"command": "anything"}),
            ("submit_plan", blocked),
        ]
    )
    assert (
        plan_research(
            "Normalize this data", DATA, model, lambda k, d: events.append((k, d))
        ).outcome
        == "needs_information"
    )
    assert sum(k == "tool_rejected" for k, d in events) == 3


def test_tool_budget_is_bounded():
    model = Model([("inspect_dataset", {"dataset_id": DATASET})] * 9)
    with pytest.raises(ValueError, match="MODEL_TURN_BUDGET"):
        plan_research("Normalize data", DATA, model, lambda k, d: None)

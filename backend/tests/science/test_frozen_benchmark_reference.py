"""Purpose: guard P07 held-out reference parsing against duplicated and malformed samples.
Behavior: reject ambiguous IDs, nonfinite truth and unexpected fields before scoring.
Refs: main_implementation plan.md P07.4; api design.md §4.5; Phase: P07.
"""

import json

import pytest

from orca.application.benchmark_evaluation import _reference_pairs


@pytest.mark.parametrize(
    "samples",
    [
        [{"id": "a", "observed": 1}, {"id": "a", "observed": 2}],
        [{"id": "a", "observed": True}],
        [{"id": "a", "observed": float("nan")}],
        [{"id": "a", "observed": 1, "hidden": 2}],
        [],
    ],
)
def test_malformed_reference_cannot_score(samples: list[dict[str, object]]) -> None:
    raw = json.dumps({"schema": "orca.benchmark.reference.v1", "samples": samples}).encode()
    with pytest.raises(ValueError):
        _reference_pairs(raw)


def test_duplicate_json_key_cannot_change_reference_value() -> None:
    raw = (
        b'{"schema":"orca.benchmark.reference.v1","samples":[{"id":"a","observed":1,"observed":2}]}'
    )
    with pytest.raises(ValueError, match="duplicate"):
        _reference_pairs(raw)

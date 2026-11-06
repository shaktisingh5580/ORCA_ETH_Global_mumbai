"""Purpose: verify normalization with explicit synthetic observations and numerical controls.
Behavior: exercise equivalent formats, ambiguity, malformed inputs and row accounting.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A01 gate; Phase: A01.
"""

import json

import pytest
from pydantic import ValidationError

from orca.contracts.normalization import Mapping
from orca.domain.normalization import inspect, normalize, parse


def test_equivalent_csv_json_units_and_original_preserved():
    csv = "time,temp\n2025-01-01T00:00:00Z,27\n2025-01-02T00:00:00Z,28\n"
    original = csv.encode()
    js = json.dumps(
        [
            {"time": "2025-01-01T00:00:00Z", "temp": 300.15},
            {"time": "2025-01-02T00:00:00Z", "temp": 301.15},
        ]
    )
    c = normalize(csv, "csv", Mapping(timestamp_column="time", value_column="temp", unit="C"))
    k = normalize(js, "json", Mapping(timestamp_column="time", value_column="temp", unit="K"))
    assert c == k
    assert [r["value"] for r in c["observations"]] == [27, 28]
    assert csv.encode() == original


def test_units_and_timezone_require_declaration():
    with pytest.raises(ValidationError):
        Mapping(timestamp_column="time", value_column="temp")
    mapping = Mapping(timestamp_column="time", value_column="temp", unit="C")
    content = "time,temp\n2025-01-01,27\n"
    assert not normalize(content, "csv", mapping)["ready_for_analysis"]
    mapping.naive_timezone = "UTC"
    assert normalize(content, "csv", mapping)["ready_for_analysis"]


def test_rejected_duplicates_and_nonfinite_rows_accounted_for():
    content = (
        "time,temp\n2025-01-01T00:00:00Z,27\n2025-01-01T00:00:00Z,28\n2025-01-02T00:00:00Z,NaN\n"
    )
    mapping = Mapping(timestamp_column="time", value_column="temp", unit="C")
    result = normalize(content, "csv", mapping)
    assert result["accepted_rows"] == 1
    assert len(result["rejected_rows"]) == 2
    assert not result["ready_for_analysis"]
    mapping.duplicate_policy = "keep_first"
    result = normalize(content, "csv", mapping)
    assert result["duplicate_rows_skipped"] == [2]
    assert len(result["rejected_rows"]) == 1


@pytest.mark.parametrize(
    "content,format",
    [
        ('[{"a":1,"a":2}]', "json"),
        ('[{"a":NaN}]', "json"),
        ('[{"a":{}}]', "json"),
        ("a,a\n1,2", "csv"),
        ("a,b\n1", "csv"),
        ("a\n1,2", "csv"),
        ("[]", "json"),
        ("{}", "json"),
    ],
)
def test_malformed_input_rejected(content, format):
    with pytest.raises(ValueError):
        parse(content, format)


def test_unknown_columns_and_unpaired_coordinates_rejected():
    with pytest.raises(ValueError):
        normalize(
            "t,v\n2025-01-01Z,1",
            "csv",
            Mapping(timestamp_column="t", value_column="missing", unit="C"),
        )
    assert inspect("t,v\na,1\nb,", "csv")["missing"]["v"] == 1


def test_row_limit():
    with pytest.raises(ValueError):
        parse("t,v\n" + "a,1\n" * 10001, "csv")

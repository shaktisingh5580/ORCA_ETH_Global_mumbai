"""Purpose: distinguish signed agent assertions from consistent numerical reproduction verdicts.
Behavior: require finite comparisons and derive the allowed verdict from the fixed tolerance.
Refs: H04 scientific/transport separation; canonical artifact contract; Phase: H04.
"""

import math
from typing import Any


def validate_comparison(result: dict[str, Any], expected: dict[str, Any]) -> None:
    def number(value: Any) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("finite numeric agent result required")
        return float(value)

    fields = ("current_mean", "baseline_mean", "anomaly")
    differences = [abs(number(result[field]) - number(expected[field])) for field in fields]
    daily = result["daily"]
    if len(daily) != len(expected["daily"]) or [row["date"] for row in daily] != [
        row["date"] for row in expected["daily"]
    ]:
        raise ValueError("agent comparison daily support differs")
    for row, reference in zip(daily, expected["daily"], strict=True):
        differences.extend(abs(number(row[field]) - number(reference[field])) for field in fields)
        if not math.isclose(
            number(row["current_mean"]) - number(row["baseline_mean"]),
            number(row["anomaly"]),
            abs_tol=1e-10,
            rel_tol=0,
        ):
            raise ValueError("agent daily anomaly contradicts its temperatures")
    maximum = max(differences)
    for field in fields:
        if not math.isclose(
            number(result[field]),
            math.fsum(number(row[field]) for row in daily) / len(daily),
            abs_tol=1e-10,
            rel_tol=0,
        ):
            raise ValueError("agent monthly summary contradicts its daily series")
    verdict = "reproduced" if maximum <= 1e-9 else "challenged"
    if (
        result["verdict"] != verdict
        or number(result["absolute_tolerance_celsius"]) != 1e-9
        or not math.isclose(
            number(result["maximum_absolute_difference"]), maximum, abs_tol=1e-12, rel_tol=0
        )
    ):
        raise ValueError("agent verdict or summary contradicts its actual numbers")
    if not math.isclose(
        number(result["current_mean"]) - number(result["baseline_mean"]),
        number(result["anomaly"]),
        abs_tol=1e-10,
        rel_tol=0,
    ):
        raise ValueError("agent monthly anomaly contradicts its temperatures")

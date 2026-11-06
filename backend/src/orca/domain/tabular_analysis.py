"""Purpose: calculate bounded descriptive summaries from approved normalized observations.
Behavior: block rejected rows and report explicit equal-observation weighting and sample support.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md sections 4-5; Phase: A02.
"""

import math
from typing import Any


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ready_for_analysis") or not result.get("observations"):
        raise ValueError("Normalization has unresolved rejected observations")
    rows = result["observations"]
    values = [float(r["value"]) for r in rows]
    mean = math.fsum(values) / len(values)
    dates = sorted(r["timestamp_utc"] for r in rows)
    return {
        "count": len(values),
        "mean_celsius": mean,
        "minimum_celsius": min(values),
        "maximum_celsius": max(values),
        "start": dates[0],
        "end": dates[-1],
        "weighting": "equal_observation",
        "unit": "C",
    }

"""Purpose: calculate P07 benchmark errors only against held-out reference observations.
Behavior: reject malformed pairs and return not-evaluable when independent truth is absent.
Refs: main_implementation plan.md P07.4; api design.md §4.5; Phase: P07.
"""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BenchmarkMetrics:
    status: str
    count: int
    mae: float | None
    rmse: float | None
    bias: float | None
    reason: str | None


def evaluate_benchmark(
    predicted: tuple[float, ...], observed: tuple[float, ...] | None
) -> BenchmarkMetrics:
    if observed is None:
        return BenchmarkMetrics("not_evaluable", 0, None, None, None, "held-out reference absent")
    if not predicted or len(predicted) != len(observed):
        raise ValueError("benchmark requires non-empty aligned prediction and reference pairs")
    if any(not math.isfinite(value) for value in (*predicted, *observed)):
        raise ValueError("benchmark values must be finite")
    errors = tuple(candidate - reference for candidate, reference in zip(predicted, observed))
    count = len(errors)
    return BenchmarkMetrics(
        status="evaluated",
        count=count,
        mae=math.fsum(abs(error) for error in errors) / count,
        rmse=math.sqrt(math.fsum(error * error for error in errors) / count),
        bias=math.fsum(errors) / count,
        reason=None,
    )

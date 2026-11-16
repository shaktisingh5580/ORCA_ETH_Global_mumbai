"""Purpose: verify P07 benchmark metrics and the absence of fabricated accuracy.
Behavior: use known vectors and reject empty or missing reference data.
Refs: main_implementation plan.md P07.4; Phase: P07.
"""

import math

import pytest

from orca.science.benchmarks import evaluate_benchmark


def test_mae_rmse_bias_known_vectors() -> None:
    result = evaluate_benchmark((2.0, 4.0, 6.0), (1.0, 5.0, 4.0))
    assert result.status == "evaluated"
    assert result.count == 3
    assert result.mae == pytest.approx(4 / 3)
    assert result.rmse == pytest.approx(math.sqrt(2))
    assert result.bias == pytest.approx(2 / 3)


def test_denominator_zero() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        evaluate_benchmark((), ())


def test_no_ground_truth_returns_not_evaluable() -> None:
    result = evaluate_benchmark((1.0, 2.0), None)
    assert result.status == "not_evaluable"
    assert result.mae is result.rmse is result.bias is None

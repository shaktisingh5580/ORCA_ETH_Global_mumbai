"""Purpose: verify the demo's arithmetic with independent closed-form reference fields.
Behavior: test constant and latitude-weighted anomalies plus incomplete/masked input rejection.
Refs: hackathon plan H02; docs/07_scientific_engine.md; Phase: H02; synthetic test-only fields.
"""

import math
from dataclasses import replace

import pytest

from orca.domain.prototype_analysis import seasonal_anomaly
from orca.domain.scientific_inputs import NativeGrid, NativeMonth, SSTCell


def month(year, lower, upper):
    grid = NativeGrid(0, 0, 2, 60, 2, 60)
    cells = tuple(
        SSTCell(day, y, x, lower if y == 0 else upper, 0.5, 1, True)
        for day in range(1, 32)
        for y in range(2)
        for x in range(2)
    )
    return NativeMonth(year, 1, grid, cells, "09:00:00.000000")


def test_prototype_constant_field_has_exact_known_anomaly():
    result = seasonal_anomaly(month(2025, 26, 26), (month(2003, 25, 25), month(2004, 25, 25)))
    assert result["anomaly"] == pytest.approx(1, abs=1e-12)
    assert result["current_mean"] == pytest.approx(26, abs=1e-12)
    assert (
        len(result["daily"]) == 31
        and result["scientific_status"] == "prototype_not_independently_validated"
    )


def test_prototype_spherical_weights_match_independent_analytic_integral():
    result = seasonal_anomaly(month(2025, 22, 20), (month(2003, 20, 20), month(2004, 20, 20)))
    # Native centers at 0/60 degrees split the clipped latitude interval at 30.
    # Integral cos(latitude) dlatitude gives sin(upper)-sin(lower).
    expected = 2 * math.sin(math.pi / 6) / math.sin(math.pi / 3)
    assert result["anomaly"] == pytest.approx(expected, abs=1e-12)
    assert result["anomaly"] != pytest.approx(1, abs=0.01)
    area = 6371008.8**2 * math.radians(2) * math.sin(math.pi / 3) / 1_000_000
    assert result["area_km2"] == pytest.approx(area, rel=1e-12)


def test_prototype_incomplete_or_invalid_cells_never_become_numeric_results():
    current = month(2025, 26, 26)
    baseline = month(2003, 25, 25)
    for invalid in (
        replace(current, cells=current.cells[:-1]),
        replace(current, cells=(replace(current.cells[0], mask=2), *current.cells[1:])),
        replace(current, cells=(replace(current.cells[0], sst=float("nan")), *current.cells[1:])),
    ):
        with pytest.raises(ValueError):
            seasonal_anomaly(invalid, (baseline,))

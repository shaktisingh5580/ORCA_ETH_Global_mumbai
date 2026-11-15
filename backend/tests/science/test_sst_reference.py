"""Purpose: check deterministic SST arithmetic against hand-computed independent references.
Behavior: synthetic values are confined to tests and cover weighting, units, masks, calendar and longitude wrap.
Refs: main_implementation plan.md P04.2; docs/07_scientific_engine.md §§13,19,21; Phase: P04.
"""

import math
from datetime import date

import pytest

from orca.science.methods.sst_anomaly import SSTSample, calculate_weighted_sst_anomaly
from orca.science.spatial import (
    longitude_intervals,
    spherical_rectangle_area_m2,
)
from orca.science.temporal import require_baseline_calendar_coverage
from orca.science.units import to_celsius

QC_DIGEST = "c" * 64


def sample(*args, **kwargs):
    return SSTSample(*args, qc_policy_digest=QC_DIGEST, qc_eligible=True, **kwargs)


def test_constant_field_zero_anomaly():
    result = calculate_weighted_sst_anomaly(
        (sample(27.0, 27.0, "degree_C", "degree_Celsius", 1.0, 86400.0),),
        expected_qc_policy_digest=QC_DIGEST,
    )
    assert result.anomaly_celsius == pytest.approx(0.0, abs=1e-12)


def test_hand_computed_weighted_grid():
    result = calculate_weighted_sst_anomaly(
        (
            sample(4.0, 1.0, "degree_C", "degree_Celsius", 1.0, 1.0),
            sample(10.0, 2.0, "degree_C", "degree_Celsius", 3.0, 1.0),
        ),
        expected_qc_policy_digest=QC_DIGEST,
    )
    # Independent hand calculation: current=(4*1+10*3)/4=8.5; baseline=(1+2*3)/4=1.75.
    assert result.current_mean_celsius == pytest.approx(8.5)
    assert result.baseline_mean_celsius == pytest.approx(1.75)
    assert result.anomaly_celsius == pytest.approx(6.75)


def test_kelvin_conversion():
    assert to_celsius(300.15, "K") == pytest.approx(27.0)
    result = calculate_weighted_sst_anomaly(
        (sample(300.15, 26.0, "kelvin", "degree_Celsius", 1.0, 1.0),),
        expected_qc_policy_digest=QC_DIGEST,
    )
    assert result.anomaly_celsius == pytest.approx(1.0)


def test_leap_day_calendar():
    require_baseline_calendar_coverage(
        (date(2024, 2, 28), date(2024, 2, 29)),
        (date(2004, 2, 28), date(2004, 2, 29)),
    )
    with pytest.raises(ValueError, match="02-29"):
        require_baseline_calendar_coverage((date(2024, 2, 29),), (date(2003, 2, 28),))


def test_masked_and_zero_cells():
    result = calculate_weighted_sst_anomaly(
        (
            sample(0.0, 1.0, "degree_C", "degree_Celsius", 2.0, 1.0),
            SSTSample(
                math.nan,
                math.nan,
                "degree_C",
                "degree_Celsius",
                1.0,
                1.0,
                qc_policy_digest=QC_DIGEST,
                qc_eligible=False,
            ),
        ),
        expected_qc_policy_digest=QC_DIGEST,
    )
    assert result.included_samples == 1
    assert result.excluded_samples == 1
    assert result.current_mean_celsius == pytest.approx(0.0)
    assert result.anomaly_celsius == pytest.approx(-1.0)


def test_antimeridian_region_handling():
    assert longitude_intervals(170.0, -170.0) == ((170.0, 180.0), (-180.0, -170.0))
    assert longitude_intervals(-20.0, 30.0) == ((-20.0, 30.0),)
    assert longitude_intervals(-180.0, 180.0) == ((-180.0, 180.0),)
    expected_area = (
        6_371_008.8**2
        * math.radians(20.0)
        * (math.sin(math.radians(10.0)) - math.sin(math.radians(-10.0)))
    )
    assert spherical_rectangle_area_m2(170.0, -10.0, -170.0, 10.0) == pytest.approx(
        expected_area, rel=1e-12
    )
    with pytest.raises(ValueError):
        longitude_intervals(20.0, 20.0)

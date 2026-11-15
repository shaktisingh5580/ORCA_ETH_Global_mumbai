"""Purpose: enforce P04's method, temporal, baseline and measurement compatibility rules.
Behavior: synthetic test-only recipes exercise exact acceptance and fail-closed policy outcomes.
Refs: main_implementation plan.md P04.1; api design.md §§3.5,4.3; ADR-R02; Phase: P04.
"""

from datetime import datetime, timedelta, timezone

import pytest

from orca.contracts.science import SSTRecipe
from orca.science.quality import validate_sst_recipe
from orca.science.registry import MethodRegistry, MethodVersion

NOW = datetime(2025, 2, 1, tzinfo=timezone.utc)
DIGEST = "a" * 64
QC_DIGEST = "c" * 64
SEMANTICS = "sea_surface_foundation_temperature"


def registry(status: str = "approved") -> MethodRegistry:
    return MethodRegistry(
        (
            MethodVersion(
                method_id="test.sst",
                version="1.0.0",
                code_digest=DIGEST,
                approval_status=status,
                required_baseline_years=2,
                measurement_semantics=SEMANTICS,
                qc_policy_id="mur-open-ocean-v1",
                qc_policy_digest=QC_DIGEST,
            ),
        )
    )


def recipe(**changes) -> SSTRecipe:
    values = {
        "method_id": "test.sst",
        "method_version": "1.0.0",
        "method_digest": DIGEST,
        "current_qc_policy_id": "mur-open-ocean-v1",
        "current_qc_policy_digest": QC_DIGEST,
        "baseline_qc_policy_id": "mur-open-ocean-v1",
        "baseline_qc_policy_digest": QC_DIGEST,
        "as_of": NOW,
        "current_start": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "current_end": NOW,
        "calendar": "gregorian",
        "baseline_years": (2003, 2004),
        "current_season": "gregorian-month:01",
        "baseline_season": "gregorian-month:01",
        "current_measurement": SEMANTICS,
        "baseline_measurement": SEMANTICS,
        "current_depth_m": None,
        "baseline_depth_m": None,
        "current_unit": "degree_C",
        "baseline_unit": "degree_Celsius",
    }
    values.update(changes)
    return SSTRecipe(**values)


def test_missing_wrong_season_baseline_rejected():
    with pytest.raises(ValueError, match="number of years"):
        validate_sst_recipe(recipe(baseline_years=(2003,)), registry())
    with pytest.raises(ValueError, match="season"):
        validate_sst_recipe(recipe(baseline_season="gregorian-month:02"), registry())


def test_depth_and_skin_bulk_incompatibility():
    with pytest.raises(ValueError, match="measurement semantics"):
        validate_sst_recipe(
            recipe(
                current_measurement="sea_surface_skin_temperature",
                baseline_measurement="sea_water_temperature",
                current_depth_m=0.0,
                baseline_depth_m=10.0,
            ),
            registry(),
        )
    with pytest.raises(ValueError, match="depths"):
        validate_sst_recipe(recipe(current_depth_m=0.0, baseline_depth_m=10.0), registry())


def test_as_of_unknown_rejected():
    with pytest.raises(ValueError, match="as_of"):
        validate_sst_recipe(recipe(as_of=None), registry())
    with pytest.raises(ValueError, match="timezone"):
        validate_sst_recipe(recipe(as_of=datetime(2025, 2, 1)), registry())
    with pytest.raises(ValueError, match="as_of cutoff"):
        validate_sst_recipe(recipe(current_end=NOW + timedelta(seconds=1)), registry())


def test_current_and_baseline_qc_policy_must_match_pinned_version():
    with pytest.raises(ValueError, match="pinned policy"):
        validate_sst_recipe(recipe(baseline_qc_policy_digest="d" * 64), registry())


def test_unapproved_method_version_fails():
    with pytest.raises(ValueError, match="not approved"):
        validate_sst_recipe(recipe(), registry("draft"))
    with pytest.raises(ValueError, match="digest"):
        validate_sst_recipe(recipe(method_digest="b" * 64), registry())

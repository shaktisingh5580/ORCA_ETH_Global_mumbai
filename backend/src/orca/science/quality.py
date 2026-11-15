"""Purpose: validate method compatibility and qualify evidence sufficiency before SST claims.
Behavior: fails closed on recipe mismatches and reports insufficient, inconclusive, invalid or supported outcomes.
Refs: main_implementation plan.md P04.1/P04.3; api design.md §§3.5,4.3,7; ADR-R02; Phase: P04.
"""

import math
from dataclasses import dataclass
from datetime import timezone
from typing import Literal

from orca.contracts.science import SSTRecipe
from orca.science.registry import MethodRegistry
from orca.science.units import is_temperature_unit


@dataclass(frozen=True, slots=True)
class EvidenceQualificationPolicy:
    minimum_spatial_coverage: float
    minimum_temporal_coverage: float
    minimum_valid_samples: int

    def __post_init__(self) -> None:
        for value in (self.minimum_spatial_coverage, self.minimum_temporal_coverage):
            if not math.isfinite(value) or not 0 < value <= 1:
                raise ValueError("coverage thresholds must be finite fractions in (0, 1]")
        if self.minimum_valid_samples < 1:
            raise ValueError("minimum valid sample count must be positive")


@dataclass(frozen=True, slots=True)
class EvidenceMetrics:
    spatial_coverage: float | None
    temporal_coverage: float | None
    valid_samples: int
    method_approved: bool
    source_qualified: bool
    baseline_qualified: bool
    uncertainty_celsius: float | None
    claim_kind: Literal["descriptive", "causal"] = "descriptive"


@dataclass(frozen=True, slots=True)
class EvidenceQualityReport:
    outcome: Literal["supported", "insufficient_evidence", "inconclusive", "invalid"]
    spatial_coverage: float | None
    temporal_coverage: float | None
    valid_samples: int
    uncertainty_celsius: float | None
    uncertainty_status: Literal["known", "unknown"]
    limitations: tuple[str, ...]


def qualify_evidence(
    metrics: EvidenceMetrics, policy: EvidenceQualificationPolicy
) -> EvidenceQualityReport:
    if metrics.claim_kind not in {"descriptive", "causal"}:
        raise ValueError("claim kind must be descriptive or causal")
    for coverage in (metrics.spatial_coverage, metrics.temporal_coverage):
        if coverage is not None and (not math.isfinite(coverage) or not 0 <= coverage <= 1):
            raise ValueError("coverage values must be finite fractions in [0, 1]")
    if metrics.valid_samples < 0:
        raise ValueError("valid sample count cannot be negative")
    if metrics.uncertainty_celsius is not None and (
        not math.isfinite(metrics.uncertainty_celsius) or metrics.uncertainty_celsius < 0
    ):
        raise ValueError("known uncertainty must be finite and nonnegative")

    limitations: list[str] = []
    if not (metrics.method_approved and metrics.source_qualified and metrics.baseline_qualified):
        outcome: Literal["supported", "insufficient_evidence", "inconclusive", "invalid"] = (
            "invalid"
        )
        limitations.append(
            "Method, source, and baseline must all be qualified before a result is supported."
        )
    elif metrics.spatial_coverage is None or metrics.temporal_coverage is None:
        outcome = "insufficient_evidence"
        limitations.append("Spatial and temporal coverage must both be known.")
    elif (
        metrics.spatial_coverage < policy.minimum_spatial_coverage
        or metrics.temporal_coverage < policy.minimum_temporal_coverage
        or metrics.valid_samples < policy.minimum_valid_samples
    ):
        outcome = "insufficient_evidence"
        limitations.append(
            "Observed coverage or sample count is below the reviewed policy threshold."
        )
    elif metrics.claim_kind == "causal":
        outcome = "inconclusive"
        limitations.append(
            "This analysis is descriptive and does not establish a causal relationship."
        )
    else:
        outcome = "supported"

    if metrics.uncertainty_celsius is None:
        limitations.append("Aggregate uncertainty is unknown; it is not treated as zero.")
    return EvidenceQualityReport(
        outcome=outcome,
        spatial_coverage=metrics.spatial_coverage,
        temporal_coverage=metrics.temporal_coverage,
        valid_samples=metrics.valid_samples,
        uncertainty_celsius=metrics.uncertainty_celsius,
        uncertainty_status="unknown" if metrics.uncertainty_celsius is None else "known",
        limitations=tuple(limitations),
    )


def validate_sst_recipe(recipe: SSTRecipe, registry: MethodRegistry) -> None:
    method = registry.resolve(recipe.method_id, recipe.method_version, recipe.method_digest)
    if recipe.calendar != method.calendar:
        raise ValueError("recipe calendar is incompatible with the pinned method")
    if (
        recipe.current_qc_policy_id != method.qc_policy_id
        or recipe.baseline_qc_policy_id != method.qc_policy_id
        or recipe.current_qc_policy_digest != method.qc_policy_digest
        or recipe.baseline_qc_policy_digest != method.qc_policy_digest
    ):
        raise ValueError("current and baseline QC must match the method's pinned policy")
    if recipe.as_of is None or recipe.as_of.utcoffset() is None:
        raise ValueError("explicit timezone-aware as_of is required")
    if recipe.current_end.astimezone(timezone.utc) > recipe.as_of.astimezone(timezone.utc):
        raise ValueError("analysis interval cannot extend beyond the as_of cutoff")
    if len(recipe.baseline_years) != method.required_baseline_years:
        raise ValueError("baseline does not contain the method's required number of years")
    current_year_utc = recipe.current_start.astimezone(timezone.utc).year
    if any(year >= current_year_utc for year in recipe.baseline_years):
        raise ValueError("baseline years must precede the current analysis year")
    if recipe.current_season != recipe.baseline_season:
        raise ValueError("baseline season must match the current season")
    if recipe.current_measurement != method.measurement_semantics:
        raise ValueError("current measurement semantics are incompatible with the method")
    if recipe.baseline_measurement != method.measurement_semantics:
        raise ValueError("baseline measurement semantics are incompatible with the method")
    if recipe.current_depth_m != recipe.baseline_depth_m:
        raise ValueError("current and baseline measurement depths are incompatible")
    if not is_temperature_unit(recipe.current_unit) or not is_temperature_unit(
        recipe.baseline_unit
    ):
        raise ValueError("current and baseline units must be explicit supported temperature units")

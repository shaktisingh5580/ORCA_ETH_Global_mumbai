"""Purpose: compute a deterministic area/time-weighted SST difference for pre-qualified samples.
Behavior: converts inputs to Celsius, excludes masked rows, applies explicit area×duration weights and returns a reference-only result.
Refs: main_implementation plan.md P04.2; docs/07_scientific_engine.md §§7,19; ADR-R02; Phase: P04.
"""

import math
import re
from dataclasses import dataclass

from orca.science.units import to_celsius

METHOD_ID = "orca.sst-area-time-anomaly"
METHOD_VERSION = "0.1.0-reference"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SSTSample:
    current_value: float
    baseline_value: float
    current_unit: str
    baseline_unit: str
    area_weight_m2: float
    duration_seconds: float
    qc_policy_digest: str
    qc_eligible: bool


@dataclass(frozen=True, slots=True)
class SSTAnomalyResult:
    current_mean_celsius: float
    baseline_mean_celsius: float
    anomaly_celsius: float
    included_samples: int
    excluded_samples: int
    total_area_time_weight: float
    qc_policy_digest: str
    method_id: str = METHOD_ID
    method_version: str = METHOD_VERSION
    scientific_status: str = "reference_only_not_approved"
    uncertainty: str = "unknown"


def calculate_weighted_sst_anomaly(
    samples: tuple[SSTSample, ...], *, expected_qc_policy_digest: str
) -> SSTAnomalyResult:
    if not samples:
        raise ValueError("at least one SST sample is required")
    if not _DIGEST.fullmatch(expected_qc_policy_digest):
        raise ValueError("expected QC policy digest must be a lowercase SHA-256 hex digest")
    weighted_current: list[float] = []
    weighted_baseline: list[float] = []
    weights: list[float] = []
    excluded = 0
    for sample in samples:
        if sample.qc_policy_digest != expected_qc_policy_digest:
            raise ValueError("sample QC policy digest differs from the pinned method policy")
        if not sample.qc_eligible:
            excluded += 1
            continue
        if (
            not math.isfinite(sample.area_weight_m2)
            or not math.isfinite(sample.duration_seconds)
            or sample.area_weight_m2 <= 0
            or sample.duration_seconds <= 0
        ):
            raise ValueError("valid samples require positive finite area and duration weights")
        weight = sample.area_weight_m2 * sample.duration_seconds
        if not math.isfinite(weight):
            raise ValueError("combined area/time weight must be finite")
        current = to_celsius(sample.current_value, sample.current_unit)
        baseline = to_celsius(sample.baseline_value, sample.baseline_unit)
        weights.append(weight)
        current_contribution = weight * current
        baseline_contribution = weight * baseline
        if not math.isfinite(current_contribution) or not math.isfinite(baseline_contribution):
            raise ValueError("weighted temperature contribution must be finite")
        weighted_current.append(current_contribution)
        weighted_baseline.append(baseline_contribution)
    try:
        total_weight = math.fsum(weights)
        current_sum = math.fsum(weighted_current)
        baseline_sum = math.fsum(weighted_baseline)
    except OverflowError as error:
        raise ValueError("scientific weighted sums must remain finite") from error
    if not math.isfinite(total_weight) or total_weight <= 0:
        raise ValueError("no valid SST samples remain after quality masking")
    current_mean = current_sum / total_weight
    baseline_mean = baseline_sum / total_weight
    anomaly = current_mean - baseline_mean
    if not all(math.isfinite(value) for value in (current_mean, baseline_mean, anomaly)):
        raise ValueError("computed SST results must be finite")
    return SSTAnomalyResult(
        current_mean_celsius=current_mean,
        baseline_mean_celsius=baseline_mean,
        anomaly_celsius=anomaly,
        included_samples=len(weights),
        excluded_samples=excluded,
        total_area_time_weight=total_weight,
        qc_policy_digest=expected_qc_policy_digest,
    )

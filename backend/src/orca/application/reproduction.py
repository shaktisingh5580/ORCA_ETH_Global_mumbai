"""Purpose: verify P07 numerical reproduction against immutable source and method lineage.
Behavior: recheck raw inputs and origin bytes, require an approved method and compare new output without claiming independent operation.
Refs: main_implementation plan.md P07.1; api design.md §4.5; Phase: P07.
"""

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Callable
from uuid import UUID

from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.science.manifests import source_bytes_digest, verify_manifest
from orca.science.registry import MethodRegistry

_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PinnedInput:
    snapshot_id: UUID
    content: bytes
    object_version_id: str | None


@dataclass(frozen=True, slots=True)
class ReproductionResult:
    verdict: str
    actual_value: float | None
    original_value: float | None
    difference: float | None
    tolerance: float | None
    implementation_digest: str
    environment_digest: str
    verified_input_count: int
    institutional_independence_verified: bool = False


def _anomaly(content: bytes) -> float:
    try:
        document = json.loads(content)
        value = document["anomaly_celsius"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("reproduction result must contain anomaly_celsius") from error
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("reproduction anomaly must be a finite number")
    return float(value)


def reproduce_numeric_result(
    manifest: ScienceEvidenceManifest,
    inputs: tuple[PinnedInput, ...],
    origin_result: bytes,
    calculator: Callable[[tuple[PinnedInput, ...]], bytes],
    *,
    registry: MethodRegistry,
    implementation_digest: str,
    environment_digest: str,
    tolerance_celsius: float,
) -> ReproductionResult:
    """Recalculate from exact bytes; execution isolation must be verified separately."""
    verify_manifest(manifest)
    if not _DIGEST.fullmatch(implementation_digest) or not _DIGEST.fullmatch(environment_digest):
        raise ValueError("pinned implementation and environment digests are required")
    if not math.isfinite(tolerance_celsius) or tolerance_celsius < 0:
        raise ValueError("finite nonnegative tolerance is required")
    if manifest.method_approval_status != "approved":
        raise ValueError("origin evidence did not use an approved method")
    if (
        manifest.result_status == "supported"
        and manifest.absolute_tolerance_celsius is not None
        and tolerance_celsius > manifest.absolute_tolerance_celsius
    ):
        raise ValueError("reproduction tolerance cannot exceed the approved evidence tolerance")
    registry.resolve(manifest.method_id, manifest.method_version, manifest.method_code_digest)
    expected = {item.snapshot_id: item for item in manifest.inputs}
    supplied = {item.snapshot_id: item for item in inputs}
    if len(supplied) != len(inputs) or set(supplied) != set(expected):
        raise ValueError("all exact evidence inputs are required")
    for item in inputs:
        pin = expected[item.snapshot_id]
        if item.object_version_id != pin.object_version_id:
            raise ValueError("reproduction input object version mismatch")
        if (
            len(item.content) != pin.byte_count
            or source_bytes_digest(item.content) != pin.raw_content_digest
        ):
            raise ValueError("reproduction input digest mismatch")
    if "sha256:" + hashlib.sha256(origin_result).hexdigest() != manifest.result_digest:
        raise ValueError("origin result digest mismatch")
    original_value = _anomaly(origin_result)
    independent_output = calculator(inputs)
    actual_value = _anomaly(independent_output)
    difference = actual_value - original_value
    return ReproductionResult(
        verdict="reproduced" if abs(difference) <= tolerance_celsius else "mismatch",
        actual_value=actual_value,
        original_value=original_value,
        difference=difference,
        tolerance=tolerance_celsius,
        implementation_digest=implementation_digest,
        environment_digest=environment_digest,
        verified_input_count=len(inputs),
    )

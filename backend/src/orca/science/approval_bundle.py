"""Purpose: load an operator-pinned human-reviewed P04 science approval bundle.
Behavior: reject missing attestations, incomplete policy or changed implementation bytes at startup.
Refs: main_implementation plan.md P04.1–P04.4; api design.md §§3.5,4.3; Phase: P04.
"""

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from orca.contracts.common import Contract, Digest, UTCDateTime
from orca.contracts.science_api import QualityCodebook, ScientificPolicySummary
from orca.science.methods.sst_anomaly import METHOD_ID, METHOD_VERSION
from orca.science.registry import MethodRegistry, MethodVersion

SCIENCE_ROOT = Path(__file__).resolve().parent
REVIEWED_FILES = (
    "methods/sst_anomaly.py",
    "spatial.py",
    "temporal.py",
    "units.py",
    "quality.py",
    "registry.py",
)


class ScientificApprovalBundle(Contract):
    schema_version: Literal["orca.science-approval/1.0"]
    reviewer_name: str = Field(min_length=2, max_length=200)
    reviewer_affiliation: str = Field(min_length=2, max_length=250)
    reviewer_expertise: str = Field(min_length=20, max_length=2000)
    reviewed_at: UTCDateTime
    decision: Literal["approved"]
    review_record_file: str = Field(min_length=1)
    review_record_digest: Digest
    method_id: str
    method_version: str
    method_code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_files: dict[str, str]
    required_baseline_years: int = Field(ge=1)
    measurement_semantics: str = Field(min_length=1)
    qc_policy_id: str = Field(min_length=1)
    qc_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    calendar: str = Field(min_length=1)
    baseline_definition: str = Field(min_length=20)
    allowed_claim: str = Field(min_length=20)
    uncertainty_treatment: str = Field(min_length=20)
    policy: ScientificPolicySummary
    quality_codebooks: tuple[QualityCodebook, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def matching_policy(self) -> "ScientificApprovalBundle":
        if self.policy.threshold_status != "approved":
            raise ValueError("scientific thresholds must be approved")
        if (
            self.policy.method_id != self.method_id
            or self.policy.version != self.method_version
            or self.policy.qc_policy_id != self.qc_policy_id
            or self.policy.qc_policy_digest != self.qc_policy_digest
        ):
            raise ValueError("scientific policy does not match the approved method")
        if set(self.implementation_files) != set(REVIEWED_FILES):
            raise ValueError("approval must pin every reviewed implementation file")
        if len({(book.source_id, book.variable) for book in self.quality_codebooks}) != len(
            self.quality_codebooks
        ):
            raise ValueError("duplicate source quality codebook")
        if any(
            book.policy_id != self.qc_policy_id or book.policy_digest != self.qc_policy_digest
            for book in self.quality_codebooks
        ):
            raise ValueError("source quality codebook does not match approved QC policy")
        return self


def load_approval_bundle(
    path: str,
) -> tuple[
    MethodRegistry,
    dict[tuple[str, str], ScientificPolicySummary],
    dict[tuple[str, str], QualityCodebook],
]:
    """Load only an exact signed-off method version; never infer approval from tests."""
    bundle_path = Path(path)
    if not bundle_path.is_file() or bundle_path.stat().st_size > 65536:
        raise ValueError("scientific approval bundle is absent or oversized")
    try:
        raw = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle = ScientificApprovalBundle.model_validate(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as error:
        raise ValueError("scientific approval bundle is unreadable") from error
    if bundle.method_id != METHOD_ID or bundle.method_version != METHOD_VERSION:
        raise ValueError("scientific approval does not match the implemented method version")
    review_path = Path(bundle.review_record_file)
    if not review_path.is_absolute():
        review_path = bundle_path.parent / review_path
    if not review_path.is_file() or review_path.stat().st_size > 1048576:
        raise ValueError("scientific review attestation is absent or oversized")
    if (
        "sha256:" + hashlib.sha256(review_path.read_bytes()).hexdigest()
        != bundle.review_record_digest
    ):
        raise ValueError("scientific review attestation digest mismatch")
    for relative in REVIEWED_FILES:
        expected = bundle.implementation_files[relative]
        if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
            raise ValueError("reviewed implementation digest is invalid")
        actual = hashlib.sha256((SCIENCE_ROOT / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"reviewed implementation changed: {relative}")
    if bundle.method_code_digest != bundle.implementation_files["methods/sst_anomaly.py"]:
        raise ValueError("method code digest differs from the reviewed implementation")
    method = MethodVersion(
        method_id=bundle.method_id,
        version=bundle.method_version,
        code_digest=bundle.method_code_digest,
        approval_status="approved",
        required_baseline_years=bundle.required_baseline_years,
        measurement_semantics=bundle.measurement_semantics,
        qc_policy_id=bundle.qc_policy_id,
        qc_policy_digest=bundle.qc_policy_digest,
        calendar=bundle.calendar,
    )
    return (
        MethodRegistry((method,)),
        {(method.method_id, method.version): bundle.policy},
        {(book.source_id, book.variable): book for book in bundle.quality_codebooks},
    )

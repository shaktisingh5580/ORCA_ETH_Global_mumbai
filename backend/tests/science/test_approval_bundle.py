"""Purpose: verify scientific approval is tied to written review and exact implementation bytes.
Behavior: use temporary test-only attestations and reject drift, missing policy and invalid decisions.
Refs: main_implementation plan.md P04.1–P04.4; api design.md §3.5; Phase: P04.
"""

import hashlib
import json
import secrets
from pathlib import Path

import pytest
from pydantic import SecretStr

from orca.api.app import create_app
from orca.config import Settings
from orca.science.approval_bundle import REVIEWED_FILES, SCIENCE_ROOT, load_approval_bundle
from orca.science.methods.sst_anomaly import METHOD_ID, METHOD_VERSION


def _write_bundle(tmp_path: Path) -> Path:
    attestation = tmp_path / "review.txt"
    attestation.write_text("Test-only scientific review attestation; no production approval.\n")
    digests = {
        path: hashlib.sha256((SCIENCE_ROOT / path).read_bytes()).hexdigest()
        for path in REVIEWED_FILES
    }
    bundle = {
        "schema_version": "orca.science-approval/1.0",
        "reviewer_name": "Test Reviewer",
        "reviewer_affiliation": "Test Institute",
        "reviewer_expertise": "Synthetic test fixture for SST methodology review",
        "reviewed_at": "2026-11-17T00:00:00Z",
        "decision": "approved",
        "review_record_file": "review.txt",
        "review_record_digest": "sha256:" + hashlib.sha256(attestation.read_bytes()).hexdigest(),
        "method_id": METHOD_ID,
        "method_version": METHOD_VERSION,
        "method_code_digest": digests["methods/sst_anomaly.py"],
        "implementation_files": digests,
        "required_baseline_years": 20,
        "measurement_semantics": "sea_surface_foundation_temperature",
        "qc_policy_id": "test-qc",
        "qc_policy_digest": "c" * 64,
        "calendar": "gregorian",
        "baseline_definition": "Matching January daily values across twenty prior years",
        "allowed_claim": "Descriptive local SST difference; no causal or climate claim",
        "uncertainty_treatment": "Unknown aggregate uncertainty must be reported, not zero",
        "policy": {
            "method_id": METHOD_ID,
            "version": METHOD_VERSION,
            "qc_policy_id": "test-qc",
            "qc_policy_digest": "c" * 64,
            "threshold_status": "approved",
            "minimum_spatial_coverage": 0.8,
            "minimum_temporal_coverage": 0.9,
            "minimum_valid_samples": 20,
            "numerical_tolerance_celsius": 1e-9,
            "rationale": "Test-only tolerance, not scientific uncertainty",
        },
        "quality_codebooks": [
            {
                "source_id": "test-mur",
                "variable": "sst",
                "policy_id": "test-qc",
                "policy_digest": "c" * 64,
                "flags": {"1": "open sea"},
            }
        ],
    }
    output = tmp_path / "bundle.json"
    output.write_text(json.dumps(bundle))
    return output


def test_reviewed_bundle_loads_only_exact_pinned_method(tmp_path: Path):
    path = _write_bundle(tmp_path)
    registry, policies, codebooks = load_approval_bundle(str(path))
    approved = registry.approved_versions()
    assert len(approved) == 1
    assert approved[0].method_id == METHOD_ID
    assert policies[(METHOD_ID, METHOD_VERSION)].minimum_valid_samples == 20
    assert codebooks[("test-mur", "sst")].flags == {"1": "open sea"}


@pytest.mark.parametrize(
    "change", ["decision", "implementation_files", "policy", "quality_codebooks"]
)
def test_review_bundle_rejects_unapproved_or_incomplete_policy(tmp_path: Path, change: str):
    path = _write_bundle(tmp_path)
    data = json.loads(path.read_text())
    if change == "decision":
        data[change] = "pending"
    elif change == "implementation_files":
        data[change]["methods/sst_anomaly.py"] = "0" * 64
    elif change == "policy":
        data[change]["minimum_spatial_coverage"] = None
    else:
        data[change] = []
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_approval_bundle(str(path))


def test_review_bundle_rejects_changed_attestation(tmp_path: Path):
    path = _write_bundle(tmp_path)
    (tmp_path / "review.txt").write_text("Changed after approval\n")
    with pytest.raises(ValueError, match="attestation digest"):
        load_approval_bundle(str(path))


def test_application_loads_approval_only_when_bundle_is_explicitly_configured(tmp_path: Path):
    settings = Settings(environment="test", monitoring_token=SecretStr(secrets.token_urlsafe(48)))
    assert create_app(settings).state.science_registry.approved_versions() == ()
    path = _write_bundle(tmp_path)
    reviewed = create_app(settings.model_copy(update={"science_approval_bundle_file": str(path)}))
    assert reviewed.state.science_registry.approved_versions()[0].method_id == METHOD_ID
    (tmp_path / "review.txt").write_text("Changed review record\n")
    with pytest.raises(ValueError, match="attestation digest"):
        create_app(settings.model_copy(update={"science_approval_bundle_file": str(path)}))

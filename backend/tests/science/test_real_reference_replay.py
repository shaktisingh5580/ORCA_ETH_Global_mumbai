"""Purpose: verify content-addressed P04 science lineage and replay preserved MUR inputs.
Behavior: test digest safeguards and recompute a real pinned subset when retained source bytes are available.
Refs: main_implementation plan.md P04.4; docs/04_database.md §§3,7; Phase: P04.
"""

import csv
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import rfc8785
from pydantic import ValidationError

from orca.contracts.science_evidence import ScienceEvidenceManifest, ScienceSnapshotInput
from orca.science.manifests import source_bytes_digest, verify_manifest
from orca.science.methods.sst_anomaly import SSTSample, calculate_weighted_sst_anomaly

EARTH_MEAN_RADIUS_M = 6_371_008.8
NUMERICAL_REPRODUCTION_TOLERANCE_C = 1e-9
QC_POLICY = {
    "id": "mur-open-ocean-valid-error-v1",
    "dataset": "jplMURSST41",
    "variable": "analysed_sst",
    "units": "degree_C",
    "require_finite_sst": True,
    "mask_variable": "mask",
    "accepted_mask_value": 1,
    "mask_meaning": "open_sea",
    "error_variable": "analysis_error",
    "error_minimum": 0.0,
    "error_maximum": 327.67,
    "error_units": "degree_C",
    "approval_status": "proposed_not_approved",
}
QC_POLICY_DIGEST = "295ff6a6480cf6beac186b615371a37a8b88e112ec6c6a23ee98d5ef947b30cb"


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def preserved_input_directory() -> Path:
    configured_directory = os.environ.get("ORCA_P04_REAL_REPLAY_DIR")
    return (
        Path(configured_directory)
        if configured_directory
        else repository_root() / ".cache" / "science-inputs" / "mur-native-batch"
    )


def manifest(**changes: object) -> ScienceEvidenceManifest:
    values: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "project_id": uuid4(),
        "created_by": "test-operator",
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "source_mode": "synthetic_test",
        "method_id": "test.sst-reference",
        "method_version": "1.0.0-test",
        "method_code_digest": "a" * 64,
        "method_approval_status": "draft",
        "qc_policy_id": "test-qc-v1",
        "qc_policy_digest": "b" * 64,
        "recipe_digest": "sha256:" + "c" * 64,
        "result_digest": "sha256:" + "d" * 64,
        "result_status": "insufficient_evidence",
        "source_qualified": False,
        "baseline_qualified": False,
        "absolute_tolerance_celsius": None,
        "uncertainty_celsius": None,
        "inputs": (
            ScienceSnapshotInput(
                snapshot_id=uuid4(),
                role="current",
                year=2025,
                raw_content_digest="sha256:" + "e" * 64,
                byte_count=100,
            ),
            ScienceSnapshotInput(
                snapshot_id=uuid4(),
                role="baseline",
                year=2003,
                raw_content_digest="sha256:" + "f" * 64,
                byte_count=99,
            ),
        ),
    }
    values.update(changes)
    return ScienceEvidenceManifest.build(**values)


def test_real_pinned_subset_recomputes_within_tolerance():
    input_directory = preserved_input_directory()
    capture_path = input_directory / "capture.json"
    report_path = repository_root() / "docs" / "gates" / "P03" / "native-inputs-cloud.json"
    if not capture_path.is_file() or not report_path.is_file():
        pytest.skip(
            "preserved real MUR inputs are unavailable; set ORCA_P04_REAL_REPLAY_DIR; "
            "G03/G04 remain open"
        )

    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    qualification = json.loads(report_path.read_text(encoding="utf-8"))
    assert capture["fixture"] is False
    assert capture["dataset"] == "jplMURSST41"
    assert capture["source_host"] == "coastwatch.pfeg.noaa.gov"
    assert qualification["fixture"] is False
    assert qualification["source_dataset"] == "jplMURSST41"
    assert qualification["qualification"]["baseline_years"] == list(range(2003, 2023))
    assert hashlib.sha256(rfc8785.dumps(QC_POLICY)).hexdigest() == QC_POLICY_DIGEST

    def read_snapshot(year: int) -> dict[tuple[str, float, float], float]:
        descriptor = capture["inputs"][str(year)]
        raw = (input_directory / descriptor["file"]).read_bytes()
        assert len(raw) == descriptor["bytes"]
        assert source_bytes_digest(raw) == descriptor["sha256"]
        expected = next(item for item in qualification["snapshots"] if item["year"] == year)
        assert expected["sha256"] == descriptor["sha256"]
        assert expected["bytes"] == descriptor["bytes"]

        records: dict[tuple[str, float, float], float] = {}
        with (input_directory / descriptor["file"]).open(
            newline="", encoding="iso-8859-1"
        ) as source:
            reader = csv.DictReader(source)
            assert reader.fieldnames == [
                "time",
                "latitude",
                "longitude",
                "analysed_sst",
                "analysis_error",
                "mask",
            ]
            units = next(reader)
            assert units["analysed_sst"] == "degree_C"
            assert units["analysis_error"] == "degree_C"
            for row in reader:
                stamp = datetime.fromisoformat(row["time"].replace("Z", "+00:00"))
                assert stamp.utcoffset() is not None
                assert stamp.year == year and stamp.month == 1
                value = float(row["analysed_sst"])
                error = float(row["analysis_error"])
                mask = int(row["mask"])
                assert math.isfinite(value) and mask == 1
                assert math.isfinite(error) and 0.0 <= error <= 327.67
                key = (
                    stamp.strftime("%m-%d"),
                    round(float(row["latitude"]), 2),
                    round(float(row["longitude"]), 2),
                )
                assert key not in records
                records[key] = value
        return records

    years = tuple(range(2003, 2023))
    baselines = {year: read_snapshot(year) for year in years}
    current = read_snapshot(2025)
    expected_keys = {
        (f"01-{day:02d}", lat, lon)
        for day in range(1, 32)
        for lat in (round(20.45 + 0.01 * index, 2) for index in range(11))
        for lon in (round(68.45 + 0.01 * index, 2) for index in range(11))
    }
    assert set(current) == expected_keys
    assert all(set(rows) == expected_keys for rows in baselines.values())

    qc_digest = QC_POLICY_DIGEST
    samples: list[SSTSample] = []
    independently_weighted_current: list[float] = []
    independently_weighted_baseline: list[float] = []
    total_area_time = 0.0
    for key in sorted(expected_keys):
        day, latitude, longitude = key
        baseline_value = math.fsum(rows[key] for rows in baselines.values()) / len(years)
        lat_min = max(20.45, latitude - 0.005)
        lat_max = min(20.55, latitude + 0.005)
        lon_min = max(68.45, longitude - 0.005)
        lon_max = min(68.55, longitude + 0.005)
        cell_area = (
            EARTH_MEAN_RADIUS_M**2
            * math.radians(lon_max - lon_min)
            * (math.sin(math.radians(lat_max)) - math.sin(math.radians(lat_min)))
        )
        duration = 86_400.0
        weight = cell_area * duration
        current_value = current[key]
        samples.append(
            SSTSample(
                current_value=current_value,
                baseline_value=baseline_value,
                current_unit="degree_C",
                baseline_unit="degree_C",
                area_weight_m2=cell_area,
                duration_seconds=duration,
                qc_policy_digest=qc_digest,
                qc_eligible=True,
            )
        )
        independently_weighted_current.append(current_value * weight)
        independently_weighted_baseline.append(baseline_value * weight)
        total_area_time += weight

    result = calculate_weighted_sst_anomaly(tuple(samples), expected_qc_policy_digest=qc_digest)
    reference_current = math.fsum(independently_weighted_current) / total_area_time
    reference_baseline = math.fsum(independently_weighted_baseline) / total_area_time
    reference_anomaly = reference_current - reference_baseline
    assert result.included_samples == 3751
    assert result.excluded_samples == 0
    assert result.current_mean_celsius == pytest.approx(
        26.24750604815608, abs=NUMERICAL_REPRODUCTION_TOLERANCE_C
    )
    assert result.baseline_mean_celsius == pytest.approx(
        25.624008769016, abs=NUMERICAL_REPRODUCTION_TOLERANCE_C
    )
    assert result.anomaly_celsius == pytest.approx(
        0.6234972791400754, abs=NUMERICAL_REPRODUCTION_TOLERANCE_C
    )
    assert result.anomaly_celsius == pytest.approx(
        reference_anomaly, abs=NUMERICAL_REPRODUCTION_TOLERANCE_C
    )
    assert result.scientific_status == "reference_only_not_approved"


def test_input_hash_matches_original():
    input_directory = preserved_input_directory()
    capture_path = input_directory / "capture.json"
    report_path = repository_root() / "docs" / "gates" / "P03" / "native-inputs-cloud.json"
    if not capture_path.is_file() or not report_path.is_file():
        pytest.skip("preserved original MUR bytes are unavailable")
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    qualification = json.loads(report_path.read_text(encoding="utf-8"))
    descriptor = capture["inputs"]["2025"]
    original = (input_directory / descriptor["file"]).read_bytes()
    recorded = next(item for item in qualification["snapshots"] if item["year"] == 2025)
    assert source_bytes_digest(original) == descriptor["sha256"] == recorded["sha256"]
    assert len(original) == descriptor["bytes"] == recorded["bytes"]


def test_manifest_digest_is_verified_before_persistence():
    evidence = manifest()
    verify_manifest(evidence)
    changed = evidence.model_dump(mode="json")
    changed["method_version"] = "2.0.0"
    with pytest.raises(ValidationError, match="digest does not match"):
        ScienceEvidenceManifest.model_validate(changed)


def test_method_version_immutable():
    evidence = manifest()
    with pytest.raises(ValidationError):
        evidence.method_version = "2.0.0"


def test_supported_result_requires_real_qualified_method_and_tolerance():
    with pytest.raises(ValidationError, match="synthetic test data"):
        manifest(result_status="supported")


def test_empty_source_bytes_are_rejected():
    with pytest.raises(ValueError, match="cannot be empty"):
        source_bytes_digest(b"")

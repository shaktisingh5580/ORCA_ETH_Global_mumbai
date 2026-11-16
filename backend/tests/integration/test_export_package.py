"""Purpose: verify P06 export rights, immutable bytes and report status disclosure.
Behavior: use synthetic test evidence only inside tests; reject missing or altered inputs and escape report content.
Refs: main_implementation plan.md P06.2-P06.3; Phase: P06.
"""

import io
import json
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from orca.application.exports import ExportInput, build_evidence_export
from orca.contracts.science_evidence import ScienceEvidenceManifest, ScienceSnapshotInput
from orca.science.manifests import source_bytes_digest
from orca.workers.handlers.report import render_report_html, report_view


def evidence() -> tuple[ScienceEvidenceManifest, bytes, bytes]:
    current, baseline = b"current SST sample", b"baseline SST sample"
    manifest = ScienceEvidenceManifest.build(
        id=uuid4(),
        tenant_id=uuid4(),
        project_id=uuid4(),
        created_by="test",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_mode="synthetic_test",
        method_id="test.sst",
        method_version="1",
        method_code_digest="a" * 64,
        method_approval_status="draft",
        qc_policy_id="test.qc",
        qc_policy_digest="b" * 64,
        recipe_digest="sha256:" + "c" * 64,
        result_digest="sha256:" + "d" * 64,
        result_status="insufficient_evidence",
        source_qualified=False,
        baseline_qualified=False,
        inputs=(
            ScienceSnapshotInput(
                snapshot_id=uuid4(),
                role="current",
                year=2025,
                raw_content_digest=source_bytes_digest(current),
                byte_count=len(current),
                object_version_id="version-1",
            ),
            ScienceSnapshotInput(
                snapshot_id=uuid4(),
                role="baseline",
                year=2003,
                raw_content_digest=source_bytes_digest(baseline),
                byte_count=len(baseline),
                object_version_id="version-2",
            ),
        ),
    )
    return manifest, current, baseline


def test_export_manifest_references_actual_blob_versions() -> None:
    manifest, current, baseline = evidence()
    package = build_evidence_export(
        manifest,
        (
            ExportInput(
                manifest.inputs[0].snapshot_id, "public-license", True, current, "version-1"
            ),
            ExportInput(
                manifest.inputs[1].snapshot_id, "public-license", True, baseline, "version-2"
            ),
        ),
    )
    with zipfile.ZipFile(io.BytesIO(package.archive)) as archive:
        stored = json.loads(archive.read("manifest.json"))
        assert stored == package.manifest
        assert archive.read(f"inputs/{manifest.inputs[0].snapshot_id}.bin") == current
        assert archive.read(f"inputs/{manifest.inputs[1].snapshot_id}.bin") == baseline
    assert package.manifest["evidence_manifest_digest"] == manifest.manifest_digest
    assert package.manifest_digest.startswith("sha256:")


def test_rights_disallow_redistribution() -> None:
    manifest, current, baseline = evidence()
    with pytest.raises(PermissionError):
        build_evidence_export(
            manifest,
            (
                ExportInput(
                    manifest.inputs[0].snapshot_id, "restricted", False, current, "version-1"
                ),
                ExportInput(manifest.inputs[1].snapshot_id, "public", True, baseline, "version-2"),
            ),
        )
    package = build_evidence_export(
        manifest,
        (
            ExportInput(manifest.inputs[0].snapshot_id, "restricted", False, None, "version-1"),
            ExportInput(manifest.inputs[1].snapshot_id, "public", True, baseline, "version-2"),
        ),
    )
    with zipfile.ZipFile(io.BytesIO(package.archive)) as archive:
        assert f"inputs/{manifest.inputs[0].snapshot_id}.bin" not in archive.namelist()


def test_missing_snapshot_export_blocked() -> None:
    manifest, current, _ = evidence()
    with pytest.raises(ValueError, match="every evidence snapshot"):
        build_evidence_export(
            manifest,
            (ExportInput(manifest.inputs[0].snapshot_id, "public", True, current, "version-1"),),
        )


def test_report_supported_vs_insufficient_and_html_escaped() -> None:
    manifest, _, _ = evidence()
    with pytest.raises(ValueError, match="limitations"):
        report_view(manifest, title="SST", limitations=())
    view = report_view(
        manifest, title="<script>alert(1)</script>", limitations=("Source < uncertain",)
    )
    html = render_report_html(view).decode("utf-8")
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "Source &lt; uncertain" in html
    assert "not an official advisory" in html

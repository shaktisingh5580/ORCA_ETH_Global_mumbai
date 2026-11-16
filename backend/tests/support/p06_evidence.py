"""Purpose: provide synthetic immutable evidence solely for P06 contract tests.
Behavior: construct a digest-valid insufficient-evidence manifest with two pinned inputs.
Refs: main_implementation plan.md P06.1; Phase: P06 test support.
"""

from datetime import datetime, timezone
from uuid import uuid4

from orca.contracts.science_evidence import ScienceEvidenceManifest, ScienceSnapshotInput
from orca.science.manifests import source_bytes_digest


def insufficient_manifest() -> ScienceEvidenceManifest:
    current, baseline = b"current", b"baseline"
    return ScienceEvidenceManifest.build(
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
            ),
            ScienceSnapshotInput(
                snapshot_id=uuid4(),
                role="baseline",
                year=2003,
                raw_content_digest=source_bytes_digest(baseline),
                byte_count=len(baseline),
            ),
        ),
    )

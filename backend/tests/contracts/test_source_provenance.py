"""Purpose: keep source reconciliation from becoming a checksum bypass.
Behavior: reject missing/corrupt originals and unrecorded current edits.
Refs: source-revisions.json; main plan P00; Phase: prerequisite recovery.
"""

import hashlib

import pytest
from source_provenance import verify_source


def setup_revision(root):
    (root / "plan").write_bytes(b"updated")
    (root / "original").write_bytes(b"original")
    original = hashlib.sha256(b"original").hexdigest()
    return original, {
        "plan": {
            "original_sha256": original,
            "current_sha256": hashlib.sha256(b"updated").hexdigest(),
            "archive": "original",
            "decision": "recovery",
            "reason": "Documented scope change",
        }
    }


def test_valid_revision_preserves_both_versions(tmp_path):
    original, revisions = setup_revision(tmp_path)
    verify_source(tmp_path, "plan", original, revisions)
    assert (tmp_path / "plan").read_bytes() == b"updated"
    assert (tmp_path / "original").read_bytes() == b"original"


@pytest.mark.parametrize("target", ["plan", "original"])
def test_tampered_revision_or_archive_fails(tmp_path, target):
    original, revisions = setup_revision(tmp_path)
    (tmp_path / target).write_bytes(b"changed")
    with pytest.raises(ValueError):
        verify_source(tmp_path, "plan", original, revisions)


def test_missing_original_fails(tmp_path):
    original, revisions = setup_revision(tmp_path)
    (tmp_path / "original").unlink()
    with pytest.raises(FileNotFoundError):
        verify_source(tmp_path, "plan", original, revisions)


def test_unrecorded_change_fails(tmp_path):
    original, _ = setup_revision(tmp_path)
    with pytest.raises(ValueError, match="Unreconciled"):
        verify_source(tmp_path, "plan", original, {})


def test_readme_exception_cannot_waive_other_sources(tmp_path):
    original, revisions = setup_revision(tmp_path)
    revisions["plan"]["exception"] = "user-approved-readme-baseline-2026-11-13"
    with pytest.raises(ValueError, match="cannot apply"):
        verify_source(tmp_path, "plan", original, revisions)


def test_actual_readme_baseline_and_later_drift(tmp_path):
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    revisions = json.loads((root / "docs/decisions/source-revisions.json").read_text())
    original = revisions["README.md"]["original_sha256"]
    (tmp_path / "README.md").write_bytes((root / "README.md").read_bytes())
    verify_source(tmp_path, "README.md", original, revisions)
    (tmp_path / "README.md").write_bytes(b"unapproved replacement")
    with pytest.raises(ValueError, match="revision mismatch"):
        verify_source(tmp_path, "README.md", original, revisions)

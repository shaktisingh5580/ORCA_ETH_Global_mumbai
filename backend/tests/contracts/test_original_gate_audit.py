"""Purpose: prevent historical evidence from being mistaken for current acceptance.
Behavior: exercise drift, absent evidence and containment without modifying real gates.
Refs: original-scope-recovery decision; main plan P00-P02; Phase: prerequisite recovery.
"""

import hashlib
import json

from audit_original_gates import audit_gate


def manifest(root, hashes, status="PASS"):
    target = root / "docs/gates/P00/manifest.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"phase": "P00", "status": status, "input_hashes": hashes}))
    return target


def test_reports_all_drift_without_rewriting_manifest(tmp_path):
    (tmp_path / "changed").write_bytes(b"new")
    target = manifest(tmp_path, {"changed": "old", "missing": "old"})
    before = target.read_bytes()
    report = audit_gate(tmp_path, "P00")
    assert report["status"] == "BLOCKED"
    assert len(report["issues"]) == 2
    assert target.read_bytes() == before


def test_unchanged_inputs_are_not_a_new_gate_pass(tmp_path):
    (tmp_path / "input").write_bytes(b"data")
    manifest(tmp_path, {"input": hashlib.sha256(b"data").hexdigest()})
    assert audit_gate(tmp_path, "P00")["status"] == "RECORDED_INPUTS_UNCHANGED"


def test_missing_manifest_blocks(tmp_path):
    assert audit_gate(tmp_path, "P00")["status"] == "BLOCKED"


def test_empty_hashes_block(tmp_path):
    manifest(tmp_path, {})
    assert audit_gate(tmp_path, "P00")["status"] == "BLOCKED"


def test_paths_outside_workspace_block(tmp_path):
    manifest(tmp_path, {"../outside": "invalid"})
    assert audit_gate(tmp_path, "P00")["issues"] == [
        {"path": "../outside", "reason": "outside_workspace"}
    ]

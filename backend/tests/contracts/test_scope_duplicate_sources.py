"""Purpose: keep contract generation stable for identical source copies without hiding conflicts.
Behavior: accept byte-identical approved directives and reject differing source snapshots.
Refs: research-only guide; source preservation agreement; Phase: platform repair.
"""

import json
from pathlib import Path

import pytest
from export_research_scope import scope_documents

ROOT = Path(__file__).resolve().parents[3]


def test_identical_directive_copies_are_safe_but_conflicting_copies_fail(tmp_path):
    approved = ROOT / "ORCA_3_0_Research_Only_Scope_and_Pruning_Guide (1).md"
    if not approved.exists():
        pytest.skip("local registered source is intentionally excluded from CI")
    content = approved.read_bytes()
    first = tmp_path / "ORCA_3_0_Research_Only_Scope_and_Pruning_Guide (1).md"
    second = tmp_path / "ORCA_3_0_Research_Only_Scope_and_Pruning_Guide.md"
    first.write_bytes(content)
    second.write_bytes(content)
    designed = json.loads(
        (ROOT / "contracts/designed/orca-proposal.json").read_text(encoding="utf-8")
    )
    manifest, _ = scope_documents(tmp_path, designed)
    assert manifest["selected_operation_count"] == 165
    second.write_bytes(content + b"\n")
    with pytest.raises(ValueError, match="conflicting"):
        scope_documents(tmp_path, designed)

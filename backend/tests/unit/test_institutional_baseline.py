"""Purpose: prevent planning documents from becoming false implemented-route evidence.
Behavior: verify real runtime inventory and preserve explicit institutional feature gaps.
Refs: ORCA_INSTITUTIONAL_PHASES_AND_ACCEPTANCE.md F01.1; Phase: F01.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from audit_institutional_baseline import inventory  # noqa: E402


def test_inventory_records_actual_routes_and_missing_federation():
    report = inventory()
    assert "POST /v1/projects/{project_id}/reasoning" in report["operations"]
    assert "POST /v1/organizations/{org_id}/agents" in report["operations"]
    assert report["capabilities"]["institution_owned_onboarding"].startswith("registered;")
    assert report["capabilities"]["tabular_a2a_delegation"] == "not implemented"
    assert "0018_research_anchors.py" in report["migrations"]
    assert report["packages"]["a2a-sdk"] == "0.3.22"

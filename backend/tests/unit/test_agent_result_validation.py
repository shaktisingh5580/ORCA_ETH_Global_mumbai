"""Purpose: reject inconsistent agent numerical claims and unintended credential forwarding.
Behavior: test verdict/tolerance/NaN contradictions and the read-only bridge environment boundary.
Refs: H04 protocol/security gate; Phase: H04; synthetic test payloads only.
"""

import copy
import subprocess

import pytest

from orca.domain.agent_results import validate_comparison
from orca.infrastructure.federation.runtime import ArkivGateway


def test_agent_cannot_fabricate_its_reproduction_verdict_or_comparison_summary():
    expected = {
        "current_mean": 27.0,
        "baseline_mean": 26.0,
        "anomaly": 1.0,
        "daily": [
            {"date": "2025-01-01", "current_mean": 27.0, "baseline_mean": 26.0, "anomaly": 1.0}
        ],
    }
    result = {
        **copy.deepcopy(expected),
        "verdict": "reproduced",
        "maximum_absolute_difference": 0.0,
        "absolute_tolerance_celsius": 1e-9,
    }
    validate_comparison(result, expected)
    for changed in (
        {"verdict": "challenged"},
        {"anomaly": 4.0},
        {"maximum_absolute_difference": 1.0},
        {"current_mean": float("nan")},
        {"absolute_tolerance_celsius": 1.0},
    ):
        with pytest.raises(ValueError):
            validate_comparison({**result, **changed}, expected)
    with pytest.raises(ValueError):
        validate_comparison(
            {
                **result,
                "current_mean": 28.0,
                "anomaly": 2.0,
                "verdict": "challenged",
                "maximum_absolute_difference": 1.0,
            },
            expected,
        )


def test_read_only_bridge_does_not_forward_signer_or_cloud_credentials(tmp_path, monkeypatch):
    envfile = tmp_path / "test-only.env"
    envfile.write_text("ARKIV_ACCESS_KEY=test-only-access\nARKIV_PRIVATE_KEY=test-only-private\n")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-only-cloud-secret")
    monkeypatch.setenv("ORCA_GEMINI_API_KEY", "test-only-provider-secret")
    monkeypatch.setenv("ARKIV_PRIVATE_KEY", "test-only-ambient-secret")
    seen = []

    def completed(*args, **kwargs):
        seen.append(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, '{"status":"test-only"}', "")

    monkeypatch.setattr("orca.infrastructure.federation.runtime.subprocess.run", completed)
    assert ArkivGateway(envfile, tmp_path, "").call({"command": "read"}) == {"status": "test-only"}
    assert seen[0]["ARKIV_ACCESS_KEY"] == "test-only-access"
    assert all(
        name not in seen[0]
        for name in ("ARKIV_PRIVATE_KEY", "SUPABASE_SERVICE_ROLE_KEY", "ORCA_GEMINI_API_KEY")
    )

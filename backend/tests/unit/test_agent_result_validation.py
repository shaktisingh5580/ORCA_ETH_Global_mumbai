"""Purpose: reject inconsistent agent numerical claims and unintended credential forwarding.
Behavior: test verdict/tolerance/NaN contradictions and the read-only bridge environment boundary.
Refs: H04 protocol/security gate, main_implementation plan.md P11.2–P11.4. Phase: H04/P11.
"""

import copy
import runpy
import subprocess
from pathlib import Path

import pytest

from orca.domain.agent_results import validate_comparison
from orca.infrastructure.federation.runtime import ArkivGateway, PublicV1ReconciliationRequired


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


def test_arkiv_v1_bridge_keeps_reads_separate_and_fences_unresolved_writes(tmp_path, monkeypatch):
    envfile = tmp_path / "test-only.env"
    envfile.write_text("ARKIV_ACCESS_KEY=test-access\nARKIV_PRIVATE_KEY=test-private\n")
    owner = "0x" + "a" * 40
    key = "0x" + "b" * 64
    commands = []

    def invoke(args, **kwargs):
        commands.append((args, kwargs))
        if args[2] == "read":
            return subprocess.CompletedProcess(args, 0, '{"status":"observed"}', "")
        return subprocess.CompletedProcess(args, 0, '{"status":"unresolved"}', "")

    monkeypatch.setattr("orca.infrastructure.federation.runtime.subprocess.run", invoke)
    gateway = ArkivGateway(envfile, tmp_path, owner)
    assert gateway.public_v1_read("task", key, owner) == {"status": "observed"}
    assert commands[0][0][2:] == ["read", "task", key, owner]
    assert "ARKIV_PRIVATE_KEY" not in commands[0][1]["env"]
    with pytest.raises(ValueError, match="pinned writer mismatch"):
        gateway.public_v1_publish({"owner": "0x" + "c" * 40})
    assert len(commands) == 1
    with pytest.raises(PublicV1ReconciliationRequired) as error:
        gateway.public_v1_publish({"owner": owner})
    assert error.value.reconciliation
    assert commands[1][0][-1] == "--approve-public"
    assert commands[1][1]["env"]["ARKIV_PRIVATE_KEY"] == "test-private"


def test_legacy_resume_cannot_requeue_public_v1_operations():
    script = Path(__file__).resolve().parents[2] / "scripts/reconcile_agent_operation.py"
    guard = runpy.run_path(str(script))["refuse_v1_resume"]
    historical = {"error_code": "ARKIV_RECONCILIATION_REQUIRED", "payload": {}}
    guard(historical, None)
    for row, receipt in (
        ({**historical, "error_code": "ARKIV_V1_RECONCILIATION_REQUIRED"}, None),
        ({**historical, "payload": {"publish_arkiv_v1": True}}, None),
        (historical, {"arkiv_v1": {"status": "indexed"}}),
    ):
        with pytest.raises(ValueError, match="cannot requeue"):
            guard(row, receipt)

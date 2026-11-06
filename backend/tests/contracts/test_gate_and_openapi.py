"""Purpose: prove gates fail closed and exported contracts reproduce exactly.
Behavior: test missing/skip/live/stale evidence plus the real four-route HTTP surface.
Refs: main plan P01.4; API sections 3.1,11,19; Phase: P01.
"""

import json
import secrets
from pathlib import Path

import pytest
from export_openapi import artifacts, export, typescript
from fastapi.testclient import TestClient
from openapi_spec_validator import validate
from pydantic import SecretStr
from run_phase_gate import digest, evaluate_gate, read_junit, verify_prerequisite

from orca.api.app import create_app
from orca.config import Settings

ROOT = Path(__file__).resolve().parents[3]


def test_gate_no_tests_collected_fails():
    assert evaluate_gate(["test_required"], {}, 0, [], [], {})[0] == "FAIL"
    assert evaluate_gate([], {}, 0, [], [], {})[0] == "FAIL"
    assert evaluate_gate(["test_required"], {"test_required": "PASS"}, 1, [], [], {})[0] == "FAIL"


def test_gate_required_skip_fails():
    for state in ("SKIP", "FAIL", "BLOCKED", "XFAIL"):
        assert (
            evaluate_gate(["test_required"], {"test_required": state}, 0, [], [], {})[0] == "FAIL"
        )


def test_gate_missing_prerequisite_blocks(tmp_path):
    errors = verify_prerequisite(tmp_path, "P00")
    assert errors
    assert (
        evaluate_gate(["test_required"], {"test_required": "PASS"}, 0, errors, [], {})[0]
        == "BLOCKED"
    )
    directory = tmp_path / "docs/gates/P00"
    directory.mkdir(parents=True)
    source = tmp_path / "source.md"
    source.write_text("original")
    (directory / "manifest.json").write_text(
        json.dumps(
            {"phase": "P00", "status": "PASS", "input_hashes": {"source.md": digest(source)}}
        )
    )
    assert verify_prerequisite(tmp_path, "P00") == []
    source.write_text("changed")
    assert verify_prerequisite(tmp_path, "P00")
    (directory / "manifest.json").write_text("{}")
    assert verify_prerequisite(tmp_path, "P00")


def test_gate_live_block_is_not_pass():
    for evidence in ({}, {"provider_probe": "BLOCKED"}, {"provider_probe": "SKIP"}):
        assert (
            evaluate_gate(
                ["test_required"], {"test_required": "PASS"}, 0, [], ["provider_probe"], evidence
            )[0]
            == "BLOCKED"
        )
    assert evaluate_gate(["test_required"], {"test_required": "PASS"}, 0, [], [], {})[0] == "PASS"


def test_openapi_export_deterministic(tmp_path):
    one, two = artifacts(), artifacts()
    assert one == two
    export(ROOT / "contracts", check=True)
    export(tmp_path)
    export(tmp_path, check=True)
    for name in ("openapi/orca-v1.json", "designed/orca-proposal.json"):
        validate(json.loads(one[name]))
    implemented = json.loads(one["openapi/orca-v1.json"])
    assert set(implemented["paths"]) == {"/livez", "/readyz", "/v1/meta", "/openapi.json"}
    assert implemented["paths"]["/readyz"]["get"]["security"] == [{"MonitoringBearer": []}]
    assert (
        "application/problem+json"
        in implemented["paths"]["/readyz"]["get"]["responses"]["401"]["content"]
    )
    designed = json.loads(one["designed/orca-proposal.json"])
    operations = [op for path in designed["paths"].values() for op in path.values()]
    assert len(operations) == 220
    assert all(op["x-orca-implementation-status"] == "DESIGNED" for op in operations)
    (tmp_path / "openapi/orca-v1.json").write_text("{}")
    with pytest.raises(ValueError, match="drift"):
        export(tmp_path, check=True)


def test_generated_ts_reproducible():
    first, second = artifacts(), artifacts()
    assert first["typescript/orca.ts"] == second["typescript/orca.ts"]
    assert (ROOT / "contracts/typescript/orca.ts").read_text() == first["typescript/orca.ts"]
    assert '"max_amount": string' in first["typescript/orca.ts"]
    assert "Array<ResearchProject>" in first["typescript/orca.ts"]
    with pytest.raises(ValueError):
        typescript({"unsupported": True})


def test_junit_missing_and_duplicate_results_fail(tmp_path):
    report = tmp_path / "report.xml"
    with pytest.raises(FileNotFoundError):
        read_junit(report)
    report.write_text('<testsuite><testcase name="a"/><testcase name="a"/></testsuite>')
    with pytest.raises(ValueError, match="Duplicate"):
        read_junit(report)
    report.write_text('<testsuite><testcase name="a"><skipped/></testcase></testsuite>')
    assert read_junit(report) == {"a": "SKIP"}


@pytest.mark.allow_hosts(["127.0.0.1", "::1"])
def test_foundation_routes_do_not_claim_core_readiness():
    token = secrets.token_urlsafe(48)
    settings = Settings(monitoring_token=SecretStr(token))
    with TestClient(create_app(settings)) as client:
        assert client.get("/livez").json() == {"status": "alive"}
        metadata = client.get("/v1/meta").json()
        assert metadata["enabled_capabilities"] == []
        assert client.get("/readyz").status_code == 401
        assert client.get("/readyz").headers["www-authenticate"] == "Bearer"
        ready = client.get("/readyz", headers={"Authorization": f"Bearer {token}"})
        assert ready.status_code == 503
        assert ready.json()["blocked_gates"] == [f"G{i:02}" for i in range(2, 9)]
        assert token not in ready.text
        assert client.get("/openapi.json").status_code == 401
        assert (
            client.get("/openapi.json", headers={"Authorization": f"Bearer {token}"}).status_code
            == 200
        )
        assert client.get("/v1/research/projects").status_code == 404
    with TestClient(create_app(settings.model_copy(update={"docs_public": True}))) as client:
        assert client.get("/openapi.json").status_code == 200

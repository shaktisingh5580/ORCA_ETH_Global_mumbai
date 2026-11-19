"""Purpose: verify the P13.1 frozen pilot replay and blinded export.
Behavior: reject drift, duplicated cases and reference conflicts using synthetic test records.
Refs: main_implementation plan.md P13.1; docs/07_scientific_engine.md §9; Phase: P13.
"""

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/pilot_replay.py"
pilot = runpy.run_path(str(SCRIPT))
ARMS = pilot["ARMS"]
Protocol = pilot["Protocol"]
RunSet = pilot["RunSet"]
sha256, score, blind, load = (pilot[name] for name in ("sha256", "score", "blind", "load"))


def encoded(value):
    return json.dumps(value, separators=(",", ":")).encode()


@pytest.fixture
def frozen():
    reference = {
        "schema": "orca.pilot.reference.v1",
        "kind": "synthetic",
        "custodian": "reference-keeper",
        "provenance_digest": sha256(b"test-only-reference-provenance"),
        "expected": {"sst": 28.1, "missing": None},
    }
    cases = [
        {"id": name, "input_digest": sha256(name.encode()), "unit": "degC", "tolerance": 0.1}
        for name in reference["expected"]
    ]
    workflow_digests = {arm: sha256(arm.encode()) for arm in ARMS}
    protocol = {
        "schema": "orca.pilot.protocol.v1",
        "mode": "rehearsal",
        "cases": cases,
        "workflow_digests": workflow_digests,
        "reference_digest": sha256(encoded(reference)),
    }
    runs = []
    for arm in ARMS:
        for case in cases:
            answered = case["id"] == "sst"
            runs.append(
                {
                    "case_id": case["id"],
                    "arm": arm,
                    "operator": "operator-" + arm,
                    "input_digest": case["input_digest"],
                    "workflow_digest": workflow_digests[arm],
                    "outcome": "answered" if answered else "abstained",
                    "prediction": 28.2 if answered else None,
                    "researcher_seconds": 12,
                    "correction_seconds": 3,
                    "output_digest": sha256(b"original"),
                    "replay_digest": sha256(b"original"),
                }
            )
    return (
        protocol,
        {
            "schema": "orca.pilot.runs.v1",
            "protocol_digest": sha256(encoded(protocol)),
            "runs": runs,
        },
        reference,
    )


def test_same_test_set_all_systems(frozen, tmp_path):
    protocol, bundle, reference = frozen
    protocol_path, runs_path = tmp_path / "protocol.json", tmp_path / "runs.json"
    protocol_path.write_bytes(encoded(protocol))
    runs_path.write_bytes(encoded(bundle))
    parsed, runs = load(protocol_path, runs_path, bundle["protocol_digest"])
    report = score(parsed, runs, encoded(reference))
    assert report["paired_numeric_cases"] == {"degC": 1}
    assert all(results["valid_outcomes"] == 2 for results in report["workflows"].values())
    for rows in (bundle["runs"][:-1], bundle["runs"] + [bundle["runs"][0]]):
        with pytest.raises(ValueError, match="exactly once"):
            score(parsed, RunSet.model_validate({**bundle, "runs": rows}), encoded(reference))
    inconsistent = copy.deepcopy(bundle)
    inconsistent["runs"][0]["input_digest"] = sha256(b"different input")
    with pytest.raises(ValueError, match="frozen input"):
        score(parsed, RunSet.model_validate(inconsistent), encoded(reference))
    protocol_path.write_bytes(encoded({**protocol, "mode": "controlled_pilot"}))
    with pytest.raises(ValueError, match="preregistered"):
        load(protocol_path, runs_path, bundle["protocol_digest"])


def test_human_rater_blinded_when_feasible(frozen):
    protocol, bundle, _ = frozen
    packet, key = blind(Protocol.model_validate(protocol), RunSet.model_validate(bundle))
    assert len(packet) == len(key) == 6
    assert set(key) == {row["review_id"] for row in packet}
    assert set(key.values()) == set(ARMS)
    assert all(
        set(row) == {"review_id", "case_id", "outcome", "prediction", "unit"} for row in packet
    )
    assert all(value not in encoded(packet).decode() for value in ARMS)


def test_ground_truth_independent(frozen):
    protocol, bundle, reference = frozen
    runs = RunSet.model_validate(bundle)
    changed = {**reference, "expected": {"sst": 28.2, "missing": None}}
    with pytest.raises(ValueError, match="frozen digest"):
        score(Protocol.model_validate(protocol), runs, encoded(changed))
    shared = {**reference, "custodian": bundle["runs"][0]["operator"], "kind": "held_out"}
    with pytest.raises(ValueError, match="custodian"):
        score(
            Protocol.model_validate({**protocol, "reference_digest": sha256(encoded(shared))}),
            runs,
            encoded(shared),
        )
    with pytest.raises(ValueError, match="requires held-out"):
        score(
            Protocol.model_validate({**protocol, "mode": "controlled_pilot"}),
            runs,
            encoded(reference),
        )


def test_no_causal_overclaim(frozen):
    protocol, bundle, reference = frozen
    bundle["runs"][0].update(prediction=None, outcome="failed")
    report = score(
        Protocol.model_validate(protocol), RunSet.model_validate(bundle), encoded(reference)
    )
    assert report["inference"] == "descriptive_within_frozen_tasks"
    assert report["scientific_causality_established"] is False
    assert report["institutional_adoption_established"] is False
    assert report["gate_pass"] is False
    assert report["paired_numeric_cases"] == {"degC": 0}
    assert report["workflows"]["scripts"]["outcomes"] == {"failed": 1, "abstained": 1}
    for workflow in report["workflows"].values():
        assert workflow["paired_metrics"]["degC"]["status"] == "not_evaluable"
        assert workflow["paired_metrics"]["degC"]["mae"] is None


def test_pilot_cli_blind_packet_and_key_remain_separate(frozen, tmp_path):
    protocol, bundle, _ = frozen
    protocol_path, runs_path = tmp_path / "protocol.json", tmp_path / "runs.json"
    packet_path, key_path = tmp_path / "packet.json", tmp_path / "key.json"
    protocol_path.write_bytes(encoded(protocol))
    runs_path.write_bytes(encoded(bundle))
    command = [
        sys.executable,
        str(SCRIPT),
        "--protocol",
        str(protocol_path),
        "--protocol-digest",
        bundle["protocol_digest"],
        "--runs",
        str(runs_path),
        "blind",
        "--packet",
        str(packet_path),
        "--private-key-map",
        str(key_path),
    ]
    blinded = subprocess.run(command, capture_output=True, check=False)
    assert blinded.returncode == 0, blinded.stderr
    original_packet = packet_path.read_bytes()
    original_key = key_path.read_bytes()
    assert {row["review_id"] for row in json.loads(original_packet)} == set(
        json.loads(original_key)
    )
    assert all(arm.encode() not in original_packet for arm in ARMS)
    assert subprocess.run(command, capture_output=True, check=False).returncode == 2
    assert packet_path.read_bytes() == original_packet
    assert key_path.read_bytes() == original_key

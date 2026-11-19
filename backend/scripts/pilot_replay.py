"""Purpose: replay a frozen P13 comparison across scripts, a general agent and ORCA.
Behavior: verify common cases and independent-reference metadata, score descriptively and blind packets.
Refs: main_implementation plan.md P13.1; docs/07_scientific_engine.md §§6,9; Phase: P13.
"""

import argparse
import hashlib
import json
import os
import secrets
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from orca.infrastructure.federation.signing import strict_json
from orca.science.benchmarks import evaluate_benchmark

Arm = Literal["scripts", "general_agent", "orca"]
ARMS: tuple[Arm, ...] = ("scripts", "general_agent", "orca")
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
Code = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
Measure = Annotated[FiniteFloat, Field(ge=-1e12, le=1e12)]
Seconds = Annotated[FiniteFloat, Field(ge=0, le=86400)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Case(Record):
    id: Code
    input_digest: Digest
    unit: Annotated[str, Field(pattern=r"^[A-Za-z0-9._%/-]{1,24}$")]
    tolerance: Annotated[Measure, Field(ge=0)]


class Protocol(Record):
    schema_version: Literal["orca.pilot.protocol.v1"] = Field(alias="schema")
    mode: Literal["rehearsal", "controlled_pilot"]
    cases: Annotated[list[Case], Field(min_length=1, max_length=500)]
    workflow_digests: dict[Arm, Digest]
    reference_digest: Digest


class Run(Record):
    case_id: Code
    arm: Arm
    operator: Code
    input_digest: Digest
    workflow_digest: Digest
    outcome: Literal["answered", "abstained", "failed"]
    prediction: Measure | None = None
    researcher_seconds: Seconds
    correction_seconds: Seconds
    output_digest: Digest | None = None
    replay_digest: Digest | None = None


class RunSet(Record):
    schema_version: Literal["orca.pilot.runs.v1"] = Field(alias="schema")
    protocol_digest: Digest
    runs: Annotated[list[Run], Field(min_length=3, max_length=1500)]


class Reference(Record):
    schema_version: Literal["orca.pilot.reference.v1"] = Field(alias="schema")
    kind: Literal["synthetic", "held_out"]
    custodian: Code
    provenance_digest: Digest
    expected: dict[Code, Measure | None]


def sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_runs(protocol: Protocol, bundle: RunSet) -> dict[str, Case]:
    cases = {case.id: case for case in protocol.cases}
    if len(cases) != len(protocol.cases) or set(protocol.workflow_digests) != set(ARMS):
        raise ValueError("Protocol requires unique cases and all three frozen workflows")
    expected = {(arm, case_id) for arm in ARMS for case_id in cases}
    actual = {(run.arm, run.case_id) for run in bundle.runs}
    if actual != expected or len(actual) != len(bundle.runs):
        raise ValueError("All workflows must cover the same frozen cases exactly once")
    for run in bundle.runs:
        if (
            run.input_digest != cases[run.case_id].input_digest
            or run.workflow_digest != protocol.workflow_digests[run.arm]
        ):
            raise ValueError("Run differs from the frozen input or workflow")
        if (run.outcome == "answered") != (run.prediction is not None):
            raise ValueError("Only answered runs can carry a prediction")
        if run.replay_digest is not None and run.output_digest is None:
            raise ValueError("A replay requires an original output digest")
    return cases


def validate_reference(
    protocol: Protocol, bundle: RunSet, cases: dict[str, Case], reference_bytes: bytes
) -> Reference:
    if sha256(reference_bytes) != protocol.reference_digest:
        raise ValueError("Held-out reference differs from the frozen digest")
    reference = Reference.model_validate(strict_json(reference_bytes))
    if set(reference.expected) != set(cases):
        raise ValueError("Reference case IDs differ from the frozen cases")
    if protocol.mode == "controlled_pilot" and reference.kind != "held_out":
        raise ValueError("A controlled pilot requires held-out reference data")
    if reference.custodian in {run.operator for run in bundle.runs}:
        raise ValueError("The reference custodian must be separate from workflow operators")
    return reference


def score(protocol: Protocol, bundle: RunSet, reference_bytes: bytes) -> dict[str, Any]:
    cases = validate_runs(protocol, bundle)
    reference = validate_reference(protocol, bundle, cases, reference_bytes)
    by_arm = {arm: {run.case_id: run for run in bundle.runs if run.arm == arm} for arm in ARMS}
    observed_numbers = {
        case_id: observed
        for case_id, observed in reference.expected.items()
        if observed is not None
    }
    predictions = {
        arm: {
            case_id: run.prediction for case_id, run in runs.items() if run.prediction is not None
        }
        for arm, runs in by_arm.items()
    }
    numeric_cases = {
        case_id
        for case_id in observed_numbers
        if all(case_id in numbers for numbers in predictions.values())
    }
    groups = {
        unit: sorted(case_id for case_id in numeric_cases if cases[case_id].unit == unit)
        for unit in sorted({case.unit for case in cases.values()})
    }
    reports = {}
    for arm, runs in by_arm.items():
        matching = sum(
            matches_expectation(run, reference.expected[case_id], cases[case_id].tolerance)
            for case_id, run in runs.items()
        )
        reports[arm] = {
            "cases": len(cases),
            "outcomes": dict(Counter(run.outcome for run in runs.values())),
            "valid_outcomes": matching,
            "total_researcher_seconds": sum(run.researcher_seconds for run in runs.values()),
            "total_correction_seconds": sum(run.correction_seconds for run in runs.values()),
            "replay_attempts": sum(run.replay_digest is not None for run in runs.values()),
            "exact_replays": sum(
                run.replay_digest is not None and run.replay_digest == run.output_digest
                for run in runs.values()
            ),
            "paired_metrics": {
                unit: asdict(
                    evaluate_benchmark(
                        tuple(predictions[arm][case_id] for case_id in ids),
                        tuple(observed_numbers[case_id] for case_id in ids) if ids else None,
                    )
                )
                for unit, ids in groups.items()
            },
        }
    return {
        "schema": "orca.pilot.report.v1",
        "mode": protocol.mode,
        "protocol_digest": bundle.protocol_digest,
        "reference_kind": reference.kind,
        "reference_digest": protocol.reference_digest,
        "reference_provenance_digest": reference.provenance_digest,
        "reference_independence": "custodian_separation_checked",
        "paired_numeric_cases": {unit: len(ids) for unit, ids in groups.items()},
        "inference": "descriptive_within_frozen_tasks",
        "scientific_causality_established": False,
        "institutional_adoption_established": False,
        "gate_pass": False,
        "workflows": reports,
    }


def matches_expectation(run: Run, observed: float | None, tolerance: float) -> bool:
    if observed is None:
        return run.outcome == "abstained"
    if run.prediction is None:
        return False
    return abs(Decimal(str(run.prediction)) - Decimal(str(observed))) <= Decimal(str(tolerance))


def blind(protocol: Protocol, bundle: RunSet) -> tuple[list[dict[str, Any]], dict[str, Arm]]:
    cases = validate_runs(protocol, bundle)
    packet: list[dict[str, Any]] = []
    key: dict[str, Arm] = {}
    for run in bundle.runs:
        identifier = secrets.token_hex(16)
        key[identifier] = run.arm
        packet.append(
            {
                "review_id": identifier,
                "case_id": run.case_id,
                "outcome": run.outcome,
                "prediction": run.prediction,
                "unit": cases[run.case_id].unit,
            }
        )
    secrets.SystemRandom().shuffle(packet)
    return packet, key


def load(protocol_path: Path, run_path: Path, expected_digest: str) -> tuple[Protocol, RunSet]:
    protocol_bytes = protocol_path.read_bytes()
    if sha256(protocol_bytes) != expected_digest:
        raise ValueError("Protocol differs from the preregistered digest")
    protocol = Protocol.model_validate(strict_json(protocol_bytes))
    bundle = RunSet.model_validate(strict_json(run_path.read_bytes()))
    if bundle.protocol_digest != expected_digest:
        raise ValueError("Run bundle was collected under a different protocol")
    return protocol, bundle


def write_new(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--protocol-digest", required=True)
    parser.add_argument("--runs", type=Path, required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    score_parser = actions.add_parser("score")
    score_parser.add_argument("--reference", type=Path, required=True)
    blind_parser = actions.add_parser("blind")
    blind_parser.add_argument("--packet", type=Path, required=True)
    blind_parser.add_argument("--private-key-map", type=Path, required=True)
    args = parser.parse_args()
    try:
        protocol, bundle = load(args.protocol, args.runs, args.protocol_digest)
        if args.action == "score":
            print(json.dumps(score(protocol, bundle, args.reference.read_bytes()), indent=2))
        else:
            if args.packet.resolve() == args.private_key_map.resolve():
                raise ValueError("The blinded packet and private key map require distinct paths")
            packet, key = blind(protocol, bundle)
            write_new(args.private_key_map, key)
            write_new(args.packet, packet)
    except (OSError, ValueError):
        # Inputs can include private observations or operator identifiers.
        parser.exit(2, "Pilot replay rejected. Check the frozen digests and input schemas.\n")


if __name__ == "__main__":
    main()

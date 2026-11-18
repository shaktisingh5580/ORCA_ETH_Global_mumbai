"""Purpose: recompute a bounded P09 provider result in a separate local process.
Behavior: verify both signatures and compare independently supplied values to the signed result.
Refs: main_implementation plan.md P09.4; api design.md §§5.4,13; Phase: P09.
"""

import argparse
import json
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from orca.application.federation import SignedResearchGrant, verify_grant
from orca.infrastructure.federation.research_a2a_client import verified_artifact
from orca.infrastructure.federation.signing import digest, strict_json


def verify_summary(
    signed: SignedResearchGrant,
    coordinator: dict[str, str],
    provider: dict[str, str],
    response: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, object]:
    """Verify an arithmetic summary against locally supplied inputs and pinned public keys."""
    grant = verify_grant(signed, coordinator["public_key"], coordinator["key_id"])
    if grant.method_id != "arithmetic_mean/1.0" or grant.capability_id != "temperature_summary_v1":
        raise ValueError("Unsupported remote summary method")
    remote = verified_artifact(
        {
            "result": {
                "kind": "task",
                "id": response["a2a_task_id"],
                "contextId": response["a2a_context_id"],
                "status": {"state": "completed"},
                "artifacts": [{"parts": [{"kind": "data", "data": response["signed_artifact"]}]}],
            }
        },
        signed,
        provider,
    )
    if remote != response:
        raise ValueError("Stored remote response differs from its signed artifact")
    if set(inputs) != {"dataset_ids", "unit", "values"}:
        raise ValueError("Provide the exact dataset IDs, unit and values")
    if inputs["dataset_ids"] != [str(item) for item in grant.dataset_ids]:
        raise ValueError("Dataset IDs differ from the signed grant")
    unit, values = inputs["unit"], inputs["values"]
    if unit not in ("C", "K") or not isinstance(values, list) or not 1 <= len(values) <= 1000:
        raise ValueError("Invalid temperature summary inputs")
    if any(type(value) not in (int, float) for value in values):
        raise ValueError("Temperature values must be numbers")
    numbers = [Decimal(str(value)) for value in values]
    result = remote["artifact"]["result"]
    reported = Decimal(str(result["mean_celsius"]))
    if not reported.is_finite() or any(not number.is_finite() for number in numbers):
        raise ValueError("Temperature values must be finite")
    input_digest = digest(
        {
            "schema_version": "orca.provider-qualification/1.0",
            "operation_id": str(grant.task_id),
            "unit": unit,
            "values": values,
        }
    )
    tolerance = Decimal("0.000000001")
    with localcontext() as context:
        context.prec = 80
        expected = sum(numbers) / Decimal(len(numbers))
        if unit == "K":
            expected -= Decimal("273.15")
        difference = abs(expected - reported)
    matched = (
        result["input_digest"] == input_digest
        and result["count"] == len(numbers)
        and result["method"] == grant.method_id
        and difference <= tolerance
    )
    return {
        "verdict": "matched" if matched else "challenged",
        "grant_digest": signed.grant_digest,
        "artifact_digest": remote["artifact_digest"],
        "input_digest": input_digest,
        "recomputed_mean_celsius": str(expected),
        "absolute_difference_celsius": str(difference),
        "comparison_tolerance_celsius": str(tolerance),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Recompute a signed ORCA remote summary offline")
    parser.add_argument("review_bundle", type=Path)
    args = parser.parse_args()
    with args.review_bundle.open("rb") as handle:
        content = handle.read(2_000_001)
    if len(content) > 2_000_000:
        parser.error("review bundle is too large")
    bundle = strict_json(content)
    result = verify_summary(
        SignedResearchGrant.model_validate(bundle["signed_grant"]),
        bundle["coordinator_public"],
        bundle["provider_public"],
        bundle["verified_response"],
        bundle["inputs"],
    )
    print(json.dumps(result, indent=2))
    if result["verdict"] != "matched":
        raise SystemExit(1)


if __name__ == "__main__":
    main()

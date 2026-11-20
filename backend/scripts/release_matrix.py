"""Purpose: reconcile the P14 release route inventory and check required local test evidence.
Behavior: classify by exact method/path and reject missing, duplicate, failed or skipped cases.
Refs: main_implementation plan.md P14.3; api design.md §19; Phase: P14.
"""

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from orca.infrastructure.federation.signing import canonical, strict_json, unb64

ROOT = Path(__file__).resolve().parents[2]
ATTESTATION_DOMAIN = b"orca.local-release-evidence.v1\0"
HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
REQUIRED_LOCAL_CASES = (
    "test_all_enabled_220_op_inventory_checked",
    "test_mock_artifacts_excluded",
    "test_openapi_client_regenerated",
    "test_no_required_skips",
    "test_repro_clean_install",
)


def load_json(relative: str) -> Any:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (method.upper(), path): operation
        for path, path_item in document["paths"].items()
        for method, operation in path_item.items()
        if method in HTTP_METHODS
    }


def release_inventory() -> dict[str, Any]:
    historical = operations(load_json("contracts/designed/orca-proposal.json"))
    selected = operations(load_json("contracts/designed/orca-research-selected.json"))
    runtime = operations(load_json("contracts/openapi/orca-v1.json"))
    rows = load_json("contracts/designed/research-route-dispositions.json")["operations"]
    decisions = {(row["method"], row["path"]): row["disposition"] for row in rows}
    if len(rows) != len(historical) or decisions.keys() != historical.keys():
        raise ValueError("Historical operations and design dispositions differ")
    if selected.keys() != {
        key for key, decision in decisions.items() if decision in {"CORE", "FED", "OPS"}
    }:
        raise ValueError("Selected design differs from route dispositions")
    return {
        "historical_operations": len(historical),
        "selected_design_operations": len(selected),
        "registered_operations": len(runtime),
        "selected_design_registered": len(runtime.keys() & selected.keys()),
        "selected_design_unregistered": len(selected.keys() - runtime.keys()),
        "supplemental_registered": len(runtime.keys() - historical.keys()),
        "historical_outside_selection_registered": [
            f"{method} {path}"
            for method, path in sorted((runtime.keys() & historical.keys()) - selected.keys())
        ],
        "unregistered_selected_operations": [
            {"method": method, "path": path, "design_disposition": decisions[(method, path)]}
            for method, path in sorted(selected.keys() - runtime.keys())
        ],
        "runtime_operations": [
            {
                "method": method,
                "path": path,
                "operation_id": operation["operationId"],
                "design_disposition": decisions.get((method, path), "SUPPLEMENTAL"),
                "security": operation.get("security", []),
            }
            for (method, path), operation in sorted(runtime.items())
        ],
    }


def verify_required_junit(path: Path) -> int:
    """Accept one explicit PASS for every required local test in a single report."""
    cases = list(ET.parse(path).iter("testcase"))
    if not cases:
        raise ValueError("No tests collected")
    if any(case.find(tag) is not None for case in cases for tag in ("failure", "error", "skipped")):
        raise ValueError("Release evidence contains a failed, errored or skipped case")
    names = [case.attrib.get("name") for case in cases]
    for required in REQUIRED_LOCAL_CASES:
        if names.count(required) != 1:
            raise ValueError(f"Expected exactly one result for {required}")
    return len(cases)


def verify_signed_evidence(document: dict[str, Any], public_key: str) -> None:
    evidence = {key: value for key, value in document.items() if key != "signature"}
    if evidence["profile"] != "research-federated":
        raise ValueError("Release evidence must retain the selected federation profile")
    gates = evidence["federation_prerequisites"]
    if set(gates) != {"G09", "G10", "G11"}:
        raise ValueError("Selected federation prerequisite evidence is incomplete")
    if evidence["gate_status"] == "PASS" and any(status != "PASS" for status in gates.values()):
        raise ValueError("Selected federation prerequisites have not passed")
    signature = document["signature"]
    if signature["algorithm"] != "Ed25519" or signature["public_key"] != public_key:
        raise ValueError("Release evidence signer differs from the expected key")
    try:
        Ed25519PublicKey.from_public_bytes(unb64(public_key)).verify(
            unb64(signature["value"]), ATTESTATION_DOMAIN + canonical(evidence)
        )
    except (InvalidSignature, ValueError) as error:
        raise ValueError("Release evidence signature is invalid") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local release inventory or JUnit evidence")
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--junit", type=Path)
    inputs.add_argument("--manifest", type=Path)
    parser.add_argument("--public-key")
    args = parser.parse_args()
    if args.junit is not None:
        try:
            passed = verify_required_junit(args.junit)
        except (OSError, ET.ParseError, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(json.dumps({"status": "LOCAL_CHECKS_PASSED", "cases": passed}))
    elif args.manifest is not None:
        if not args.public_key:
            parser.error("--public-key is required when verifying a signed manifest")
        try:
            verify_signed_evidence(strict_json(args.manifest.read_bytes()), args.public_key)
        except (OSError, KeyError, TypeError, ValueError) as error:
            parser.exit(1, f"{error}\n")
        print(json.dumps({"status": "LOCAL_EVIDENCE_SIGNATURE_VERIFIED"}))
    else:
        print(json.dumps(release_inventory(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

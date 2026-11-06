"""Purpose: convert the approved research-only directive into machine-readable route scope.
Behavior: preserve the historical 220-route inventory while exporting only CORE/FED/OPS routes.
Refs: research-only guide sections 3, 8.4, 10.1 and ADR-R01; Phase: scope migration.
"""

import argparse
import copy
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

EXPECTED_COUNTS = {"CORE": 100, "FED": 47, "OPS": 18, "DEFER": 21, "EXCLUDE": 34}
SELECTED = {"CORE", "FED", "OPS"}
ROW = re.compile(
    r"^\|\s*(P\d{2})\s*\|\s*`(GET|POST|PUT|PATCH|DELETE)`\s*\|\s*`([^`]+)`\s*\|"
    r"\s*\*\*(CORE|FED|OPS|DEFER|EXCLUDE)\*\*\s*\|"
)


def historical_operations(designed: dict[str, Any]) -> dict[tuple[str, str], str]:
    operations: dict[tuple[str, str], str] = {}
    for path, methods in designed.get("paths", {}).items():
        for method, operation in methods.items():
            key = (method.upper(), path)
            if key in operations:
                raise ValueError(f"duplicate historical operation: {key}")
            operations[key] = str(operation["x-orca-phase"])
    if len(operations) != 220:
        raise ValueError(f"historical design must contain 220 operations, found {len(operations)}")
    return operations


def parse_directive(path: Path) -> list[dict[str, str]]:
    operations = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = ROW.match(line)
        if match:
            phase, method, route, disposition = match.groups()
            operations.append(
                {
                    "phase": phase,
                    "method": method,
                    "path": route,
                    "disposition": disposition,
                }
            )
    return operations


def validate_operations(
    operations: list[dict[str, str]], historical: dict[tuple[str, str], str]
) -> None:
    keys = [(entry["method"], entry["path"]) for entry in operations]
    if len(keys) != len(set(keys)):
        raise ValueError("research scope contains duplicate method/path entries")
    counts = Counter(entry["disposition"] for entry in operations)
    if dict(counts) != EXPECTED_COUNTS:
        raise ValueError(f"research scope counts changed: {dict(counts)}")
    if set(keys) != set(historical):
        missing = sorted(set(historical) - set(keys))
        extra = sorted(set(keys) - set(historical))
        raise ValueError(
            f"research scope differs from historical inventory: missing={missing}, extra={extra}"
        )
    for entry in operations:
        key = (entry["method"], entry["path"])
        if historical[key] != entry["phase"]:
            raise ValueError(f"phase changed for {key}: {historical[key]} -> {entry['phase']}")


def scope_documents(root: Path, designed: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    historical = historical_operations(designed)
    guides = sorted(root.glob("ORCA_3_0_Research_Only_Scope_and_Pruning_Guide*.md"))
    committed = root / "contracts/designed/research-route-dispositions.json"
    if len(guides) > 1 and len({guide.read_bytes() for guide in guides}) > 1:
        raise ValueError(
            "conflicting research-only scope guides found; reconcile authoritative content"
        )
    if guides:
        operations = parse_directive(guides[0])
    elif committed.exists():
        operations = json.loads(committed.read_text(encoding="utf-8"))["operations"]
    else:
        raise FileNotFoundError(
            "research-only scope guide or committed disposition manifest required"
        )
    validate_operations(operations, historical)
    operations = sorted(operations, key=lambda item: (item["phase"], item["path"], item["method"]))
    manifest = {
        "schema_version": "orca.route-scope/1.0",
        "decision": "ADR-R01-research-only-scope",
        "decision_date": "2026-11-06",
        "status": "IMPLEMENTATION_DIRECTIVE_RECEIVED",
        "historical_operation_count": 220,
        "selected_operation_count": sum(entry["disposition"] in SELECTED for entry in operations),
        "counts": EXPECTED_COUNTS,
        "operations": operations,
    }
    dispositions = {(entry["method"], entry["path"]): entry["disposition"] for entry in operations}
    selected = copy.deepcopy(designed)
    selected["info"] = {
        "title": "ORCA research-selected design inventory",
        "version": "3.2.0-research-scope",
        "description": (
            "Design-only CORE/FED/OPS ceiling. A route is not implemented or live until its gate passes."
        ),
    }
    selected["paths"] = {}
    for path, methods in designed["paths"].items():
        for method, operation in methods.items():
            disposition = dispositions[(method.upper(), path)]
            if disposition not in SELECTED:
                continue
            selected_operation = copy.deepcopy(operation)
            selected_operation["x-orca-disposition"] = disposition
            selected["paths"].setdefault(path, {})[method] = selected_operation
    selected_count = sum(len(methods) for methods in selected["paths"].values())
    if selected_count != 165:
        raise ValueError(
            f"selected research inventory must contain 165 operations, found {selected_count}"
        )
    return manifest, selected


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def artifacts(root: Path, designed: dict[str, Any]) -> dict[str, str]:
    manifest, selected = scope_documents(root, designed)
    return {
        "designed/research-route-dispositions.json": encode(manifest),
        "designed/orca-research-selected.json": encode(selected),
    }


def export(root: Path, check: bool = False) -> None:
    designed_path = root / "contracts/designed/orca-proposal.json"
    designed = json.loads(designed_path.read_text(encoding="utf-8"))
    for name, content in artifacts(root, designed).items():
        target = root / "contracts" / name
        if check:
            if not target.exists() or target.read_text(encoding="utf-8") != content:
                raise ValueError(f"Generated research scope drift: {name}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    export(Path(__file__).resolve().parents[2], args.check)

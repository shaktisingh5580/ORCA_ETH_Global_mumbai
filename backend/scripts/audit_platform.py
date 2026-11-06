"""Purpose: compare the selected ORCA platform contract with registered runtime operations.
Behavior: enumerate exact missing routes without treating registration or demo code as completion.
Refs: main implementation plan P00-P14; research-only guide 8; Phase: platform completion audit.
"""

import json
from pathlib import Path
from typing import TypedDict

ROOT = Path(__file__).resolve().parents[2]
METHODS = {"get", "post", "put", "patch", "delete"}


class PhaseAudit(TypedDict):
    selected_operations: int
    registered_operations: int
    registered: list[str]
    missing: list[str]
    completion: str


def main() -> None:
    disposition = json.loads(
        (ROOT / "contracts/designed/research-route-dispositions.json").read_text(encoding="utf-8")
    )
    core = json.loads((ROOT / "contracts/openapi/orca-v1.json").read_text(encoding="utf-8"))
    demo = json.loads((ROOT / "contracts/openapi/orca-demo.json").read_text(encoding="utf-8"))
    registered = {
        (method.upper(), path)
        for path, item in core["paths"].items()
        for method in item
        if method in METHODS
    }
    selected = [
        operation
        for operation in disposition["operations"]
        if operation["disposition"] in {"CORE", "FED", "OPS"}
    ]
    phases: dict[str, PhaseAudit] = {}
    for phase in sorted({operation["phase"] for operation in selected}):
        operations = [operation for operation in selected if operation["phase"] == phase]
        present = [
            f"{op['method']} {op['path']}"
            for op in operations
            if (op["method"], op["path"]) in registered
        ]
        missing = [
            f"{op['method']} {op['path']}"
            for op in operations
            if (op["method"], op["path"]) not in registered
        ]
        phases[phase] = {
            "selected_operations": len(operations),
            "registered_operations": len(present),
            "registered": present,
            "missing": missing,
            "completion": "NOT_ESTABLISHED_BY_ROUTE_REGISTRATION",
        }
    report = {
        "audit_date": "2026-11-06",
        "scope": "Research-only federated ORCA prototype and full selected contract",
        "method": "Exact method/path comparison of selected dispositions with generated runtime OpenAPI; component inspection is documented separately",
        "selected_operations": len(selected),
        "registered_selected_operations": sum(p["registered_operations"] for p in phases.values()),
        "missing_selected_operations": sum(len(p["missing"]) for p in phases.values()),
        "demo_only_operations": [
            f"{method.upper()} {path}"
            for path, item in demo["paths"].items()
            for method in item
            if method in METHODS
        ],
        "phases": phases,
        "corrections": [
            "P02's historical G02 PASS is not evidence that all 31 selected P02 operations were implemented; only 7 are registered",
            "P03 has all 16 selected handlers registered but source/QC/baseline science, permissions and deployment acceptance remain incomplete",
            "Descriptive demo computation does not implement the selected P04 anomaly method registry and scientific gates",
            "A confirmed public result reference does not implement P09 A2A, P10 ENS or the 22 P11 coordination APIs",
            "The 109-test pass predates accelerated demo additions; it does not validate the current full platform",
        ],
        "scope_boundaries": [
            "No Marine advisory/chat/voice/general model-training expansion",
            "P12 is research integration and scope regression, not the old 42 legacy operations",
            "P13/P14 acceptance work is additional to route counts",
        ],
        "evidence": [
            "docs/gates/P03/manifest.json",
            "docs/gates/DEMO/manifest.json",
            "docs/gates/DEMO/terminal-demo.json",
            "docs/gates/DEMO/arkiv-publication.json",
        ],
        "not_verified_by_this_audit": [
            "Frontend repository implementation",
            "Current authenticated end-to-end browser journey",
            "Current full automated regressions",
            "Scientific correctness and institutional independence",
        ],
    }
    destination = ROOT / "docs/decisions/platform-implementation-audit.json"
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "selected_operations",
                    "registered_selected_operations",
                    "missing_selected_operations",
                    "demo_only_operations",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()

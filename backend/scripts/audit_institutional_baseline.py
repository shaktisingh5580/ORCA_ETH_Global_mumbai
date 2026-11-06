"""Purpose: inventory the implemented institutional recovery baseline without private settings.
Behavior: inspect generated runtime routes, migrations, pinned packages and protected source hashes.
Refs: ORCA_INSTITUTIONAL_PHASES_AND_ACCEPTANCE.md F01.1; Phase: F01.
"""

import hashlib
import importlib.metadata
import json
from pathlib import Path
from typing import Any

from export_prototype_contract import contract

ROOT = Path(__file__).resolve().parents[2]


def inventory() -> dict[str, Any]:
    schema = json.loads(contract())
    routes = sorted(
        f"{method.upper()} {path}"
        for path, item in schema["paths"].items()
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    )
    source_names = [
        "main_implementation plan.md",
        "api design.md",
        "ORCA_ENSv2_Arkiv_Integration_Blueprint_2026-11-05.md",
        "ORCA_3_0_Research_Only_Scope_and_Pruning_Guide (1).md",
        "ORCA_3_0_Navigation_and_Dashboard_Redesign_Guidelines (1).md",
    ]
    sources = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if (ROOT / name).exists()
        else None
        for name in source_names
    }
    return {
        "phase": "F01.1",
        "profile": "implemented prototype; route presence does not prove acceptance",
        "operations": routes,
        "migrations": sorted(p.name for p in (ROOT / "backend/migrations/versions").glob("*.py")),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("a2a-sdk", "fastapi", "pydantic", "httpx", "rfc8785")
        },
        "protected_sources": sources,
        "source_gate": "PASS" if all(sources.values()) else "BLOCKED_MISSING_LOCAL_SOURCES",
        "capabilities": {
            "operator_pinned_sst_a2a": "implemented; loopback only",
            "institution_owned_onboarding": "not implemented",
            "ens_bindings": "not implemented",
            "mcp_scientific_tools": "not implemented",
            "tabular_reasoning": "implemented; coordinator workers",
            "tabular_a2a_delegation": "not implemented",
            "arkiv_evidence_attestation": "implemented; distinct from delegation",
        },
        "frontend": {
            "owned_workspace": "backend/src/orca/api/dashboard",
            "preserved_route": "/research",
            "external_repository": "https://github.com/vinit-singh2007/ORCA",
            "external_revision": "not inspected in this checkout; not claimed deployed from here",
        },
    }


if __name__ == "__main__":
    target = ROOT / "docs/gates/F01/baseline.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(inventory(), indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {target.relative_to(ROOT)}")

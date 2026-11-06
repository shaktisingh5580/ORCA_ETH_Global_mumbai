"""Purpose: generate reproducible HTTP/schema/frontend contracts from actual DTOs.
Behavior: separate 220 designed routes from four implemented routes; export TS types.
Refs: API sections 3,4,19; main plan P01.2/P01.4; Phase: P01.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr
from pydantic.json_schema import models_json_schema

from orca.api.app import create_app
from orca.api.errors import Problem
from orca.config import Settings
from orca.contracts.common import (
    AccessDecision,
    APIMetadata,
    ChainReference,
    GeoScope,
    IdentityReference,
    Liveness,
    MethodRef,
    MoneyBudget,
    Operation,
    Page,
    Polygon,
    ProvenanceRef,
    ReadinessReport,
    ResearchProject,
    ResourceRef,
    ScientificOutcome,
    ScientificQuantity,
    SourceClock,
    TimeWindow,
)

ROOT = Path(__file__).resolve().parents[2]
MODELS: list[type[BaseModel]] = [
    GeoScope,
    ResourceRef,
    Operation,
    SourceClock,
    ScientificQuantity,
    ProvenanceRef,
    MethodRef,
    AccessDecision,
    ChainReference,
    Problem,
    APIMetadata,
    MoneyBudget,
    IdentityReference,
    Liveness,
    Polygon,
    ReadinessReport,
    ResearchProject,
    ScientificOutcome,
    TimeWindow,
    Page[ResearchProject],
]


def typescript(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return str(schema["$ref"]).split("/")[-1]
    if "const" in schema:
        return json.dumps(schema["const"])
    if "enum" in schema:
        return " | ".join(json.dumps(x) for x in schema["enum"])
    if "oneOf" in schema:
        return " | ".join(typescript(x) for x in schema["oneOf"])
    if "anyOf" in schema:
        return " | ".join(typescript(x) for x in schema["anyOf"])
    kind = schema.get("type")
    if kind == "object":
        fields = [
            f"{json.dumps(k)}{'' if k in schema.get('required', []) else '?'}: {typescript(v)};"
            for k, v in schema.get("properties", {}).items()
        ]
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            fields.append(f"[key: string]: {typescript(additional)};")
        return "{ " + " ".join(fields) + " }"
    if kind == "array":
        if "prefixItems" in schema:
            return "[" + ", ".join(typescript(x) for x in schema["prefixItems"]) + "]"
        return f"Array<{typescript(schema['items'])}>"
    if kind in {"string", "number", "integer", "boolean", "null"}:
        return "number" if kind == "integer" else str(kind)
    raise ValueError(f"Unsupported schema conversion: {schema}")


def artifacts() -> dict[str, str]:
    _, combined = models_json_schema([(m, "validation") for m in MODELS])
    definitions = combined["$defs"]
    schemas = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$defs": definitions}
    # The exporter never starts a server; the ephemeral token is not exported.
    import secrets

    settings = Settings(monitoring_token=SecretStr(secrets.token_urlsafe(48)), environment="test")
    implemented = create_app(settings).openapi()
    source = ROOT / "api design.md"
    if source.exists():
        document = source.read_text(encoding="utf-8")
        inventory = document[document.index("### 3.1 ") : document.index("## 4. Canonical")]
    else:
        # CI excludes local research Markdown by user request. Reuse only the
        # committed design inventory; local G01 still verifies it from source.
        previous = ROOT / "contracts/designed/orca-proposal.json"
        designed = json.loads(previous.read_text(encoding="utf-8"))
        inventory = ""
    designed = designed if not source.exists() else {
        "openapi": "3.1.0",
        "info": {
            "title": "ORCA design-only inventory",
            "version": "3.1.0-proposal",
            "description": "Not a running API. Later-phase DTO details remain to be implemented.",
        },
        "paths": {},
    }
    count = 0
    for line in inventory.splitlines():
        cells = [c.strip().strip("`") for c in line.split("|")[1:-1]]
        if (
            len(cells) < 8
            or not re.fullmatch(r"P\d{2}", cells[0])
            or cells[1] not in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        ):
            continue
        phase, method, path, permission, request, response, status, guards = cells[:8]
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"\{([^}]+)\}", path)
        ]
        op = {
            "operationId": re.sub(r"[^a-zA-Z0-9]+", "_", method.lower() + path).strip("_"),
            "description": guards,
            "x-orca-phase": phase,
            "x-orca-implementation-status": "DESIGNED",
            "x-orca-permission": permission,
            "x-orca-request-design": request,
            "x-orca-response-design": response,
            "x-orca-contract-completeness": "inventory-only",
            "parameters": parameters,
            "responses": {"default": {"description": "Design only: " + status + " " + response}},
        }
        designed["paths"].setdefault(path, {})[method.lower()] = op
        count += 1
    if source.exists() and count != 220:
        raise ValueError(f"Source operation inventory changed: {count}, reconcile contract first")
    if sum(len(operations) for operations in designed["paths"].values()) != 220:
        raise ValueError("Design inventory must contain 220 operations")
    ts = "// Generated by backend/scripts/export_openapi.py; do not edit.\n// Types do not replace runtime schema validation.\n"
    ts += (
        "\n".join(
            f"export type {name} = {typescript(s)};" for name, s in sorted(definitions.items())
        )
        + "\n"
    )

    def encode(value: Any) -> str:
        return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    return {
        "openapi/orca-v1.json": encode(implemented),
        "designed/orca-proposal.json": encode(designed),
        "schemas/common.json": encode(schemas),
        "typescript/orca.ts": ts,
    }


def export(destination: Path, check: bool = False) -> None:
    for name, content in artifacts().items():
        path = destination / name
        if check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                raise ValueError(f"Generated contract drift: {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    export(ROOT / "contracts", args.check)

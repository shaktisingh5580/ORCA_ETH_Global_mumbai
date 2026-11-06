"""Purpose: compute G02 from all named tests and actual database/object-store evidence.
Behavior: report BLOCKED for unavailable live services and FAIL for code/test defects.
Refs: main implementation plan P02 and section 9; Phase: P02.
"""

import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from check_file_headers import violations as header_errors
from check_import_boundaries import violations as import_errors
from run_phase_gate import verify_prerequisite

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
OUT = ROOT / "docs/gates/P02"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cases(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        name = case.attrib["name"]
        if name in result:
            raise ValueError(f"duplicate test ID: {name}")
        result[name] = (
            "FAIL"
            if case.find("failure") is not None or case.find("error") is not None
            else "SKIP"
            if case.find("skipped") is not None
            else "PASS"
        )
    return result


def write(report: dict[str, object]) -> None:
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    reasons = "\n".join(f"- {reason}" for reason in cast(list[str], report.get("reasons", [])))
    (OUT / "report.md").write_text(
        f"# P02 gate report\n\nStatus: **{report['status']}**.\n\n{reasons}\n\n"
        "P02 can pass only with live PostGIS, the non-owner runtime role, and versioned object storage. "
        "Local issuer cryptographic tests do not substitute for a selected deployment issuer.\n",
        encoding="utf-8",
    )


def main() -> int:
    write({"phase": "P02", "gate_id": "G02", "status": "IN_PROGRESS"})
    requirements = json.loads((OUT / "requirements.json").read_text(encoding="utf-8"))
    required = [name for names in requirements["required_tests"].values() for name in names]
    prerequisite_errors = verify_prerequisite(ROOT, "P01")
    if prerequisite_errors:
        write(
            {"phase": "P02", "gate_id": "G02", "status": "BLOCKED", "reasons": prerequisite_errors}
        )
        return 2
    static = header_errors(BACKEND) + import_errors(BACKEND / "src")
    dispositions = json.loads(
        (ROOT / "contracts/designed/research-route-dispositions.json").read_text(encoding="utf-8")
    )
    runtime = json.loads((ROOT / "contracts/openapi/orca-v1.json").read_text(encoding="utf-8"))
    selected = [
        (item["method"].lower(), item["path"])
        for item in dispositions["operations"]
        if item["phase"] == "P02" and item["disposition"] in {"CORE", "FED", "OPS"}
    ]
    missing_routes = [
        f"{method.upper()} {path}"
        for method, path in selected
        if method not in runtime["paths"].get(path, {})
    ]
    static += [f"unimplemented selected P02 operation: {route}" for route in missing_routes]
    junit = OUT / "test-results.xml"
    command = [sys.executable, "-m", "pytest", "-q", f"--junitxml={junit}"]
    process = subprocess.run(command, cwd=BACKEND, capture_output=True, text=True, timeout=900)
    (OUT / "test-results.txt").write_text(process.stdout + process.stderr, encoding="utf-8")
    observed = cases(junit) if junit.exists() else {}
    missing = [name for name in required if name not in observed]
    failed = [name for name in required if observed.get(name) == "FAIL"]
    skipped = [name for name in required if observed.get(name) == "SKIP"]
    if static or missing or failed or (process.returncode not in {0, 5} and not skipped):
        status = "FAIL"
        reasons = (
            static
            + [f"missing test: {name}" for name in missing]
            + [f"failed test: {name}" for name in failed]
        )
    elif skipped:
        status = "BLOCKED"
        reasons = [f"live prerequisite unavailable: {name}" for name in skipped]
    else:
        status, reasons = "PASS", []
    paths = [
        p
        for folder in (
            "backend/src",
            "backend/migrations",
            "backend/tests",
            "backend/scripts",
            "contracts",
            "infra",
        )
        for p in (ROOT / folder).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(paths)}
    report: dict[str, object] = {
        "phase": "P02",
        "gate_id": "G02",
        "status": status,
        "selected_operation_count": len(selected),
        "missing_selected_operations": missing_routes,
        "reasons": reasons,
        "counts": {
            "required": len(required),
            "collected": len(observed),
            "passed": sum(v == "PASS" for v in observed.values()),
            "failed": sum(v == "FAIL" for v in observed.values()),
            "skipped": sum(v == "SKIP" for v in observed.values()),
        },
        "required_test_results": {name: observed.get(name, "MISSING") for name in required},
        "subphase_results": {
            gate: "PASS"
            if all(observed.get(name) == "PASS" for name in names)
            else "BLOCKED"
            if any(observed.get(name) == "SKIP" for name in names)
            else "FAIL"
            for gate, names in requirements["required_tests"].items()
        },
        "environment": {"python": sys.version, "execution": "local"},
        "input_hashes": hashes,
        "source_tree_sha256": hashlib.sha256(
            json.dumps(hashes, sort_keys=True).encode()
        ).hexdigest(),
        "next_phase": "P03" if status == "PASS" else None,
    }
    write(report)
    print(json.dumps({"status": status, "counts": report["counts"], "reasons": reasons}, indent=2))
    return 0 if status == "PASS" else 2 if status == "BLOCKED" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write(
            {
                "phase": "P02",
                "gate_id": "G02",
                "status": "FAIL",
                "reasons": [f"gate execution error: {type(exc).__name__}"],
            }
        )
        raise

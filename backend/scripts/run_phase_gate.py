"""Purpose: compute phase admission from actual tests and current prerequisite evidence.
Behavior: invalidate old success first; reject missing, skipped, stale and live-blocked gates.
Refs: main_implementation plan.md P01.4 and section 9; Phase: P01.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from check_file_headers import violations as header_errors
from check_import_boundaries import violations as import_errors
from export_openapi import export

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
OUT = ROOT / "docs/gates/P01"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_prerequisite(root: Path, phase: str) -> list[str]:
    path = root / "docs/gates" / phase / "manifest.json"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        if report.get("status") != "PASS" or report.get("phase") != phase:
            return [f"{phase}: prerequisite not PASS"]
        hashes = report.get("input_hashes")
        if not isinstance(hashes, dict) or not hashes:
            return [f"{phase}: no source evidence"]
        for relative, expected in hashes.items():
            source = (root / relative).resolve()
            if (
                not source.is_relative_to(root.resolve())
                or not source.is_file()
                or digest(source) != expected
            ):
                return [f"{phase}: missing or changed input {relative}"]
        return []
    except (OSError, ValueError, TypeError):
        return [f"{phase}: missing or invalid report"]


def evaluate_gate(
    required: list[str],
    cases: dict[str, str],
    exit_code: int,
    prerequisite_errors: list[str],
    required_live: list[str],
    live_evidence: dict[str, str],
) -> tuple[str, list[str]]:
    if prerequisite_errors:
        return "BLOCKED", prerequisite_errors
    failures = [
        f"{name}: {cases.get(name, 'MISSING')}" for name in required if cases.get(name) != "PASS"
    ]
    failures += [
        f"{name}: {state}"
        for name, state in cases.items()
        if state != "PASS" and name not in required
    ]
    if not cases:
        failures.append("No tests collected")
    if exit_code:
        failures.append(f"Test command exited {exit_code}")
    if failures:
        return "FAIL", failures
    missing = [name for name in required_live if live_evidence.get(name) != "PASS"]
    if missing:
        return "BLOCKED", [f"Missing live evidence: {name}" for name in missing]
    return "PASS", []


def read_junit(path: Path) -> dict[str, str]:
    root = ET.parse(path).getroot()
    cases: dict[str, str] = {}
    for case in root.iter("testcase"):
        name = case.attrib["name"]
        state = (
            "FAIL"
            if case.find("failure") is not None or case.find("error") is not None
            else "SKIP"
            if case.find("skipped") is not None
            else "PASS"
        )
        if name in cases:
            raise ValueError(f"Duplicate test ID: {name}")
        cases[name] = state
    return cases


def input_hashes() -> dict[str, str]:
    files: set[Path] = set()
    for folder in (
        "backend/src",
        "backend/scripts",
        "backend/tests",
        "backend/fixtures",
        "contracts",
        ".github/workflows",
    ):
        files.update(
            p for p in (ROOT / folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts
        )
    files.update(
        ROOT / p
        for p in (
            "backend/pyproject.toml",
            "backend/uv.lock",
            "backend/.python-version",
            "backend/.env.example",
            "backend/README.md",
            "api design.md",
            "main_implementation plan.md",
            "docs/gates/P01/requirements.json",
            "docs/gates/P00/manifest.json",
            "docs/decisions/P01-contract-reconciliation.md",
        )
    )
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in sorted(files)}


def write_report(report: dict[str, Any]) -> None:
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (OUT / "report.md").write_text(
        f"# P01 gate report\n\nStatus: **{report['status']}**. Execution: {report.get('execution', 'not completed')}.\n\n"
        f"Tests: {json.dumps(report.get('counts', {}))}.\n\n"
        "[Manifest](manifest.json), [requirements](requirements.json), [test output](test-results.txt), "
        "[JUnit](test-results.xml), [clean install](clean-install.txt), [build output](build.txt).\n\n"
        + "\n".join(f"- {reason}" for reason in report.get("reasons", []))
        + "\n\nP01 delivers runtime contracts, the selected research-route inventory and an isolated frontend harness. "
        "Research-foundation readiness remains 503 until G02 passes. Scientific execution and federation "
        "remain unavailable. P02 may continue only with current G00 and G01 PASS evidence. "
        "Rerun after any input change; a stored PASS alone is insufficient.\n",
        encoding="utf-8",
    )


def run() -> int:
    write_report({"phase": "P01", "gate_id": "G01", "status": "IN_PROGRESS"})
    requirements = json.loads((OUT / "requirements.json").read_text(encoding="utf-8"))
    required = [name for names in requirements["required_tests"].values() for name in names]
    prereqs = [
        e for phase in requirements["prerequisites"] for e in verify_prerequisite(ROOT, phase)
    ]
    if prereqs:
        write_report({"phase": "P01", "gate_id": "G01", "status": "BLOCKED", "reasons": prereqs})
        return 1
    errors = header_errors(BACKEND) + import_errors(BACKEND / "src")
    export(ROOT / "contracts", check=True)
    before = input_hashes()
    env = dict(
        os.environ,
        UV_CACHE_DIR=str(ROOT / ".cache/uv"),
        UV_PYTHON_INSTALL_DIR=str(ROOT / ".cache/python"),
    )
    build_command = ["uv", "build", "--offline", "--out-dir", "dist"]
    build = subprocess.run(
        build_command, cwd=BACKEND, env=env, capture_output=True, text=True, timeout=180
    )
    (OUT / "build.txt").write_text(build.stdout + build.stderr, encoding="utf-8")
    if build.returncode:
        errors.append("Offline release build failed; see build.txt")
    junit = OUT / "test-results.xml"
    junit.unlink(missing_ok=True)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/unit/test_foundation.py",
        "tests/contracts/test_common_schemas.py",
        "tests/contracts/test_mock_scenarios.py",
        "tests/contracts/test_gate_and_openapi.py",
        "tests/contracts/test_research_scope.py",
        f"--junitxml={junit}",
    ]
    tests = subprocess.run(
        command, cwd=BACKEND, env=env, capture_output=True, text=True, timeout=600
    )
    (OUT / "test-results.txt").write_text(tests.stdout + tests.stderr, encoding="utf-8")
    cases = read_junit(junit) if junit.exists() else {}
    if before != input_hashes():
        errors.append("Source changed during gate execution")
    status, reasons = evaluate_gate(
        required, cases, tests.returncode, [], requirements["required_live_evidence"], {}
    )
    if errors:
        status, reasons = "FAIL", reasons + errors
    report = {
        "phase": "P01",
        "gate_id": "G01",
        "status": status,
        "reasons": reasons,
        "execution": "CI" if os.environ.get("CI") == "true" else "local; no remote CI run claimed",
        "environment": {"python": sys.version, "platform": sys.platform},
        "commands": [build_command, command],
        "input_hashes": before,
        "source_tree_sha256": hashlib.sha256(
            json.dumps(before, sort_keys=True).encode()
        ).hexdigest(),
        "counts": {
            "required": len(required),
            "collected": len(cases),
            "passed": sum(s == "PASS" for s in cases.values()),
            "failed": sum(s == "FAIL" for s in cases.values()),
            "skipped": sum(s == "SKIP" for s in cases.values()),
        },
        "required_test_results": {name: cases.get(name, "MISSING") for name in required},
        "subphase_results": {
            gate: status
            if errors
            else "PASS"
            if all(cases.get(n) == "PASS" for n in names) and status == "PASS"
            else status
            for gate, names in requirements["required_tests"].items()
        },
        "evidence_hashes": {
            p.name: digest(p) for p in OUT.iterdir() if p.suffix in {".txt", ".xml"}
        },
        "next_phase": "P02" if status == "PASS" else None,
    }
    write_report(report)
    print(json.dumps({k: report[k] for k in ("status", "counts", "reasons")}, indent=2))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["P01"])
    parser.parse_args()
    try:
        code = run()
    except Exception as exc:
        write_report(
            {
                "phase": "P01",
                "gate_id": "G01",
                "status": "FAIL",
                "reasons": [f"Gate input/execution error: {type(exc).__name__}"],
            }
        )
        raise
    raise SystemExit(code)

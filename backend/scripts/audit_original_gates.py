"""Purpose: distinguish historical gate passes from unchanged current evidence.
Behavior: report every missing or changed hashed input without rewriting gate manifests.
Refs: main implementation plan P00-P03; ADR-R01; original-scope-recovery decision.
Phase: P00-P02 prerequisite recovery. This audit never certifies a release.
"""

import argparse
import hashlib
import json
from pathlib import Path


def audit_gate(root: Path, phase: str) -> dict[str, object]:
    """Check recorded input bytes, retaining failures rather than stopping at the first."""
    manifest = root / "docs" / "gates" / phase / "manifest.json"
    issues: list[dict[str, str]] = []
    try:
        report = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("manifest must be an object")
    except (OSError, ValueError):
        return {"phase": phase, "status": "BLOCKED", "issues": [{"reason": "unreadable_manifest"}]}
    if report.get("phase") != phase or report.get("status") != "PASS":
        issues.append({"reason": "no_recorded_phase_pass"})
    hashes = report.get("input_hashes")
    if not isinstance(hashes, dict) or not hashes:
        issues.append({"reason": "missing_input_hashes"})
        hashes = {}
    for relative, expected in sorted(hashes.items()):
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()):
            issues.append({"path": relative, "reason": "outside_workspace"})
            continue
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            issues.append({"path": relative, "reason": "missing_or_unreadable"})
            continue
        if actual != expected:
            issues.append(
                {"path": relative, "reason": "changed", "expected": str(expected), "actual": actual}
            )
    return {
        "phase": phase,
        "historical_status": report.get("status"),
        "status": "BLOCKED" if issues else "RECORDED_INPUTS_UNCHANGED",
        "checked_inputs": len(hashes),
        "issues": issues,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    gates = [audit_gate(root, phase) for phase in ("P00", "P01", "P02")]
    report = {
        "schema_version": "orca.original-gate-audit/1",
        "scope": "Recorded inputs only; does not run tests or verify live services",
        "full_gate_pass_claimed": False,
        "gates": gates,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({str(g["phase"]): g["status"] for g in gates}))
    return int(any(g["status"] == "BLOCKED" for g in gates))


if __name__ == "__main__":
    raise SystemExit(main())

"""Purpose: independently compare submitted numerical claims with approved computation evidence.
Behavior: emit stable structured findings; never delegate numerical validity to model prose.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03.
"""

from typing import Any

from orca.contracts.review import CandidateResult


def compare_candidate(
    candidate: CandidateResult, plan_digest: str, artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    def add(kind: str, dataset: str, expected: Any, observed: Any) -> None:
        findings.append(
            {
                "id": f"finding-{len(findings) + 1}",
                "kind": kind,
                "dataset_id": dataset,
                "expected": expected,
                "observed": observed,
                "evidence_ref": f"approved-result:{dataset}",
            }
        )

    if candidate.plan_digest != plan_digest:
        add("plan_mismatch", "plan", plan_digest, candidate.plan_digest)
    expected_ids = {str(a["dataset_id"]) for a in artifacts}
    for identifier in set(candidate.means_celsius) | set(candidate.evidence_digests):
        if identifier not in expected_ids:
            add("unexpected_dataset", identifier, None, "submitted")
    for artifact in artifacts:
        identifier = str(artifact["dataset_id"])
        expected = artifact["summary"]["mean_celsius"]
        observed = candidate.means_celsius.get(identifier)
        if observed is None or abs(expected - observed) > 1e-9:
            add("numerical_mismatch", identifier, expected, observed)
        if candidate.evidence_digests.get(identifier) != artifact["artifact_digest"]:
            add(
                "evidence_mismatch",
                identifier,
                artifact["artifact_digest"],
                candidate.evidence_digests.get(identifier),
            )
    return findings

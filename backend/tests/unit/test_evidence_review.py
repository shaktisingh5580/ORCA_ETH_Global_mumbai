"""Purpose: verify reviewer grounding on real comparisons with synthetic candidate controls.
Behavior: reject invented findings and preserve clean agreement without forced disagreement.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03.
"""

from review_test_model import ReviewModel

from orca.application.review_loop import reason_about_review
from orca.contracts.review import CandidateResult
from orca.domain.review import compare_candidate


def test_clean_and_inconsistent_candidates():
    candidate = CandidateResult(
        label="Synthetic",
        source_mode="synthetic_demo",
        plan_digest="sha256:" + "a" * 64,
        means_celsius={"sample": 27},
        evidence_digests={"sample": "sha256:" + "b" * 64},
    )
    artifacts = [
        {
            "dataset_id": "sample",
            "summary": {"mean_celsius": 27},
            "artifact_digest": "sha256:" + "b" * 64,
        }
    ]
    assert compare_candidate(candidate, candidate.plan_digest, artifacts) == []
    candidate.means_celsius["sample"] = 28
    findings = compare_candidate(candidate, candidate.plan_digest, artifacts)
    assert len(findings) == 1 and findings[0]["expected"] == 27 and findings[0]["observed"] == 28


def test_model_review_can_agree_and_challenge():
    for findings in (
        [],
        [{"id": "finding-1", "kind": "numerical_mismatch", "expected": 27, "observed": 28}],
    ):
        result = reason_about_review({"findings": findings}, ReviewModel(), lambda k, d: None)
        assert result["reviewer"]["verdict"] == ("challenged" if findings else "agrees")

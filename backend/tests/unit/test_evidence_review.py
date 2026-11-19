"""Purpose: verify reviewer grounding on real comparisons with synthetic candidate controls.
Behavior: reject invented findings and preserve clean agreement without forced disagreement.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03.
"""

import pytest
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


@pytest.mark.parametrize(
    ("claim", "reviewer_override", "coordinator_override", "expected"),
    [
        (27.0, "challenged", "recompute_approved_plan", "agrees"),
        (28.0, "agrees", "no_change", "challenged"),
    ],
    ids=["false-challenge", "conflicting-provider-result"],
)
def test_false_challenge_and_provider_conflict(
    claim, reviewer_override, coordinator_override, expected
):
    plan_digest = "sha256:" + "a" * 64
    evidence_digest = "sha256:" + "b" * 64
    candidate = CandidateResult(
        label="Provider test candidate",
        source_mode="synthetic_demo",
        plan_digest=plan_digest,
        means_celsius={"sample": claim},
        evidence_digests={"sample": evidence_digest},
    )
    reference = {
        "dataset_id": "sample",
        "summary": {"mean_celsius": 27.0},
        "artifact_digest": evidence_digest,
    }
    findings = compare_candidate(candidate, plan_digest, [reference])
    assert [item["kind"] for item in findings] == (
        ["numerical_mismatch"] if expected == "challenged" else []
    )
    if findings:
        assert findings[0]["expected"] == 27.0
        assert findings[0]["observed"] == claim

    class ConflictingModel(ReviewModel):
        def turn(self, instruction, contents, tools):
            if len(contents) == 3:
                reviewer = "You are ORCA's reviewer" in instruction
                args = {
                    "summary": "Wrong verdict despite the supplied comparison.",
                    "finding_ids": [item["id"] for item in findings],
                }
                if reviewer:
                    args.update(verdict=reviewer_override, limitations=["Test response"])
                else:
                    args["action"] = coordinator_override
                return {
                    "model": "test-only",
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": "submit_decision", "args": args}}],
                    },
                }
            if len(contents) > 3:
                # Retry from the prior evidence tool response after the rejection.
                contents = contents[:3]
            return super().turn(instruction, contents, tools)

    events = []
    decisions = reason_about_review(
        {"findings": findings, "candidate": candidate.model_dump(), "artifacts": [reference]},
        ConflictingModel(),
        lambda name, event: events.append((name, event)),
    )
    assert [event["error"] for name, event in events if name == "tool_rejected"] == [
        "Verdict conflicts with numerical evidence",
        "Correction action conflicts with review evidence",
    ]
    assert decisions["reviewer"]["verdict"] == expected
    assert decisions["coordinator"]["action"] == (
        "recompute_approved_plan" if findings else "no_change"
    )
    assert reference["summary"]["mean_celsius"] == 27.0

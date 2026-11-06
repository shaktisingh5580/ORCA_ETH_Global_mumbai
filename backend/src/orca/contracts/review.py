"""Purpose: describe evidence-linked review submissions and correction decisions.
Behavior: require bounded numerical claims and exact approved-plan references.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03.
"""

from typing import Literal

from pydantic import Field

from orca.contracts.common import Contract


class CandidateResult(Contract):
    label: str = Field(min_length=1, max_length=120)
    source_mode: Literal["uploaded", "synthetic_demo", "computed"]
    plan_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    means_celsius: dict[str, float] = Field(min_length=1, max_length=4)
    evidence_digests: dict[str, str] = Field(min_length=1, max_length=4)


class ReviewDecision(Contract):
    verdict: Literal["agrees", "challenged"]
    summary: str = Field(min_length=1, max_length=3000)
    finding_ids: list[str] = Field(max_length=16)
    limitations: list[str] = Field(min_length=1, max_length=10)


class CorrectionProposal(Contract):
    action: Literal["recompute_approved_plan", "request_information", "no_change"]
    summary: str = Field(min_length=1, max_length=3000)
    finding_ids: list[str] = Field(max_length=16)


class CorrectionApproval(Contract):
    approve: Literal[True]
    candidate_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

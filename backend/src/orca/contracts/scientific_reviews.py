"""Purpose: define bounded P07 reviewer grants, challenges and review records.
Behavior: keep scientific disagreements explicit and separate from proof of independent operation.
Refs: main_implementation plan.md P07.2; api design.md §§3.8, 4.5; Phase: P07.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from orca.contracts.common import Contract, Digest, UTCDateTime

DiscrepancyType = Literal[
    "wrong_baseline", "numerical_mismatch", "method_mismatch", "missing_source", "unsupported_claim"
]


class ReviewGrantRequest(Contract):
    reviewer_subject: str = Field(min_length=1, max_length=255)
    expires_at: UTCDateTime


class ReviewGrantView(Contract):
    id: UUID
    artifact_id: UUID
    reviewer_subject: str
    granted_by: str
    expires_at: UTCDateTime
    revoked_at: UTCDateTime | None
    created_at: UTCDateTime


class ChallengeRequest(Contract):
    discrepancy_type: DiscrepancyType
    source_refs: list[UUID] = Field(min_length=1, max_length=64)
    statement: str = Field(min_length=10, max_length=4000)

    @field_validator("source_refs")
    @classmethod
    def unique_sources(cls, values: list[UUID]) -> list[UUID]:
        if len(set(values)) != len(values):
            raise ValueError("challenge source references must be unique")
        return values


class ChallengeView(Contract):
    id: UUID
    artifact_id: UUID
    reviewer_subject: str
    discrepancy_type: DiscrepancyType
    source_refs: list[UUID]
    statement: str
    status: Literal["open", "responded"] = "open"
    created_at: UTCDateTime


class ChallengeResponseRequest(Contract):
    response_artifact_id: UUID
    statement: str = Field(min_length=10, max_length=4000)


class ChallengeResponseView(Contract):
    id: UUID
    challenge_id: UUID
    responder_subject: str
    response_artifact_id: UUID
    statement: str
    created_at: UTCDateTime


class IndependentReviewRequest(Contract):
    verdict: Literal["agrees", "disagrees", "inconclusive"]
    statement: str = Field(min_length=10, max_length=4000)
    reproduction_digest: Digest | None = None


class IndependentReviewView(Contract):
    id: UUID
    artifact_id: UUID
    reviewer_subject: str
    verdict: Literal["agrees", "disagrees", "inconclusive"]
    statement: str
    reproduction_digest: Digest | None
    independence_verified: bool
    created_at: UTCDateTime

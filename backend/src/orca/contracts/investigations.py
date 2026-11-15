"""Purpose: define private investigation drafts, revisioned plans and their wire views.
Behavior: validate bounded research scope and expose explicit revisions and ETags.
Refs: main_implementation plan.md P05.1; api design.md §§2.5, 3.6; Phase: P05.
"""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract, Digest, GeoScope, TimeWindow, UTCDateTime
from orca.contracts.research_plans import TypedPlan


class CreateInvestigation(Contract):
    project_id: UUID
    question: str = Field(min_length=10, max_length=2000)
    geo_scope: GeoScope
    time_window: TimeWindow
    as_of: UTCDateTime | None = None
    risk_tier: str = Field(min_length=1, max_length=64)


class PatchInvestigation(Contract):
    question: str | None = Field(default=None, min_length=10, max_length=2000)
    geo_scope: GeoScope | None = None
    time_window: TimeWindow | None = None
    description: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def has_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one investigation field must be supplied")
        for field in ("question", "geo_scope", "time_window"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be cleared")
        return self


class InvestigationView(Contract):
    id: UUID
    project_id: UUID
    question: str
    description: str | None
    geo_scope: GeoScope
    time_window: TimeWindow
    as_of: datetime | None
    risk_tier: str
    state: Literal["DRAFT", "PLAN_PENDING", "PLAN_READY", "APPROVED"]
    # A plan is not a result. These fields deliberately remain explicit until a
    # later worker phase establishes source readiness and a scientific outcome.
    source_readiness: Literal["not_assessed", "unverified"]
    scientific_status: Literal["not_run"]
    current_plan_revision: int
    version: int
    created_at: UTCDateTime
    updated_at: UTCDateTime
    etag: str


class InvestigationPlanView(Contract):
    id: UUID
    investigation_id: UUID
    project_id: UUID
    revision: int
    plan: TypedPlan
    plan_digest: Digest
    created_by: str
    created_at: UTCDateTime
    etag: str


class ApprovePlan(Contract):
    """Exact plan acknowledgement; the digest prevents approval drift."""

    digest: Digest
    accepted_assumptions: list[str] = Field(default_factory=list, max_length=24)
    approver_comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def unique_assumptions(self) -> Self:
        if len(set(self.accepted_assumptions)) != len(self.accepted_assumptions):
            raise ValueError("accepted assumptions must be unique")
        if any(not item or len(item) > 500 for item in self.accepted_assumptions):
            raise ValueError("accepted assumptions must contain 1 to 500 characters")
        return self


class ApprovedPlanView(Contract):
    id: UUID
    investigation_id: UUID
    plan_id: UUID
    project_id: UUID
    plan_digest: Digest
    accepted_assumptions: list[str]
    approver_comment: str | None
    approved_by: str
    approved_at: UTCDateTime


class StartRun(Contract):
    plan_id: UUID
    digest: Digest
    execution_budget: str = Field(pattern=r"^(?:0|[1-9]\d*)(?:\.\d{1,8})?$")


class RunView(Contract):
    id: UUID
    investigation_id: UUID
    plan_id: UUID
    state: Literal[
        "QUEUED",
        "RUNNING",
        "CANCEL_REQUESTED",
        "CANCELLED",
        "FAILED",
        "SUCCEEDED_SUPPORTED",
        "SUCCEEDED_INSUFFICIENT",
        "INCONCLUSIVE",
    ]
    stage: str
    execution_budget: str
    created_at: UTCDateTime
    updated_at: UTCDateTime


class RunOutcome(Contract):
    status: Literal["supported", "insufficient_evidence", "inconclusive", "invalid"]
    artifact_ids: list[UUID]
    limitations: list[str]
    evidence_manifest_id: UUID | None = None

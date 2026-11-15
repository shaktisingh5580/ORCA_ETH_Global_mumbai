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

"""Purpose: define the deliberately bounded hackathon question-to-report API.
Behavior: expose guided plans, real execution state and honest prototype result labels.
Refs: HACKATHON_IMPLEMENTATION_PLAN.md H02-H04; FRONTEND_BACKEND_HANDOFF.md; Phase: H02/H03.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Digest, UTCDateTime


class PrototypeConfig(Contract):
    profile: Literal["hackathon-prototype"] = "hackathon-prototype"
    source_mode: Literal["real"] = "real"
    supabase_url: str | None
    supabase_anon_key: str | None
    gemini_configured: bool
    study: str
    baseline: str
    showcase_available: bool


class CreateInvestigation(Contract):
    project_id: UUID
    question: str = Field(min_length=10, max_length=2000)


class PrototypePlan(Contract):
    method: Literal["orca.native-seasonal-sst/hackathon-v1"] = (
        "orca.native-seasonal-sst/hackathon-v1"
    )
    planner: Literal["guided", "gemini"] = "guided"
    source_mode: Literal["real"] = "real"
    summary: str
    scope: str
    baseline: str
    steps: list[str]
    bundle_digest: Digest
    limitations: list[str]


class Investigation(Contract):
    id: UUID
    project_id: UUID
    question: str
    state: Literal["planned", "approved"]
    plan: PrototypePlan
    created_at: UTCDateTime


class ApprovePlan(Contract):
    approve: Literal[True]


class CreateRun(Contract):
    project_id: UUID
    investigation_id: UUID


class PrototypeRun(Contract):
    id: UUID
    project_id: UUID
    investigation_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: str
    result: dict[str, Any] | None = None
    result_digest: Digest | None = None
    error_code: str | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class ApprovePublication(Contract):
    approve_public_reference: Literal[True]

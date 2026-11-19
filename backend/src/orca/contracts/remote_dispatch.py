"""Purpose: expose bounded P09 remote research intent and transport status contracts.
Behavior: distinguish verified transport artifacts from scientific acceptance and remote cancellation.
Refs: main_implementation plan.md P09.3; api design.md §§3.6,17.1; Phase: P09.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Digest, Money, UTCDateTime


class CreateRemoteDispatch(Contract):
    agent_id: UUID
    capability_id: str = Field(min_length=1, max_length=100)
    method_id: str = Field(min_length=1, max_length=120)
    budget_usd: Money
    endpoint: str | None = Field(default=None, max_length=500)
    fallback_from: UUID | None = None


class RemoteDispatchView(Contract):
    id: UUID
    run_id: UUID
    agent_id: UUID
    binding_version: UUID
    capability_id: str
    method_id: str
    plan_digest: Digest
    dataset_ids: list[UUID]
    budget_usd: Money
    state: Literal[
        "queued", "leased", "completed", "failed", "cancelled", "reconciliation_required"
    ]
    generation: int
    grant_digest: Digest | None
    result_digest: Digest | None
    verified_response: dict[str, Any] | None
    error_code: str | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    scientific_acceptance: Literal["not_evaluated"] = "not_evaluated"
    remote_cancellation_confirmed: bool = False
    endpoint: str | None = None
    fallback_from: UUID | None = None


class RemoteDispatchEvent(Contract):
    sequence: int
    kind: str
    run_id: UUID
    agent_id: UUID
    binding_version: UUID
    generation: int
    state: str
    grant_digest: Digest | None
    result_digest: Digest | None
    remote_task_id: str | None
    remote_context_id: str | None
    error_code: str | None
    remote_cancellation_confirmed: bool
    observed_at: UTCDateTime


class RemoteDispatchHistory(Contract):
    items: list[RemoteDispatchEvent]
    next_after: int | None

"""Purpose: define the bounded agent-coordination and frontend API contracts.
Behavior: require explicit disclosure, scoped IDs, immutable bindings and signed evidence.
Refs: H04-agent-coordination.json; canonical agent/database guides; Phase: H04.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, Digest

Capability = Literal["sst_reproduction", "sst_review"]


class AgentBinding(Contract):
    id: UUID
    binding_version: UUID
    name: str = Field(min_length=1, max_length=100)
    capability: Capability
    endpoint: str
    public_key: str
    key_id: str = Field(min_length=1, max_length=100)
    wallet: str = Field(pattern=r"^0x[0-9a-fA-F]{40}$")
    operator_mode: Literal["team_controlled"] = "team_controlled"
    identity_status: Literal["operator_pinned_not_ens_verified"] = (
        "operator_pinned_not_ens_verified"
    )


class CreateNetworkTask(Contract):
    project_id: UUID
    source_run_id: UUID
    capability: Capability = "sst_reproduction"
    review_of: UUID | None = None
    approve_public_reference: bool = False
    deadline_seconds: int = Field(default=3600, ge=120, le=86400)


class RequestClaim(Contract):
    provider_id: UUID


class AssignClaim(Contract):
    claim_id: UUID
    expected_generation: int = Field(ge=0)


class CancelNetworkTask(Contract):
    expected_generation: int = Field(ge=0)


class NetworkOperation(Contract):
    id: UUID
    task_id: UUID
    action: str
    status: Literal["queued", "running", "succeeded", "failed", "reconciliation_required"]
    error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class NetworkTask(Contract):
    id: UUID
    project_id: UUID
    source_run_id: UUID
    capability: Capability
    review_of: UUID | None
    publication_mode: Literal["private", "public_reference"]
    state: str
    generation: int
    recipe_digest: Digest
    deadline: datetime
    created_at: datetime
    claims: list[dict[str, Any]] = Field(default_factory=list)
    assignments: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    operations: list[NetworkOperation] = Field(default_factory=list)


class SignedEnvelope(Contract):
    payload: dict[str, Any]
    content_digest: Digest
    key_id: str
    signature: str
    algorithm: Literal["Ed25519"] = "Ed25519"

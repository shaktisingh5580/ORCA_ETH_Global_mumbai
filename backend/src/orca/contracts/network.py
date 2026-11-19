"""Purpose: define the bounded agent-coordination and frontend API contracts.
Behavior: require explicit disclosure, scoped IDs, immutable bindings and signed evidence.
Refs: H04-agent-coordination.json; main_implementation plan.md P09.1 and P11.2. Phase: H04/P09/P11.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.agents import AdditionalAgentEndpoint, CapabilityID
from orca.contracts.common import Contract, Digest, UTCDateTime

Capability = Literal["sst_reproduction", "sst_review"]


class AgentEndpointObservation(Contract):
    agent_id: UUID
    binding_version: UUID
    observed_at: UTCDateTime
    status: Literal["matches_registration", "card_mismatch", "unavailable"]
    endpoint: str | None = None


class AgentEndpointJob(Contract):
    id: UUID
    agent_id: UUID
    state: Literal["queued", "running", "succeeded", "failed"]
    error_code: str | None
    endpoint: str | None = None


class AgentEndpointCheckJob(AgentEndpointJob):
    report: AgentEndpointObservation | None


class AgentEndpointQualificationJob(AgentEndpointJob):
    report: dict[str, Any] | None


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
    correction_of: UUID | None = None
    approve_public_reference: bool = False
    publish_arkiv_v1: bool = False
    deadline_seconds: int = Field(default=3600, ge=120, le=86400)


class RequestClaim(Contract):
    provider_id: UUID


class ImportClaim(Contract):
    provider_id: UUID
    entity_key: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    arkiv_v1_entity_key: str | None = Field(default=None, pattern=r"^0x[0-9a-fA-F]{64}$")


class AssignClaim(Contract):
    claim_id: UUID
    expected_generation: int = Field(ge=0)


class ApproveArkivEvidence(Contract):
    approve_public_reference: Literal[True]


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
    correction_of: UUID | None = None
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


class FederatedAgentSummary(Contract):
    """Project an existing registration without implying ENS or institutional trust."""

    id: UUID
    organization_id: UUID
    name: str
    capability: str
    provider_mode: Literal["team_controlled", "self_declared_external"]
    state: Literal[
        "draft", "verification_pending", "verified", "qualified", "active", "suspended", "revoked"
    ]
    qualification_expires_at: datetime | None
    service_eligible: bool
    ens_verified: Literal[False] = False
    institutional_affiliation_verified: Literal[False] = False
    research_dispatch_enabled: Literal[False] = False


class FederatedAgentDetail(FederatedAgentSummary):
    """Expose public registration claims while keeping trust decisions explicit."""

    version: int
    endpoint: str
    key_id: str
    public_key: str
    card_digest: Digest
    additional_capabilities: list[CapabilityID] = Field(default_factory=list)
    additional_endpoints: list[AdditionalAgentEndpoint] = Field(default_factory=list)
    qualified_secondary_endpoints: list[str] = Field(default_factory=list)


class AgentCapabilityClaim(Contract):
    capability_id: str
    declaration: Literal["self_declared"] = "self_declared"
    qualified_for_service: bool
    approved_scientific_method: Literal[False] = False
    research_dispatch_enabled: Literal[False] = False


class AgentEndpointClaim(Contract):
    transport: Literal["a2a-jsonrpc/0.3.0"] = "a2a-jsonrpc/0.3.0"
    uri: str
    card_url: str
    card_digest: Digest
    service_eligible: bool
    research_dispatch_enabled: Literal[False] = False
    capabilities: list[CapabilityID] = Field(default_factory=list)
    primary: bool = True

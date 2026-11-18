"""Purpose: bind a proposed remote research task to one approved ORCA scope.
Behavior: issue and verify short-lived Ed25519 grants only for eligible exact bindings.
Refs: main_implementation plan.md P09.3; api design.md §§2.2,17.1; Phase: P09.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import Field

from orca.contracts.common import Contract, Digest
from orca.infrastructure.federation.signing import b64, canonical, digest, unb64

DOMAIN = "orca.scientific-task-grant/1.0"


class ResearchGrant(Contract):
    schema_version: Literal["orca.scientific-task-grant/1.0"] = "orca.scientific-task-grant/1.0"
    audience_agent_id: UUID
    binding_version: UUID
    organization_id: UUID
    project_id: UUID
    run_id: UUID
    task_id: UUID
    capability_id: str = Field(min_length=1, max_length=100)
    approved_plan_digest: Digest
    method_id: str = Field(min_length=1, max_length=120)
    dataset_ids: list[UUID] = Field(min_length=1, max_length=20)
    max_cost_usd: Decimal = Field(gt=0, le=100)
    expires_at: datetime


class SignedResearchGrant(Contract):
    grant: ResearchGrant
    grant_digest: Digest
    key_id: str = Field(min_length=1, max_length=100)
    signature: str


class ResearchDispatchScope(Contract):
    """Current authorization facts loaded from durable run and provider state."""

    agent_id: UUID
    binding_version: UUID
    organization_id: UUID
    project_id: UUID
    run_id: UUID
    task_id: UUID
    capability_id: str
    approved_plan_digest: Digest
    approved_method_id: str
    permitted_dataset_ids: set[UUID]
    available_budget_usd: Decimal
    service_eligible: bool
    research_dispatch_enabled: bool


def _payload(grant: ResearchGrant) -> dict[str, object]:
    return grant.model_dump(mode="json")


def _statement(grant_digest: str, key_id: str) -> dict[str, str]:
    return {"domain": DOMAIN, "grant_digest": grant_digest, "key_id": key_id}


def validate_scope(
    grant: ResearchGrant,
    *,
    agent_id: UUID,
    binding_version: UUID,
    organization_id: UUID,
    project_id: UUID,
    run_id: UUID,
    task_id: UUID,
    capability_id: str,
    approved_plan_digest: str,
    approved_method_id: str,
    permitted_dataset_ids: set[UUID],
    available_budget_usd: Decimal,
    service_eligible: bool,
    research_dispatch_enabled: bool,
    now: datetime | None = None,
) -> None:
    """Fail closed on stale trust, unrelated data or a grant outside the run budget."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or grant.expires_at.tzinfo is None:
        raise ValueError("Grant times must be timezone-aware")
    if not service_eligible or not research_dispatch_enabled:
        raise PermissionError("Provider is not authorized for research dispatch")
    if (
        grant.audience_agent_id != agent_id
        or grant.binding_version != binding_version
        or grant.organization_id != organization_id
        or grant.project_id != project_id
        or grant.run_id != run_id
        or grant.task_id != task_id
        or grant.capability_id != capability_id
        or grant.approved_plan_digest != approved_plan_digest
        or grant.method_id != approved_method_id
    ):
        raise PermissionError("Grant differs from the current approved binding")
    if (
        len(set(grant.dataset_ids)) != len(grant.dataset_ids)
        or not set(grant.dataset_ids) <= permitted_dataset_ids
    ):
        raise PermissionError("Grant includes unapproved datasets")
    if grant.max_cost_usd > available_budget_usd:
        raise PermissionError("Grant exceeds the reserved run budget")
    if not current < grant.expires_at <= current + timedelta(minutes=5):
        raise PermissionError("Grant expired or exceeds the five-minute lifetime")


def sign_grant(grant: ResearchGrant, private_key: str, key_id: str) -> SignedResearchGrant:
    grant_digest = digest(_payload(grant))
    signature = Ed25519PrivateKey.from_private_bytes(unb64(private_key)).sign(
        canonical(_statement(grant_digest, key_id))
    )
    return SignedResearchGrant(
        grant=grant, grant_digest=grant_digest, key_id=key_id, signature=b64(signature)
    )


def verify_grant(signed: SignedResearchGrant, public_key: str, key_id: str) -> ResearchGrant:
    if signed.key_id != key_id or signed.grant_digest != digest(_payload(signed.grant)):
        raise ValueError("Grant signature context or digest mismatch")
    try:
        Ed25519PublicKey.from_public_bytes(unb64(public_key)).verify(
            unb64(signed.signature), canonical(_statement(signed.grant_digest, key_id))
        )
    except (InvalidSignature, ValueError) as error:
        raise ValueError("Invalid research grant signature") from error
    return signed.grant

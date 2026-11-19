"""Purpose: define a bounded P10 read-only ENS observation contract.
Behavior: distinguish finalized name state from authorization and institutional proof.
Refs: main_implementation plan.md P10.4; api design.md §3.11; Phase: P10.
"""

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract


class ResolveENSRequest(Contract):
    organization_id: UUID
    name: str = Field(min_length=5, max_length=253)
    ens_chain_id: Literal[11155111]


class ENSResolutionSnapshot(Contract):
    name: str
    ens_chain_id: Literal[11155111] = 11155111
    status: str
    records: dict[str, str]
    name_active: bool = False
    controller: str | None = None
    resolver: str | None = None
    observed_block: int | None = None
    block_hash: str | None = None
    source_revision: str | None = None
    finalized: bool | None = None
    authority_verified: Literal[False] = False
    institutional_affiliation_verified: Literal[False] = False
    research_dispatch_enabled: Literal[False] = False


Address = Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]


class CompareENSResolversRequest(Contract):
    organization_id: UUID
    first_name: str = Field(min_length=5, max_length=253)
    second_name: str = Field(min_length=5, max_length=253)
    ens_chain_id: Literal[11155111]


class ENSResolverObservation(Contract):
    name: str
    controller: Address
    resolver: Address


class ENSResolverBoundary(Contract):
    ens_chain_id: Literal[11155111]
    status: Literal["DISTINCT_RESOLVERS", "SHARED_RESOLVER"]
    names: tuple[ENSResolverObservation, ENSResolverObservation]
    observed_block: int = Field(ge=0)
    block_hash: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    delegation_isolation: Literal["NOT_PROVEN"] = "NOT_PROVEN"
    authority_verified: Literal[False] = False
    research_dispatch_enabled: Literal[False] = False

    @model_validator(mode="after")
    def consistent_addresses(self) -> Self:
        first, second = self.names
        shared = first.resolver.lower() == second.resolver.lower()
        if first.name == second.name or shared != (self.status == "SHARED_RESOLVER"):
            raise ValueError("Inconsistent resolver comparison")
        return self


class ENSPermissionRequest(CompareENSResolversRequest):
    delegate: Address


class ENSPermissionObservation(ENSResolverObservation):
    root_text_role: bool = Field(strict=True)
    endpoint_text_role: bool = Field(strict=True)

    @model_validator(mode="after")
    def consistent_roles(self) -> Self:
        if self.root_text_role and not self.endpoint_text_role:
            raise ValueError("Root text role must imply endpoint permission")
        return self


class ENSPermissionSnapshot(ENSResolverBoundary):
    names: tuple[ENSPermissionObservation, ENSPermissionObservation]
    delegate: Address
    key: Literal["org.orca.endpoint"]
    resource: str = Field(pattern=r"^0x[0-9a-fA-F]{64}$")


class CreateIdentityBinding(ResolveENSRequest):
    agent_id: UUID
    expected_version: int = Field(ge=1)
    arkiv_wallet: Address


class IdentityBindingChallenge(Contract):
    id: UUID
    manifest: dict[str, Any]


class SubmitIdentityBinding(Contract):
    organization_id: UUID
    challenge_id: UUID
    controller: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")
    wallet: str = Field(pattern=r"^0x[0-9a-fA-F]{130}$")
    service: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")


class IdentityBindingEvidence(Contract):
    challenge_id: UUID
    agent_id: UUID
    status: Literal["SIGNATURES_VALID"] = "SIGNATURES_VALID"
    ens_digest: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    arkiv_digest: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    observed_block: int = Field(ge=1)
    block_hash: str = Field(pattern=r"^0x[0-9a-f]{64}$")
    authority_verified: Literal[False] = False
    institutional_affiliation_verified: Literal[False] = False
    research_dispatch_enabled: Literal[False] = False

"""Purpose: define a bounded P10 read-only ENS observation contract.
Behavior: distinguish finalized name state from authorization and institutional proof.
Refs: main_implementation plan.md P10.4; api design.md §3.11; Phase: P10.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

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

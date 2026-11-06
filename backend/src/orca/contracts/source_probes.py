"""Purpose: bound operator source-probe requests and distinguish access evidence from science.
Behavior: require an approved distribution and expose durable operation/evidence status.
Refs: API 3.4 source-probes; main plan P03.2/P03.4; Phase: P03.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import (
    Contract,
    Digest,
    GeoScope,
    OpaqueID,
    Operation,
    TimeWindow,
    UTCDateTime,
)


class ProbeLimits(Contract):
    max_bytes: int = Field(default=1_048_576, ge=1, le=1_048_576)
    timeout_seconds: float = Field(default=20, gt=0, le=20)


class ProbeRequest(Contract):
    organization_id: UUID
    provider_id: OpaqueID
    distribution_id: UUID
    region: GeoScope
    time_window: TimeWindow
    limits: ProbeLimits = Field(default_factory=ProbeLimits)


class SourceProbeEvidence(Contract):
    scope: Literal["metadata_access_only"] = "metadata_access_only"
    response_digest: Digest
    response_bytes: int = Field(gt=0, le=1_048_576)
    retrieved_at: UTCDateTime
    metadata_matches_pinned_version: bool
    variable_count: int = Field(ge=0)
    scientific_status: Literal["not_validated"] = "not_validated"
    region_and_time_verified: Literal[False] = False


class SourceProbeStatus(Contract):
    operation: Operation
    attempts: int
    error_code: str | None
    evidence: SourceProbeEvidence | None

"""Purpose: expose bounded scientific metadata fitness and compatibility reports.
Behavior: distinguish definite incompatibility from missing method, QC and baseline evidence.
Refs: API 3.4; main plan P03.5; research-only guide 5; Phase: P03.
"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract, GeoScope, OpaqueID, TimeWindow, UTCDateTime


class FitnessRequest(Contract):
    organization_id: UUID
    method_id: OpaqueID
    version_ref: UUID
    geo_scope: GeoScope
    time_window: TimeWindow
    depth: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    as_of: UTCDateTime | None = None


class DataCompatibilityRequest(Contract):
    organization_id: UUID
    method_id: OpaqueID
    products: list[UUID] = Field(min_length=1, max_length=8)
    baseline_ref: UUID
    geo_scope: GeoScope
    time_window: TimeWindow


class FitnessCheck(Contract):
    name: str
    status: Literal["pass", "fail", "unknown"]
    reason: str


class FitnessReport(Contract):
    status: Literal["incompatible", "unknown"]
    method_id: OpaqueID
    version_refs: list[UUID]
    checks: list[FitnessCheck]
    scientific_validation: Literal["not_validated"] = "not_validated"


class CompatibilityReport(FitnessReport):
    baseline_ref: UUID

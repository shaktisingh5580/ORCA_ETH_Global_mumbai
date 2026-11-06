"""Purpose: bound P03 snapshot acquisition requests and expose durable status.
Behavior: require a pinned version, explicit variables, geometry and UTC interval.
Refs: API 3.4; main plan P03.3/P03.5; Phase: P03.5.
"""

from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract, GeoScope, OpaqueID, Operation, TimeWindow


class SnapshotRequest(Contract):
    organization_id: UUID
    product_version_id: UUID
    geo_scope: GeoScope
    time_window: TimeWindow
    variables: Annotated[list[OpaqueID], Field(min_length=1, max_length=8)]
    depth: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(set(self.variables)) != len(self.variables):
            raise ValueError("duplicate variables")
        if (self.time_window.end - self.time_window.start).total_seconds() > 31 * 86400:
            raise ValueError("snapshot interval exceeds 31 days")
        return self


class SnapshotRequestStatus(Contract):
    operation: Operation
    snapshot_id: UUID | None
    error_code: str | None
    attempts: int

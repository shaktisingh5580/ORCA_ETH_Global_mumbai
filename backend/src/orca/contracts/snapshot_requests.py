"""Purpose: bound P03 snapshot acquisition requests and expose durable status.
Behavior: require a pinned version, explicit variables, geometry and UTC interval.
Refs: API 3.4; main plan P03.3/P03.5; Phase: P03.5.
"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.catalog import ProductVersion
from orca.contracts.common import Contract, GeoScope, OpaqueID, Operation, TimeWindow, UTCDateTime


class SnapshotRequest(Contract):
    organization_id: UUID
    product_version_id: UUID
    geo_scope: GeoScope
    time_window: TimeWindow
    variables: Annotated[list[OpaqueID], Field(min_length=1, max_length=8)]
    depth: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    as_of: UTCDateTime | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if len(set(self.variables)) != len(self.variables):
            raise ValueError("duplicate variables")
        if (self.time_window.end - self.time_window.start).total_seconds() > 31 * 86400:
            raise ValueError("snapshot interval exceeds 31 days")
        if self.as_of is not None:
            if self.as_of > datetime.now(timezone.utc):
                raise ValueError("analysis cutoff cannot be in the future")
            if self.time_window.end > self.as_of:
                raise ValueError("snapshot interval must end at or before its analysis cutoff")
        return self


class SnapshotRequestStatus(Contract):
    operation: Operation
    snapshot_id: UUID | None
    error_code: str | None
    attempts: int


def request_digest(body: SnapshotRequest, version: ProductVersion) -> str:
    """Build an immutable tenant, rights, source-version, and request-scope cache identity."""
    if body.product_version_id != version.id:
        raise ValueError("snapshot request and pinned source version differ")
    value: dict[str, object] = {
        "request": body.model_dump(mode="json"),
        "source_scope": {
            "tenant_id": str(version.tenant_id),
            "product_id": str(version.product_id),
            "version_id": str(version.id),
            "source_version": version.source_version,
            "metadata_digest": version.metadata_digest,
            "license_ref": version.license.license_ref,
            "license_evidence_digest": version.license.evidence_digest,
            "can_preserve": version.license.can_preserve,
        },
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

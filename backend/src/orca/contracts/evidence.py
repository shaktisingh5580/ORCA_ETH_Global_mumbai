"""Purpose: define P06 evidence and map descriptors with explicit provenance boundaries.
Behavior: require geographic bounds, CRS, units, legend and immutable manifest references before publication.
Refs: main_implementation plan.md P06.1; api design.md §§3.7, 6.6; Phase: P06.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from orca.contracts.common import Contract, Digest
from orca.contracts.science_evidence import ScienceEvidenceManifest
from orca.science.manifests import verify_manifest


class MapDescriptor(Contract):
    id: UUID
    artifact_id: UUID
    bbox: tuple[float, float, float, float]
    crs: Literal["EPSG:4326"]
    unit: str = Field(min_length=1, max_length=40)
    legend: str = Field(min_length=1, max_length=500)
    source_manifest_digest: Digest
    status: Literal["available", "unavailable"]
    unavailable_reason: str | None = None

    @model_validator(mode="after")
    def valid_bounds_and_status(self) -> "MapDescriptor":
        west, south, east, north = self.bbox
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("bbox must be ordered WGS84 bounds")
        if self.status == "unavailable" and not self.unavailable_reason:
            raise ValueError("unavailable maps require a reason")
        if self.status == "available" and self.unavailable_reason is not None:
            raise ValueError("available maps cannot have an unavailable reason")
        return self


class EvidenceClaim(Contract):
    id: UUID
    artifact_id: UUID
    manifest_digest: Digest
    statement: str = Field(min_length=1, max_length=2000)
    support: Literal["supported", "insufficient_evidence", "inconclusive", "invalid"]


class ArtifactView(Contract):
    id: UUID
    investigation_id: UUID
    run_id: UUID
    evidence_manifest_id: UUID
    manifest_digest: Digest
    scientific_status: Literal["supported", "insufficient_evidence"]
    source_mode: Literal["live_real", "preserved_real", "synthetic_test"]
    method_id: str
    method_version: str
    created_at: datetime


def validate_claim_support(
    claim: EvidenceClaim, manifest: ScienceEvidenceManifest, *, source_stale: bool = False
) -> None:
    """Reject a public claim without matching immutable evidence and fresh support."""
    verify_manifest(manifest)
    if claim.manifest_digest != manifest.manifest_digest:
        raise ValueError("claim references a different evidence manifest")
    if claim.support == "supported" and (manifest.result_status != "supported" or source_stale):
        raise ValueError("supported claim requires complete, non-stale scientific evidence")
    if claim.support != manifest.result_status:
        raise ValueError("claim status must equal the immutable result status")

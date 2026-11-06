"""Purpose: define the minimal P03 scientific catalog and explicit preservation rights.
Behavior: reject ambiguous SST semantics and keep versioned metadata immutable.
Refs: API 3.4/4.2; research-only guide 5; P03-research-scope.json; Phase: P03.1.
"""

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator
from typing_extensions import Self

from orca.contracts.common import Contract, Digest, Latitude, Longitude, OpaqueID, UTCDateTime


class Coverage(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    bbox: tuple[Longitude, Latitude, Longitude, Latitude]
    start: UTCDateTime
    end: UTCDateTime

    @model_validator(mode="after")
    def ordered(self) -> Self:
        west, south, east, north = self.bbox
        if west >= east or south >= north:
            raise ValueError("ordered non-antimeridian bounding box required")
        if self.end <= self.start:
            raise ValueError("coverage end must be after start")
        return self


class LicenseProfile(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    license_ref: OpaqueID | None = None
    evidence_digest: Digest | None = None
    can_preserve: bool = False
    can_redistribute: bool = False

    @model_validator(mode="after")
    def require_evidence(self) -> Self:
        if (self.can_preserve or self.can_redistribute) and not (
            self.license_ref and self.evidence_digest
        ):
            raise ValueError("affirmative rights require a license and evidence digest")
        return self


class ScientificProduct(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    tenant_id: UUID
    provider_id: OpaqueID
    source_product_id: OpaqueID
    kind: Literal["observation", "reanalysis", "forecast", "derived", "static"]
    access_class: Literal["public", "restricted"] = "restricted"
    metadata_publication_allowed: bool = False
    status: Literal["candidate", "probe_passed", "validated"] = "candidate"


class SourceProvider(Contract):
    id: OpaqueID
    live_verification: Literal["unknown"] = "unknown"


class ProductVersion(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    tenant_id: UUID
    product_id: UUID
    source_version: OpaqueID
    metadata_digest: Digest
    native_variable: OpaqueID
    standard_name: Literal["sea_surface_temperature", "sea_surface_foundation_temperature"]
    physical_definition: str = Field(min_length=1, max_length=2000)
    native_units: Literal["K", "degree_Celsius", "Degree C", "degree_C"]
    dimensions: tuple[OpaqueID, ...] = ()
    depth_m: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    calendar: OpaqueID
    retrieved_at: UTCDateTime
    published_at: UTCDateTime | None = None
    available_at: UTCDateTime | None = None
    license: LicenseProfile
    coverage: Coverage | None = None

    def can_preserve_for(self, tenant_id: UUID) -> bool:
        return self.tenant_id == tenant_id and self.license.can_preserve


class ProductCoverage(Contract):
    product_id: UUID
    version_id: UUID | None
    coverage: Coverage | None
    valid_observation_coverage_verified: Literal[False] = False


class CanonicalVariableMapping(Contract):
    product_id: UUID
    version_id: UUID
    metadata_digest: Digest
    native_name: OpaqueID
    standard_name: Literal["sea_surface_temperature", "sea_surface_foundation_temperature"]
    native_units: str
    dimensions: tuple[OpaqueID, ...]
    physical_definition: str
    depth_m: float | None
    calendar: OpaqueID

    @classmethod
    def from_version(cls, version: ProductVersion) -> "CanonicalVariableMapping":
        return cls(
            product_id=version.product_id,
            version_id=version.id,
            metadata_digest=version.metadata_digest,
            native_name=version.native_variable,
            standard_name=version.standard_name,
            native_units=version.native_units,
            dimensions=version.dimensions,
            physical_definition=version.physical_definition,
            depth_m=version.depth_m,
            calendar=version.calendar,
        )


def visible_metadata(product: ScientificProduct, tenant_id: UUID) -> bool:
    """Apply metadata visibility only; this never authorizes downloading source bytes."""
    return product.tenant_id == tenant_id or (
        product.access_class == "public" and product.metadata_publication_allowed
    )

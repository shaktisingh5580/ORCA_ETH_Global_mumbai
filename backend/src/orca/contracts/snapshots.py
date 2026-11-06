"""Purpose: define immutable P03 snapshot provenance without claiming scientific validity.
Behavior: require exact content identity, pinned metadata and a credential-free source URI.
Refs: API 4.2; research-only guide 5.2; main plan P03.3; Phase: P03.3.
"""

from typing import Literal, Self
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from orca.contracts.catalog import ProductVersion
from orca.contracts.common import Contract, Digest, UTCDateTime


class DatasetSnapshotManifest(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["orca.dataset-snapshot/1.0"] = "orca.dataset-snapshot/1.0"
    id: UUID
    tenant_id: UUID
    product_version: ProductVersion
    raw_content_digest: Digest
    byte_count: int = Field(gt=0)
    retrieved_at: UTCDateTime
    origin_uri: str = Field(max_length=2048)
    scientific_status: Literal["not_validated"] = "not_validated"
    quality_policy_ref: str | None = None
    acquisition_request_digest: Digest | None = None

    @field_validator("origin_uri")
    @classmethod
    def safe_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(ord(char) <= 32 for char in value)
        ):
            raise ValueError("origin must be HTTPS without credentials, query or fragment")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.product_version.tenant_id != self.tenant_id:
            raise ValueError("snapshot and product version must share tenant")
        if not self.product_version.license.can_preserve:
            raise ValueError("snapshot preservation rights required")
        return self


class DatasetSnapshot(Contract):
    id: UUID
    product_id: UUID
    version_id: UUID
    raw_content_digest: Digest
    byte_count: int = Field(gt=0)
    retrieved_at: UTCDateTime
    scientific_status: Literal["not_validated"] = "not_validated"

    @classmethod
    def from_manifest(cls, manifest: DatasetSnapshotManifest) -> Self:
        return cls(
            id=manifest.id,
            product_id=manifest.product_version.product_id,
            version_id=manifest.product_version.id,
            raw_content_digest=manifest.raw_content_digest,
            byte_count=manifest.byte_count,
            retrieved_at=manifest.retrieved_at,
        )

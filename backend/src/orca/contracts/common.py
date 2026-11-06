"""Purpose: define shared strict wire contracts without database/provider dependencies.
Behavior: enforce finite spatial values, explicit timezones, IDs and decimal strings.
Refs: API sections 2 and 4; docs/decisions/P01-contract-reconciliation.md; Phase: P01.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Generic, Literal, Self, TypeVar
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)
from shapely.geometry import shape


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def timestamp_input(value: object) -> object:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("RFC3339 timestamp with explicit offset required")
    return value


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone required")
    return value.astimezone(timezone.utc)


UTCDateTime = Annotated[
    datetime,
    BeforeValidator(timestamp_input),
    AfterValidator(utc),
    PlainSerializer(lambda v: v.isoformat().replace("+00:00", "Z"), return_type=str),
]
OpaqueID = Annotated[str, Field(strict=True, min_length=1, max_length=128, pattern=r"^\S+$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]


def money_string(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9]\d{0,17})(?:\.\d{1,8})?", value):
        raise ValueError("nonnegative decimal string required, at most 8 fractional digits")
    return format(Decimal(value), "f")


Money = Annotated[
    str,
    BeforeValidator(money_string),
    WithJsonSchema({"type": "string", "pattern": r"^(0|[1-9]\d{0,17})(\.\d{1,8})?$"}),
]
Longitude = Annotated[float, Field(strict=True, ge=-180, le=180, allow_inf_nan=False)]
Latitude = Annotated[float, Field(strict=True, ge=-90, le=90, allow_inf_nan=False)]
Position = tuple[Longitude, Latitude]
Ring = Annotated[list[Position], Field(min_length=4, max_length=5000)]


class Polygon(Contract):
    type: Literal["Polygon"]
    coordinates: Annotated[list[Ring], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        if sum(len(ring) for ring in self.coordinates) > 5000:
            raise ValueError("too many vertices")
        for ring in self.coordinates:
            if ring[0] != ring[-1]:
                raise ValueError("ring must be closed")
            if any(abs(a[0] - b[0]) > 180 for a, b in zip(ring, ring[1:])):
                raise ValueError("antimeridian normalization not supported in this contract")
        polygon = shape(self.model_dump())
        if polygon.is_empty or not polygon.is_valid or polygon.area == 0:
            raise ValueError("invalid or empty polygon")
        return self


class TimeWindow(Contract):
    start: UTCDateTime
    end: UTCDateTime
    calendar: str | None = None
    temporal_resolution: str | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


T = TypeVar("T")


class Page(Contract, Generic[T]):
    items: list[T]
    next_cursor: str | None
    page_size: Annotated[int, Field(ge=1, le=100)] = 20


class IdentityReference(Contract):
    id: UUID
    evidence_id: OpaqueID


class MoneyBudget(Contract):
    max_amount: Money
    currency: Literal["USD"] = "USD"
    spent_amount: Money = "0"
    reserved_amount: Money = "0"

    @model_validator(mode="after")
    def within_budget(self) -> Self:
        if Decimal(self.spent_amount) + Decimal(self.reserved_amount) > Decimal(self.max_amount):
            raise ValueError("spent and reserved amounts exceed budget")
        return self


class ResearchProject(Contract):
    id: UUID
    organization_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=200)]
    description: str | None
    access_class: Literal["public", "organization", "project", "restricted", "confidential"]
    state: Literal["active", "archived"]
    created_at: UTCDateTime
    updated_at: UTCDateTime
    etag: str


class ScientificOutcome(Contract):
    status: Literal["supported", "insufficient_evidence", "inconclusive", "invalid"]
    claim_ids: list[OpaqueID]
    artifact_ids: list[OpaqueID]
    limitations: list[str]
    quality_metrics: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def support_required(self) -> Self:
        if self.status == "supported" and (not self.claim_ids or not self.artifact_ids):
            raise ValueError("supporting evidence references required")
        if self.status != "supported" and not self.limitations:
            raise ValueError("limitations required for unsupported result")
        return self


class Liveness(Contract):
    status: Literal["alive"] = "alive"


class ReadinessReport(Contract):
    components: dict[str, Literal["not_implemented", "ready", "unavailable"]]
    release_profile: str
    blocked_gates: list[str]


class Capability(Contract):
    name: str
    implemented: bool = False
    live_verified: bool = False

    @model_validator(mode="after")
    def no_false_live_claim(self) -> Self:
        if self.live_verified and not self.implemented:
            raise ValueError("unimplemented capability cannot be live verified")
        return self


class APIMetadata(Contract):
    revision: str
    release_profile: str
    enabled_capabilities: list[Capability]


class Point(Contract):
    type: Literal["Point"]
    coordinates: Position


class MultiPolygon(Contract):
    type: Literal["MultiPolygon"]
    coordinates: Annotated[list[list[Ring]], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_geometry(self) -> Self:
        if sum(len(r) for p in self.coordinates for r in p) > 5000:
            raise ValueError("too many vertices")
        for coordinates in self.coordinates:
            Polygon(type="Polygon", coordinates=coordinates)
        if not shape(self.model_dump()).is_valid:
            raise ValueError("invalid multipolygon")
        return self


class GeoScope(Contract):
    type: Literal["Point", "Polygon", "MultiPolygon"]
    geometry: Annotated[Point | Polygon | MultiPolygon, Field(discriminator="type")]
    crs: Literal["EPSG:4326"] = "EPSG:4326"

    @model_validator(mode="after")
    def consistent_type(self) -> Self:
        if self.type != self.geometry.type:
            raise ValueError("scope type must match geometry type")
        return self


class ResourceRef(Contract):
    type: OpaqueID
    id: OpaqueID
    version: OpaqueID
    digest: Digest | None = None
    access_class: (
        Literal["public", "organization", "project", "restricted", "confidential"] | None
    ) = None


class Operation(Contract):
    id: UUID
    kind: OpaqueID
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    resource_ref: ResourceRef | None
    created_at: UTCDateTime
    updated_at: UTCDateTime
    progress: Annotated[float, Field(ge=0, le=1)] | None = None
    retry_after_seconds: Annotated[int, Field(ge=0)] | None = None
    links: dict[str, str] = Field(default_factory=dict)


class SourceClock(Contract):
    observed_at: UTCDateTime | None
    valid_at: UTCDateTime | None
    issued_at: UTCDateTime | None
    published_at: UTCDateTime | None
    available_at: UTCDateTime | None
    retrieved_at: UTCDateTime | None
    availability_status: Literal["complete", "partial", "unavailable"]
    missing_reasons: dict[str, Annotated[str, Field(min_length=1)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def account_for_missing_times(self) -> Self:
        fields = (
            "observed_at",
            "valid_at",
            "issued_at",
            "published_at",
            "available_at",
            "retrieved_at",
        )
        missing = {name for name in fields if getattr(self, name) is None}
        if set(self.missing_reasons) != missing:
            raise ValueError("each missing timestamp needs its own reason; no invented times")
        expected = (
            "complete"
            if not missing
            else "unavailable"
            if len(missing) == len(fields)
            else "partial"
        )
        if self.availability_status != expected:
            raise ValueError("availability status disagrees with timestamps")
        return self


class ScientificQuantity(Contract):
    value: float | None
    unit: Annotated[str, Field(min_length=1)]
    status: Literal["known", "unknown", "not_applicable"]
    uncertainty: Annotated[float, Field(ge=0)] | None = None
    method_id: OpaqueID | None = None
    precision_policy: str | None = None

    @model_validator(mode="after")
    def distinguish_unknown(self) -> Self:
        if (self.status == "known") != (self.value is not None):
            raise ValueError("only known quantities have numeric values")
        if self.value is None and self.uncertainty is not None:
            raise ValueError("unknown quantity cannot have numeric uncertainty")
        return self


class ProvenanceRef(Contract):
    source_id: OpaqueID
    product_id: OpaqueID
    version_ref: OpaqueID
    snapshot_digest: Digest
    license_ref: OpaqueID | None = None
    quality_ref: OpaqueID | None = None
    source_url: str | None = None


class MethodRef(Contract):
    method_id: OpaqueID
    method_version: OpaqueID
    code_digest: Digest
    environment_digest: Digest | None = None
    parameters_digest: Digest | None = None


class AccessDecision(Contract):
    allowed: bool
    policy_version: OpaqueID
    evaluated_at: UTCDateTime
    reason_code: OpaqueID | None = None


class ChainReference(Contract):
    chain_id: Annotated[int, Field(strict=True, gt=0)]
    tx_hash: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    entity_key: OpaqueID
    owner: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{40}$")]
    state: Literal["submitted", "confirmed", "indexed", "expired", "reorged", "unknown"]
    block_number: Annotated[int, Field(strict=True, ge=0)] | None = None
    observed_at: UTCDateTime | None = None
    expires_at: UTCDateTime | None = None
    index_confirmed_at: UTCDateTime | None = None

"""Purpose: describe bounded tabular inputs and explicit normalization approvals.
Behavior: reject undeclared fields, units and policies before transformation.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md sections 3-4; Phase: A01.
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from orca.contracts.common import Contract


class DatasetInput(Contract):
    name: str = Field(min_length=1, max_length=120)
    format: Literal["csv", "json"]
    content: str = Field(min_length=1, max_length=5_242_880)
    source_mode: Literal["uploaded", "synthetic_demo"] = "uploaded"


class Mapping(Contract):
    timestamp_column: str = Field(min_length=1, max_length=100)
    value_column: str = Field(min_length=1, max_length=100)
    id_column: str | None = Field(default=None, max_length=100)
    latitude_column: str | None = Field(default=None, max_length=100)
    longitude_column: str | None = Field(default=None, max_length=100)
    unit: Literal["C", "K"]
    naive_timezone: Literal["UTC"] | None = None
    variable: Literal["sea_surface_temperature"] = "sea_surface_temperature"
    duplicate_policy: Literal["reject", "keep_first"] = "reject"


class NormalizeRequest(Contract):
    mapping: Mapping
    approve: Literal[True]


class DatasetView(Contract):
    id: UUID
    project_id: UUID
    name: str
    source_mode: str
    content_digest: str
    inspection: dict[str, Any]


class NormalizationView(Contract):
    id: UUID
    dataset_id: UUID
    mapping: Mapping
    artifact_digest: str
    result: dict[str, Any]

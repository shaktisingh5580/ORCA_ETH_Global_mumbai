"""Purpose: describe the accelerated research/Arkiv demo without claiming validated science.
Behavior: bind descriptive results to exact snapshots and expose a minimal consented public record.
Refs: accelerated-research-arkiv-demo.json; scientific engine 7; Phase: accelerated P04/P06/P11 demo.
"""

from typing import Literal
from uuid import UUID

from orca.contracts.common import Contract, Digest


class DemoRunRequest(Contract):
    organization_id: UUID
    snapshot_id: UUID


class DemoResult(Contract):
    id: UUID
    snapshot_id: UUID
    source_digest: Digest
    method: Literal["orca.descriptive-sst/1.0"] = "orca.descriptive-sst/1.0"
    units: Literal["degree_Celsius"] = "degree_Celsius"
    row_count: int
    finite_count: int
    minimum: float
    maximum: float
    sample_mean: float
    daily_sample_means: dict[str, float]
    measurement_definition: str
    scientific_status: Literal["not_validated"] = "not_validated"
    limitations: list[str]


class PublicDemoResult(Contract):
    schema_version: Literal["orca.public-demo-result/1.0"] = "orca.public-demo-result/1.0"
    result_id: UUID
    result_digest: Digest
    method: Literal["orca.descriptive-sst/1.0"] = "orca.descriptive-sst/1.0"
    scientific_status: Literal["not_validated"] = "not_validated"
    disclosure: Literal["explicitly_approved_public_reference"] = (
        "explicitly_approved_public_reference"
    )
